import base64
import io
import json
import logging
import math
import re
import time
from typing import Any

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from pypdf import PdfReader

from app.core.config import settings
from app.schemas.extraction import ExtractionProvider

logger = logging.getLogger(__name__)

# Timeout configuration for external APIs (seconds)
OPENAI_API_TIMEOUT = 120  # 2 minutes max for OpenAI API calls
OCR_API_TIMEOUT = 90
PDF_SIZE_WARNING_MB = 50  # Warn and use streaming for PDFs larger than this


class ExtractionConfigError(Exception):
    pass


class ExtractionProcessingError(Exception):
    pass


KYC_IDENTITY_RE = re.compile(
    r"(aadhar|aadhaar|\bkyc\b|know\s*your\s*customer|pan\s*card|\bpan\b|voter\s*id|passport|driving\s*license|id\s*proof|identity\s*proof|\be-?kyc\b|\bckyc\b|frm[-_ ]?id)",
    re.I,
)
KYC_FILENAME_RE = re.compile(
    r"(aadhar|aadhaar|\bkyc\b|pan\s*card|passport|voter\s*id|driving\s*license|id\s*proof|identity\s*proof|\be-?kyc\b|\bckyc\b|frm[-_ ]?id)",
    re.I,
)
CLINICAL_SIGNAL_RE = re.compile(
    r"\b(diagnosis|admission|admitted|discharge|surgery|procedure|complaints?|treatment|medicine|investigation|hospital|ward|patient|operation|post\s*op|clinical)\b",
    re.I,
)
ORG_NAME_RE = re.compile(
    r"(hospital|clinic|diagnostic|laboratory|lab\b|society|insurance|limited|ltd\b|llp|plaza)",
    re.I,
)
GSTIN_RE = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", re.I)
PHARMACY_HINT_RE = re.compile(
    r"\b(pharmacy|chemist|medical\s*store|drug\s*store|medicals)\b",
    re.I,
)
HOSPITAL_HINT_RE = re.compile(
    r"\b(hospital|clinic|nursing\s*home|medical\s*centre|medical\s*center)\b",
    re.I,
)


def _looks_like_kyc_document(document_name: str, text: str = "") -> bool:
    name = str(document_name or "")
    if KYC_FILENAME_RE.search(name):
        return True

    sample = str(text or "")[:12000]
    if not sample.strip():
        return False

    kyc_hits = len(KYC_IDENTITY_RE.findall(sample))
    clinical_hits = len(CLINICAL_SIGNAL_RE.findall(sample))
    # Avoid skipping merged claim documents that mention KYC once/twice.
    return kyc_hits >= 4 and clinical_hits <= 1


def _sanitize_person_name(value: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if ORG_NAME_RE.search(v):
        return ""
    return v


def _apply_kyc_exclusion(entities: dict[str, Any], reason: str) -> dict[str, Any]:
    out = dict(entities or {})
    out["name"] = ""
    out["patient_name"] = ""
    out["diagnosis"] = ""
    out["clinical_findings"] = ""
    out["all_investigation_reports_with_values"] = []
    out["all_investigation_report_lines"] = []
    out["detailed_conclusion"] = reason
    out["hospital_name"] = ""
    out["pharmacy_name"] = ""
    out["gst_number"] = ""
    out["vendor_name"] = ""
    out["vendor_gstin"] = ""
    out["hospital_address"] = ""
    out["treating_doctor"] = ""
    out["doctor_registration_number"] = ""
    out["medicine_used"] = ""
    out["bill_amount"] = ""
    out["claim_amount"] = ""
    out["kyc_excluded"] = True
    out["kyc_exclusion_reason"] = reason
    return out


def _parse_json_payload(raw: Any) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        return None

    text = raw.strip()
    if not text:
        return None

    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

    # Some model outputs contain multiple JSON objects back-to-back.
    # Parse the first complete object to avoid falling back to noisy raw text.
    obj_start = text.find("{")
    if obj_start >= 0:
        depth = 0
        in_str = False
        escaped = False
        obj_end = -1
        for idx in range(obj_start, len(text)):
            ch = text[idx]
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    obj_end = idx
                    break
        if obj_end > obj_start:
            try:
                parsed = json.loads(text[obj_start : obj_end + 1])
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                return None
    return None

def _normalize_evidence_refs(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []

    normalized: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, dict):
            normalized.append(item)
            continue
        if item is None:
            continue

        snippet = str(item).strip()
        if not snippet:
            continue
        normalized.append(
            {
                "type": "text",
                "field": "evidence",
                "snippet": snippet,
            }
        )
    return normalized



def _normalize_lookup_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_to_text(item) for item in value]
        return "\n".join([part for part in parts if part])
    if isinstance(value, dict):
        parts: list[str] = []
        for k, v in value.items():
            txt = _to_text(v)
            if txt:
                parts.append(f"{k}: {txt}")
        return " | ".join(parts)
    return str(value).strip()


def _find_entity_value(entities: dict[str, Any], aliases: list[str]) -> Any:
    if not isinstance(entities, dict):
        return None
    alias_keys = [_normalize_lookup_key(alias) for alias in aliases]
    for key, value in entities.items():
        key_norm = _normalize_lookup_key(key)
        if not key_norm:
            continue
        for alias in alias_keys:
            if key_norm == alias or alias in key_norm or key_norm in alias:
                return value
    return None


def _normalize_amount_text(value: Any) -> str:
    raw = _to_text(value)
    if not raw:
        return ""
    normalized = raw.replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", normalized)
    if not m:
        return ""
    num = m.group(1)
    if "." in num:
        num = num.rstrip("0").rstrip(".")
    return num


def _extract_bill_amount_from_text(src: str) -> str:
    patterns = [
        r"(?:claim(?:ed)?\s*amount|amount\s*claimed|bill\s*amount|total\s*bill(?:\s*amount)?|invoice\s*amount|net\s*payable)\s*[:\-]?\s*(?:inr|rs\.?|rupees)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
        r"(?:inr|rs\.?|rupees)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, src or "", re.I)
        if m:
            return _normalize_amount_text(m.group(1))
    return ""


def _looks_like_billing_or_rate_text(value: Any) -> bool:
    text = _to_text(value)
    if not text:
        return False
    low = text.lower()
    if re.search(r"\b(?:rupees?|inr|rs\.?)\b", low):
        return True
    if re.search(r"\b(?:bill(?:ing)?|invoice|receipt|cash|discount|paid|payment|charges?|rate|price)\b", low):
        return True
    if re.search(r"\b(?:total\s*(?:bill|amount|sum|payment)|final\s*payment|amount\s*claimed|claim(?:ed)?\s*amount)\b", low):
        return True
    if re.search(r"\bvalue\s*:\s*\d+(?:\.\d+)?\s*(?:rupees?|inr|rs\.?)\b", low):
        return True
    return False


def _looks_like_hospital_address_text(value: Any) -> bool:
    text = _to_text(value)
    if not text:
        return False
    low = text.lower()
    if re.search(r"\b(?:hospital\s*address|full\s*postal\s*address|address\s*of\s*hospital|hospital\s*addr(?:ess)?)\b", low):
        return True
    if re.search(r"^(?:add(?:ress)?\s*[:\-])", low):
        return True

    address_tokens = (
        "road", "rd", "street", "st", "lane", "ln", "nagar", "colony", "city",
        "district", "state", "pin", "pincode", "zip", "plot", "floor", "building",
        "plaza", "near", "opp", "opposite", "shop", "apartment", "apt", "flat",
        "unit", "society", "chsl", "complex", "tower", "wing", "sector", "block",
        "east", "west", "north", "south", "taluka", "tehsil",
    )
    token_hits = 0
    for tok in address_tokens:
        if re.search(rf"\b{re.escape(tok)}\b", low):
            token_hits += 1

    if re.search(r"\b(?:shop|apartment|apt|flat|unit)\s*no\.?\s*\d+\b", low) and re.search(r"\b\d{6}\b", low):
        return True
    if token_hits >= 2 and (re.search(r"\d{3,}", low) or re.search(r"\b(?:pin|pincode|zip)\b", low)):
        return True
    if token_hits >= 1 and low.count(",") >= 2 and re.search(r"\b\d{6}\b", low):
        return True
    return False


def _clean_hospital_name_text(value: Any) -> str:
    text = _to_text(value)
    if not text:
        return ""
    m = re.search(
        r"([A-Za-z][A-Za-z .&'\-]{2,90}\b(?:Hospital|Clinic|Nursing Home|Medical Centre|Medical Center|Research Centre|Research Center))",
        text,
        re.I,
    )
    if m:
        return _to_text(m.group(1))
    if _looks_like_hospital_address_text(text):
        return ""
    return text


def _clean_pharmacy_name_text(value: Any) -> str:
    text = _to_text(value)
    if not text:
        return ""
    m = re.search(
        r"([A-Za-z][A-Za-z0-9 .&'\-]{2,100}\b(?:Pharmacy|Chemist|Medical(?:\s*Store)?|Drug\s*Store|Medicals))",
        text,
        re.I,
    )
    if m:
        return _to_text(m.group(1))
    if _looks_like_hospital_address_text(text):
        return ""
    return text


def _normalize_gst_number(value: Any) -> str:
    raw = _to_text(value).upper()
    if not raw:
        return ""
    m = GSTIN_RE.search(raw)
    if not m:
        return ""
    return (m.group(0) or "").upper()


def _clean_header_identity_name(value: Any) -> str:
    text = _to_text(value).strip(" -:\t|")
    if not text:
        return ""
    text = re.sub(
        r"^(?:hospital(?:\s*name)?|name\s*of\s*hospital|pharmacy(?:\s*name)?|name\s*of\s*pharmacy|chemist(?:\s*name)?|medical\s*store(?:\s*name)?)\s*[:\-]\s*",
        "",
        text,
        flags=re.I,
    ).strip(" -:\t|")
    text = re.sub(r"\bGST(?:IN)?\b\s*[:\-].*$", "", text, flags=re.I).strip(" -:\t|")
    return text


def _looks_like_header_metadata_line(value: Any) -> bool:
    text = _to_text(value)
    if not text:
        return True
    low = text.lower()
    if _looks_like_hospital_address_text(text):
        return True
    if _looks_like_billing_or_rate_text(text):
        return True
    if re.search(r"\b(?:invoice|bill\s*no|receipt|date|time|patient|doctor|contact|mobile|phone|email|website|www\.|qty|quantity|hsn|cgst|sgst|igst|taxable|subtotal|total|discount|cash|upi|pan)\b", low):
        return True
    if re.search(r"(?:\+?91[\s\-]*)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b", low):
        return True
    return False


def _extract_top_header_fields(text: str) -> dict[str, str]:
    lines = [ln.strip() for ln in re.split(r"\r\n|\r|\n", str(text or "")) if (ln or "").strip()]
    header_lines = lines[:22]
    hospital_name = ""
    pharmacy_name = ""
    gst_number = ""

    for line in header_lines:
        if not gst_number:
            gst_number = _normalize_gst_number(line)
        if gst_number:
            break

    hospital_label_patterns = [
        r"(?:hospital\s*name|name\s*of\s*hospital|treating\s*hospital|provider\s*hospital)\s*[:\-]\s*([^\n\r]{2,220})",
    ]
    pharmacy_label_patterns = [
        r"(?:pharmacy\s*name|name\s*of\s*pharmacy|chemist\s*name|medical\s*store\s*name|vendor\s*name)\s*[:\-]\s*([^\n\r]{2,220})",
    ]

    for line in header_lines:
        if not hospital_name:
            for pat in hospital_label_patterns:
                m = re.search(pat, line, re.I)
                if m:
                    candidate = _clean_hospital_name_text(_clean_header_identity_name(m.group(1)))
                    if candidate and not _looks_like_header_metadata_line(candidate):
                        hospital_name = candidate
                        break
        if not pharmacy_name:
            for pat in pharmacy_label_patterns:
                m = re.search(pat, line, re.I)
                if m:
                    candidate = _clean_pharmacy_name_text(_clean_header_identity_name(m.group(1)))
                    if candidate and not _looks_like_header_metadata_line(candidate):
                        pharmacy_name = candidate
                        break

    for line in header_lines:
        candidate_raw = _clean_header_identity_name(line)
        if not candidate_raw:
            continue
        if _looks_like_header_metadata_line(candidate_raw):
            continue
        if not pharmacy_name and PHARMACY_HINT_RE.search(candidate_raw):
            pharmacy_name = _clean_pharmacy_name_text(candidate_raw)
            continue
        if not hospital_name and HOSPITAL_HINT_RE.search(candidate_raw) and not PHARMACY_HINT_RE.search(candidate_raw):
            hospital_name = _clean_hospital_name_text(candidate_raw)
            continue

    if not hospital_name:
        for line in header_lines[:6]:
            candidate = _clean_hospital_name_text(_clean_header_identity_name(line))
            if not candidate or _looks_like_header_metadata_line(candidate):
                continue
            if HOSPITAL_HINT_RE.search(candidate):
                hospital_name = candidate
                break

    if not pharmacy_name:
        for line in header_lines[:10]:
            candidate = _clean_pharmacy_name_text(_clean_header_identity_name(line))
            if not candidate or _looks_like_header_metadata_line(candidate):
                continue
            if PHARMACY_HINT_RE.search(candidate):
                pharmacy_name = candidate
                break

    return {
        "hospital_name": _to_text(hospital_name),
        "pharmacy_name": _to_text(pharmacy_name),
        "gst_number": _normalize_gst_number(gst_number),
    }


def _clean_clinical_findings_text(value: Any) -> str:
    lines: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", _to_text(value)):
        t = raw.strip(" -:\t")
        if not t:
            continue
        low = t.lower()
        if _looks_like_billing_or_rate_text(t):
            continue
        if _looks_like_hospital_address_text(t):
            continue
        if re.search(r"\b(?:follow\s*up(?:\s*to)?|review\s+after|in\s+case\s+of\s+emergency|call\s+us|contact\s*(?:us|no)?|helpline|mobile(?:\s*number)?|phone(?:\s*number)?)\b", low):
            continue
        if re.search(r"(?:\+?91[\s\-]*)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b", low):
            continue
        if re.search(r"^(patient\s*name|name\s*of\s*the\s*establishment|hospital\s*address|full\s*postal\s*address|add\s*:|admit(?:ting|ing)\s*dr|consultant\s*name)\b", low):
            continue
        lines.append(t)
    seen: set[str] = set()
    out: list[str] = []
    for line in lines:
        k = line.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(line)
    return "\n".join(out)


def _extract_hospital_address_from_lines(lines: list[str], start_idx: int) -> str:
    out: list[str] = []
    address_tokens = (
        "road", "rd", "street", "st", "lane", "ln", "nagar", "colony", "city", "district", "state",
        "pin", "pincode", "zip", "plot", "floor", "building", "plaza", "near", "above", "hyderabad",
        "mumbai", "delhi", "bengaluru", "bangalore", "chennai", "pune", "kolkata",
    )
    for idx in range(start_idx, min(start_idx + 4, len(lines))):
        line = str(lines[idx] or "").strip(" -:\t")
        if not line:
            break
        low = line.lower()
        if idx > start_idx and re.match(r"^[A-Za-z ]{2,40}\s*[:\-]", line):
            break
        if idx == start_idx or any(tok in low for tok in address_tokens) or bool(re.search(r"\d{3,}", low)):
            out.append(line)
        else:
            break
    return ", ".join([x for x in out if x]).strip(" ,")


def _normalize_investigation_reports(raw: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                test_name = _to_text(
                    item.get("test_name")
                    or item.get("test")
                    or item.get("name")
                    or item.get("parameter")
                    or item.get("investigation")
                )
                value = _to_text(item.get("value") or item.get("result") or item.get("finding"))
                unit = _to_text(item.get("unit"))
                reference_range = _to_text(item.get("reference_range") or item.get("range") or item.get("normal_range"))
                flag = _to_text(item.get("flag") or item.get("status"))
                observed_at = _to_text(item.get("date") or item.get("observed_at"))

                line_parts: list[str] = []
                if test_name:
                    line_parts.append(test_name)
                if value:
                    val = value + (f" {unit}" if unit else "")
                    line_parts.append(f"Value: {val}")
                if reference_range:
                    line_parts.append(f"Range: {reference_range}")
                if flag:
                    line_parts.append(f"Flag: {flag}")
                if observed_at:
                    line_parts.append(f"Date: {observed_at}")
                line = " | ".join(line_parts).strip()
                if line and not _looks_like_billing_or_rate_text(line):
                    rows.append(
                        {
                            "test_name": test_name,
                            "value": value,
                            "unit": unit,
                            "reference_range": reference_range,
                            "flag": flag,
                            "date": observed_at,
                            "line": line,
                        }
                    )
            else:
                text_line = _to_text(item)
                if text_line and not _looks_like_billing_or_rate_text(text_line):
                    rows.append(
                        {
                            "test_name": "",
                            "value": "",
                            "unit": "",
                            "reference_range": "",
                            "flag": "",
                            "date": "",
                            "line": text_line,
                        }
                    )
    elif isinstance(raw, dict):
        for key, value in raw.items():
            text_line = _to_text(value)
            if text_line and not _looks_like_billing_or_rate_text(text_line):
                rows.append(
                    {
                        "test_name": str(key),
                        "value": text_line,
                        "unit": "",
                        "reference_range": "",
                        "flag": "",
                        "date": "",
                        "line": f"{key}: {text_line}",
                    }
                )
    else:
        text_line = _to_text(raw)
        if text_line and not _looks_like_billing_or_rate_text(text_line):
            rows.append(
                {
                    "test_name": "",
                    "value": "",
                    "unit": "",
                    "reference_range": "",
                    "flag": "",
                    "date": "",
                    "line": text_line,
                }
            )
    return rows


def _extract_focus_fields_from_text(text: str) -> dict[str, Any]:
    src = str(text or "")
    lines = [ln.strip() for ln in re.split(r"\r\n|\r|\n", src) if (ln or "").strip()]
    header_fields = _extract_top_header_fields(src)

    name = ""
    diagnosis = ""
    hospital_name = ""
    pharmacy_name = _clean_pharmacy_name_text(header_fields.get("pharmacy_name"))
    gst_number = _normalize_gst_number(header_fields.get("gst_number"))
    hospital_address = ""
    bill_amount = ""
    clinical: list[str] = []
    investigations: list[dict[str, Any]] = []
    detailed_conclusion = ""

    name_patterns = [
        r"(?:patient\s*name|name\s*of\s*patient|insured\s*name|beneficiary\s*name|policy\s*holder\s*name)\s*[:\-]\s*([^\n\r]{2,140})",
        r"nominee\s*registered\s*[:\-]\s*(?:yes\s*,\s*)?([^\n\r]{2,140})",
    ]
    for pat in name_patterns:
        m = re.search(pat, src, re.I)
        if m:
            name = (m.group(1) or "").strip()
            if name:
                break

    diagnosis_patterns = [
        r"(?:final\s*diagnosis|provisional\s*diagnosis|diagnosis|diagnoses)\s*[:\-]\s*([^\n\r]{2,220})",
        r"impression\s*[:\-]\s*([^\n\r]{2,220})",
    ]
    for pat in diagnosis_patterns:
        m = re.search(pat, src, re.I)
        if m:
            diagnosis = (m.group(1) or "").strip()
            if diagnosis:
                break

    hospital_name_patterns = [
        r"(?:hospital\s*name|name\s*of\s*hospital|treating\s*hospital|provider\s*hospital)\s*[:\-]\s*([^\n\r]{2,220})",
    ]
    for pat in hospital_name_patterns:
        m = re.search(pat, src, re.I)
        if m:
            hospital_name = (m.group(1) or "").strip()
            if hospital_name:
                break

    hospital_address_patterns = [
        r"(?:hospital\s*address|address\s*of\s*hospital|hospital\s*addr(?:ess)?)\s*[:\-]\s*([^\n\r]{5,260})",
    ]
    for pat in hospital_address_patterns:
        m = re.search(pat, src, re.I)
        if m:
            hospital_address = (m.group(1) or "").strip()
            if hospital_address:
                break

    if not hospital_name:
        for idx, line in enumerate(lines):
            low = line.lower()
            if "hospital" in low or "clinic" in low or "nursing home" in low:
                if "address" in low and ":" in line:
                    continue
                if len(line) <= 220:
                    hospital_name = line.strip(" -:\t")
                    if not hospital_address:
                        hospital_address = _extract_hospital_address_from_lines(lines, idx + 1)
                    break
    if not hospital_name:
        hospital_name = _to_text(header_fields.get("hospital_name"))

    if not hospital_address:
        for idx, line in enumerate(lines):
            if re.search(r"hospital\s*address|address\s*of\s*hospital", line, re.I):
                hospital_address = _extract_hospital_address_from_lines(lines, idx)
                if hospital_address:
                    break

    hospital_name = _clean_hospital_name_text(hospital_name)
    pharmacy_name = _clean_pharmacy_name_text(pharmacy_name)
    gst_number = _normalize_gst_number(gst_number)

    bill_amount = _extract_bill_amount_from_text(src)

    investigation_tokens = (
        "hb", "hgb", "hemoglobin", "wbc", "rbc", "platelet", "mcv", "mch", "mchc", "rdw",
        "creatinine", "urea", "bun", "sodium", "potassium", "bilirubin", "sgot", "sgpt", "alt", "ast",
        "test", "lab", "investigation", "culture", "urine", "x-ray", "ct", "mri",
    )
    clinical_tokens = (
        "complaint", "complaints", "symptom", "symptoms", "admitted", "admission", "finding", "findings",
        "history", "examination", "treated", "treatment", "diagnosis", "diagnosed",
    )
    conclusion_tokens = ("conclusion", "recommendation", "opinion", "summary", "final", "decision")

    for line in lines:
        low = line.lower()
        has_number = bool(re.search(r"\d", line))

        if any(tok in low for tok in clinical_tokens) and not _looks_like_billing_or_rate_text(line):
            clinical.append(line)

        has_result_word = bool(re.search(r"\\b(positive|negative|reactive|non-reactive|detected|not detected)\\b", low))
        if any(tok in low for tok in investigation_tokens) and (has_number or has_result_word) and not _looks_like_billing_or_rate_text(line):
            investigations.append(
                {
                    "test_name": "",
                    "value": "",
                    "unit": "",
                    "reference_range": "",
                    "flag": "",
                    "date": "",
                    "line": line,
                }
            )

        if not detailed_conclusion and any(tok in low for tok in conclusion_tokens):
            detailed_conclusion = line

    def _dedup_lines(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    clinical = _dedup_lines(clinical)
    inv_lines = _dedup_lines([str(r.get("line") or "").strip() for r in investigations])
    investigations = [
        {
            "test_name": "",
            "value": "",
            "unit": "",
            "reference_range": "",
            "flag": "",
            "date": "",
            "line": ln,
        }
        for ln in inv_lines
    ]

    treating_doctor = ""
    doctor_registration_number = ""
    medicine_used = ""

    doctor_patterns = [
        r"(?:treat(?:ing)?\s*doctor|consultant\s*doctor|attending\s*doctor|admit\s*dr|treated\s*by)\s*[:\-]\s*([^\n\r]{3,140})",
        r"\b(Dr\.?\s*[A-Z][A-Za-z\s\.]{2,80})\b",
    ]
    for pat in doctor_patterns:
        m_doc = re.search(pat, src, re.I)
        if m_doc:
            treating_doctor = (m_doc.group(1) or "").strip(" ,.;:-")
            if treating_doctor:
                break

    reg_patterns = [
        r"(?:reg(?:istration)?\s*(?:no|number)?|mci\s*reg(?:istration)?\s*(?:no|number)?|nmc\s*reg(?:istration)?\s*(?:no|number)?)\s*[:#\-]?\s*([A-Za-z0-9\-/\.]{4,40})",
    ]
    for pat in reg_patterns:
        m_reg = re.search(pat, src, re.I)
        if m_reg:
            doctor_registration_number = (m_reg.group(1) or "").strip(" ,.;:-")
            if doctor_registration_number:
                break

    med_lines: list[str] = []
    med_tokens = r"\b(tab|tablet|cap|capsule|inj|injection|syrup|drop|drops|iv|po|od|bd|tid|qid|hs|stat|mg|g|ml|mcg|iu|antibiotic)\b"
    in_med_section = False
    for line in lines:
        low = line.lower()
        if re.search(r"\b(treatment\s+medicines?|medications?|prescriptions?|drug\s+chart|rx)\b", low):
            in_med_section = True
            continue
        if in_med_section and re.match(r"^[A-Z][A-Z\s\/-]{6,}$", line):
            in_med_section = False
        if in_med_section or re.search(med_tokens, low):
            if len(line) <= 180:
                med_lines.append(line.strip(" -:\t"))

    med_lines = _dedup_lines(med_lines)
    medicine_used = "\n".join(med_lines[:60])

    if not detailed_conclusion:
        pieces: list[str] = []
        if diagnosis:
            pieces.append(f"Diagnosis: {diagnosis}")
        if hospital_name:
            pieces.append(f"Hospital: {hospital_name}")
        if bill_amount:
            pieces.append(f"Bill amount: {bill_amount}")
        if clinical:
            pieces.append("Clinical findings: " + "; ".join(clinical[:4]))
        if inv_lines:
            pieces.append("Investigations: " + "; ".join(inv_lines[:5]))
        if medicine_used:
            pieces.append("Medicines: " + "; ".join(med_lines[:6]))
        detailed_conclusion = " | ".join(pieces)

    return {
        "name": name,
        "diagnosis": diagnosis,
        "hospital_name": hospital_name,
        "pharmacy_name": pharmacy_name,
        "gst_number": gst_number,
        "vendor_name": pharmacy_name,
        "vendor_gstin": gst_number,
        "hospital_address": hospital_address,
        "treating_doctor": treating_doctor,
        "doctor_registration_number": doctor_registration_number,
        "medicine_used": medicine_used,
        "bill_amount": bill_amount,
        "claim_amount": bill_amount,
        "clinical_findings": "\n".join(clinical),
        "all_investigation_reports_with_values": investigations,
        "detailed_conclusion": detailed_conclusion,
    }
def _normalize_extracted_entities(raw_entities: Any, fallback_text: str = "") -> dict[str, Any]:
    entities = raw_entities if isinstance(raw_entities, dict) else {}
    normalized = dict(entities)

    name_value = _find_entity_value(
        entities,
        ["name", "patient_name", "patient", "insured", "beneficiary", "policy_holder_name"],
    )
    diagnosis_value = _find_entity_value(
        entities,
        ["diagnosis", "final_diagnosis", "provisional_diagnosis", "diagnoses"],
    )
    clinical_findings_value = _find_entity_value(
        entities,
        ["clinical_findings", "finding", "findings", "major_diagnostic_finding", "hospital_finding", "summary"],
    )
    investigations_value = _find_entity_value(
        entities,
        [
            "all_investigation_reports_with_values",
            "all_investigation_reports",
            "investigation_reports",
            "investigations",
            "lab_results",
            "test_results",
        ],
    )
    detailed_conclusion_value = _find_entity_value(
        entities,
        ["detailed_conclusion", "conclusion", "recommendation", "decision", "rationale"],
    )
    hospital_name_value = _find_entity_value(
        entities,
        ["hospital_name", "hospital", "treating_hospital", "provider_hospital", "hospital_city_name"],
    )
    hospital_address_value = _find_entity_value(
        entities,
        ["hospital_address", "hospital_addr", "address_of_hospital", "hospital_full_address", "provider_hospital_address"],
    )
    bill_amount_value = _find_entity_value(
        entities,
        ["bill_amount", "claim_amount", "claimed_amount", "amount_claimed", "total_bill", "invoice_amount", "net_payable"],
    )
    pharmacy_name_value = _find_entity_value(
        entities,
        ["pharmacy_name", "vendor_name", "chemist_name", "medical_store_name", "shop_name"],
    )
    gst_number_value = _find_entity_value(
        entities,
        ["gst_number", "gst_no", "gstin", "vendor_gstin", "vendor_gst_number", "pharmacy_gstin", "hospital_gstin"],
    )

    treating_doctor_value = _find_entity_value(
        entities,
        ["treating_doctor", "treating_doctor_name", "doctor_name", "attending_doctor", "consultant_doctor", "admit_doctor"],
    )
    doctor_registration_value = _find_entity_value(
        entities,
        ["doctor_registration_number", "treating_doctor_registration_number", "registration_no", "registration_number", "mci_reg_no", "nmc_reg_no"],
    )
    medicine_used_value = _find_entity_value(
        entities,
        ["medicine_used", "medicines", "medications", "treatment_medicines", "drug_chart", "prescription", "rx"],
    )
    name_text = _to_text(name_value)
    diagnosis_text = _to_text(diagnosis_value)
    clinical_findings_text = _clean_clinical_findings_text(clinical_findings_value)
    investigation_rows = _normalize_investigation_reports(investigations_value)
    detailed_conclusion_text = _to_text(detailed_conclusion_value)
    hospital_name_text = _clean_hospital_name_text(hospital_name_value)
    pharmacy_name_text = _clean_pharmacy_name_text(pharmacy_name_value)
    gst_number_text = _normalize_gst_number(gst_number_value)
    hospital_address_text = _to_text(hospital_address_value)
    bill_amount_text = _normalize_amount_text(bill_amount_value)
    treating_doctor_text = _to_text(treating_doctor_value)
    doctor_registration_text = _to_text(doctor_registration_value)
    medicine_used_text = _to_text(medicine_used_value)

    heuristic = _extract_focus_fields_from_text(fallback_text)
    if not name_text:
        name_text = _to_text(heuristic.get("name"))
    if not diagnosis_text:
        diagnosis_text = _to_text(heuristic.get("diagnosis"))
    if not clinical_findings_text:
        clinical_findings_text = _clean_clinical_findings_text(heuristic.get("clinical_findings"))
    if not investigation_rows:
        investigation_rows = _normalize_investigation_reports(heuristic.get("all_investigation_reports_with_values"))
    if not detailed_conclusion_text:
        detailed_conclusion_text = _to_text(heuristic.get("detailed_conclusion")) or _to_text(fallback_text)
    if not hospital_name_text:
        hospital_name_text = _clean_hospital_name_text(heuristic.get("hospital_name"))
    if not pharmacy_name_text:
        pharmacy_name_text = _clean_pharmacy_name_text(heuristic.get("pharmacy_name"))
    if not gst_number_text:
        gst_number_text = _normalize_gst_number(heuristic.get("gst_number"))
    if not hospital_address_text:
        hospital_address_text = _to_text(heuristic.get("hospital_address"))
    if not bill_amount_text:
        bill_amount_text = _normalize_amount_text(heuristic.get("bill_amount"))
    if not treating_doctor_text:
        treating_doctor_text = _to_text(heuristic.get("treating_doctor"))
    if not doctor_registration_text:
        doctor_registration_text = _to_text(heuristic.get("doctor_registration_number"))
    if not medicine_used_text:
        medicine_used_text = _to_text(heuristic.get("medicine_used"))

    name_text = _sanitize_person_name(name_text)

    normalized["name"] = name_text
    normalized["patient_name"] = name_text or _sanitize_person_name(_to_text(entities.get("patient_name")))
    normalized["diagnosis"] = diagnosis_text
    normalized["clinical_findings"] = clinical_findings_text
    normalized["all_investigation_reports_with_values"] = investigation_rows
    normalized["all_investigation_report_lines"] = [row.get("line", "") for row in investigation_rows if row.get("line")]
    normalized["detailed_conclusion"] = detailed_conclusion_text
    normalized["hospital_name"] = hospital_name_text
    normalized["pharmacy_name"] = pharmacy_name_text
    normalized["gst_number"] = gst_number_text
    if not _to_text(normalized.get("vendor_name")) and pharmacy_name_text:
        normalized["vendor_name"] = pharmacy_name_text
    if not _normalize_gst_number(normalized.get("vendor_gstin")) and gst_number_text:
        normalized["vendor_gstin"] = gst_number_text
    normalized["hospital_address"] = hospital_address_text
    normalized["treating_doctor"] = treating_doctor_text
    normalized["doctor_registration_number"] = doctor_registration_text
    normalized["medicine_used"] = medicine_used_text
    normalized["bill_amount"] = bill_amount_text
    normalized["claim_amount"] = bill_amount_text
    normalized["focused_extraction_fields"] = [
        "name",
        "diagnosis",
        "hospital_name",
        "pharmacy_name",
        "gst_number",
        "hospital_address",
        "treating_doctor",
        "doctor_registration_number",
        "medicine_used",
        "bill_amount",
        "clinical_findings",
        "all_investigation_reports_with_values",
        "detailed_conclusion",
    ]

    # Keep output user-friendly for non-clinical documents that naturally have sparse medical fields.
    focused_has_content = bool(
        normalized.get("name")
        or normalized.get("diagnosis")
        or normalized.get("clinical_findings")
        or normalized.get("hospital_name")
        or normalized.get("pharmacy_name")
        or normalized.get("gst_number")
        or normalized.get("hospital_address")
        or normalized.get("treating_doctor")
        or normalized.get("doctor_registration_number")
        or normalized.get("medicine_used")
        or normalized.get("bill_amount")
        or normalized.get("all_investigation_reports_with_values")
        or normalized.get("detailed_conclusion")
    )
    if not focused_has_content:
        normalized["detailed_conclusion"] = "No relevant medical details extracted from this document."
    return normalized
def _extract_openai_response_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""

    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()

    output_chunks: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for out in output:
            if not isinstance(out, dict):
                continue
            content = out.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                t = item.get("text")
                if isinstance(t, str) and t.strip():
                    output_chunks.append(t.strip())
        if output_chunks:
            return "\n".join(output_chunks).strip()

    msg = (((body.get("choices") or [{}])[0]).get("message") or {}) if isinstance(body.get("choices"), list) else {}
    msg_content = msg.get("content") if isinstance(msg, dict) else ""
    if isinstance(msg_content, str):
        return msg_content.strip()
    if isinstance(msg_content, list):
        joined = []
        for item in msg_content:
            if isinstance(item, dict):
                t = item.get("text") or item.get("content")
                if isinstance(t, str) and t.strip():
                    joined.append(t.strip())
        return "\n".join(joined).strip()
    return ""


def _parse_unstructured_claim_extraction(content: str) -> dict[str, Any]:
    text = (content or "").strip()
    admission_required = "uncertain"
    confidence = 0.0
    treating_doctor = ""
    patient_name = ""
    diagnosis = ""
    hospital_name = ""
    pharmacy_name = ""
    gst_number = ""
    hospital_address = ""
    bill_amount = ""

    evidence_lines: list[str] = []
    missing_info: list[str] = []
    clinical_lines: list[str] = []
    investigation_lines: list[str] = []

    m = re.search(r"Was\s+Hospital\s+Admission\s+Medically\s+Required\?\s*(YES|NO)", text, re.I)
    if m:
        v = (m.group(1) or "").strip().upper()
        if v == "YES":
            admission_required = "yes"
            confidence = 0.88
        elif v == "NO":
            admission_required = "no"
            confidence = 0.85

    m_doctor = re.search(r"\bDR\.?\s*[A-Z][A-Za-z\.\s]{2,60}", text, re.I)
    if m_doctor:
        treating_doctor = (m_doctor.group(0) or "").strip()

    m_name = re.search(r"(?:patient\s*name|insured|beneficiary)\s*[:\-]\s*([^\n\r]{2,120})", text, re.I)
    if m_name:
        patient_name = (m_name.group(1) or "").strip()

    m_diag = re.search(r"(?:diagnosis|final\s*diagnosis|provisional\s*diagnosis)\s*[:\-]\s*([^\n\r]{2,220})", text, re.I)
    if m_diag:
        diagnosis = (m_diag.group(1) or "").strip()

    focus_fields = _extract_focus_fields_from_text(text)
    hospital_name = _to_text(focus_fields.get("hospital_name"))
    pharmacy_name = _to_text(focus_fields.get("pharmacy_name"))
    gst_number = _normalize_gst_number(focus_fields.get("gst_number"))
    hospital_address = _to_text(focus_fields.get("hospital_address"))
    bill_amount = _normalize_amount_text(focus_fields.get("bill_amount"))

    for line in re.split(r"\r\n|\r|\n", text):
        l = (line or "").strip()
        if not l:
            continue

        m_ev = re.match(r"^[\-\*\u2022]\s*(.+)$", l)
        if m_ev:
            snippet = (m_ev.group(1) or "").strip()
            if snippet:
                evidence_lines.append(snippet)
                clinical_lines.append(snippet)
            continue

        if re.search(r"\b(Hb|WBC|RBC|Platelet|MCV|MCH|MCHC|RDW|BUN|Creatinine|glucose|sodium|potassium|ALT|AST)\b", l, re.I):
            investigation_lines.append(l)
            evidence_lines.append(l)
            continue

        if re.search(r"\b(LOW|HIGH|ELEVATED|DECREASED|ABNORMAL|DERANGED|THROMBOCYTOPENIA|RISK|PRETERM|LOW BIRTH WEIGHT)\b", l, re.I):
            evidence_lines.append(l)
            clinical_lines.append(l)
            continue

        if re.search(r"not clearly detailed|not clearly|insufficient|missing", l, re.I):
            missing_info.append(l)

    def _dedup(lines: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in lines:
            key = item.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(key)
        return out

    dedup_evidence = _dedup(evidence_lines)
    dedup_missing = _dedup(missing_info)
    dedup_clinical = _dedup(clinical_lines)
    dedup_investigations = _dedup(investigation_lines)

    extracted = {
        "admission_required": admission_required,
        "treating_doctor": treating_doctor,
        "name": patient_name,
        "patient_name": patient_name,
        "diagnosis": diagnosis,
        "hospital_name": hospital_name,
        "pharmacy_name": pharmacy_name,
        "gst_number": gst_number,
        "vendor_name": pharmacy_name,
        "vendor_gstin": gst_number,
        "hospital_address": hospital_address,
        "bill_amount": bill_amount,
        "claim_amount": bill_amount,
        "clinical_findings": "\n".join(dedup_clinical),
        "all_investigation_reports_with_values": [{"line": line} for line in dedup_investigations],
        "detailed_conclusion": text,
        "rationale": text,
        "missing_information": dedup_missing,
    }

    extracted = _normalize_extracted_entities(extracted, text)

    return {
        "extracted_entities": extracted,
        "evidence_refs": [
            {"type": "text", "field": "evidence", "snippet": line}
            for line in dedup_evidence
        ],
        "confidence": confidence,
    }

def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_pdf_text(payload: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(payload))
    except Exception:
        return ""

    chunks: list[str] = []
    for page in reader.pages:
        text = (page.extract_text() or "").strip()
        if text:
            chunks.append(text)

    return "\n\n".join(chunks)


def _extract_text_with_ocr_space(document_name: str, mime_type: str, payload: bytes) -> str:
    if not settings.ocr_space_api_key:
        return ""

    def run_ocr(engine: int) -> tuple[str, str]:
        files = {
            "file": (document_name or "document", payload, mime_type or "application/octet-stream"),
        }
        data = {
            "apikey": settings.ocr_space_api_key,
            "language": "eng",
            "isOverlayRequired": "false",
            "OCREngine": str(engine),
            "scale": "true",
        }

        try:
            with httpx.Client(timeout=90.0) as client:
                response = client.post(settings.ocr_space_endpoint, files=files, data=data)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ExtractionProcessingError(f"OCR request failed: {exc}") from exc

        body = response.json()
        if body.get("IsErroredOnProcessing"):
            error_msg = body.get("ErrorMessage") or body.get("ErrorDetails") or "OCR processing error"
            return "", str(error_msg)

        parsed_results = body.get("ParsedResults") or []
        text_parts: list[str] = []
        for item in parsed_results:
            parsed_text = (item or {}).get("ParsedText")
            if parsed_text:
                text_parts.append(parsed_text)

        return "\n".join(text_parts).strip(), ""

    primary_engine = settings.ocr_space_engine or 2
    text, error_message = run_ocr(primary_engine)
    if text:
        return text

    if primary_engine != 1 and "Engine 2" in error_message:
        fallback_text, fallback_error = run_ocr(1)
        if fallback_text:
            return fallback_text
        error_message = fallback_error or error_message

    if error_message:
        raise ExtractionProcessingError(f"OCR processing failed: {error_message}")
    return ""


def _looks_like_text_mime(mime_type: str) -> bool:
    mt = (mime_type or "").lower()
    if mt.startswith("text/"):
        return True
    return mt in {
        "application/json",
        "application/xml",
        "text/xml",
        "application/csv",
        "text/csv",
    }


def _normalize_document_text(document_name: str, mime_type: str, payload: bytes) -> tuple[str, str]:
    lower_name = (document_name or "").lower()
    lower_mime = (mime_type or "").lower()
    is_pdf = lower_mime == "application/pdf" or lower_name.endswith(".pdf")
    is_image = lower_mime.startswith("image/") or lower_name.endswith(
        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")
    )

    # Prefer OCR Space for image/PDF documents so OCR_SPACE_ENDPOINT is actively used.
    if is_image or is_pdf:
        try:
            ocr_text = _extract_text_with_ocr_space(document_name, mime_type, payload).strip()
            if ocr_text:
                return ocr_text, "ocr-space"
        except ExtractionProcessingError:
            pass

    if _looks_like_text_mime(lower_mime) or lower_name.endswith((".txt", ".csv", ".json", ".xml")):
        text = _decode_text(payload).strip()
        if text:
            return text, "text-decode"

    if is_pdf:
        pdf_text = _extract_pdf_text(payload).strip()
        if pdf_text:
            return pdf_text, "pdf-text"

    fallback = _decode_text(payload).strip()
    if fallback:
        return fallback, "fallback-decode"

    return "", "none"


def _build_textract_client():
    region_name = (
        str(settings.aws_textract_region or "").strip()
        or str(settings.s3_region or "").strip()
        or "ap-south-1"
    )
    access_key = str(settings.aws_textract_access_key_id or settings.s3_access_key or "").strip()
    secret_key = str(settings.aws_textract_secret_access_key or settings.s3_secret_key or "").strip()
    session_token = str(settings.aws_textract_session_token or "").strip()
    endpoint_url = str(settings.aws_textract_endpoint_url or "").strip()

    kwargs: dict[str, Any] = {"region_name": region_name}
    if access_key and secret_key:
        kwargs["aws_access_key_id"] = access_key
        kwargs["aws_secret_access_key"] = secret_key
    if session_token:
        kwargs["aws_session_token"] = session_token
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url

    try:
        return boto3.client("textract", **kwargs)
    except Exception as exc:
        raise ExtractionConfigError(f"AWS Textract client configuration failed: {exc}") from exc


def _textract_requires_async(document_name: str, mime_type: str) -> bool:
    lower_name = str(document_name or "").strip().lower()
    lower_mime = str(mime_type or "").strip().lower()
    return (
        lower_name.endswith(".pdf")
        or lower_name.endswith(".tif")
        or lower_name.endswith(".tiff")
        or lower_mime == "application/pdf"
        or lower_mime in {"image/tiff", "image/tif"}
    )


def _collect_textract_lines(response: dict[str, Any]) -> list[str]:
    blocks = response.get("Blocks") if isinstance(response, dict) else []
    lines: list[str] = []
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("BlockType") or "").upper()
            text_val = str(block.get("Text") or "").strip()
            if not text_val:
                continue
            if block_type == "LINE":
                lines.append(text_val)

    if not lines and isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict):
                continue
            text_val = str(block.get("Text") or "").strip()
            if text_val:
                lines.append(text_val)
    return lines


def _extract_text_with_textract_async_s3(
    client: Any,
    *,
    bucket: str,
    key: str,
    document_name: str,
) -> dict[str, Any]:
    try:
        start = client.start_document_text_detection(
            DocumentLocation={"S3Object": {"Bucket": bucket, "Name": key}}
        )
    except (ClientError, BotoCoreError) as exc:
        raise ExtractionProcessingError(f"AWS Textract async start failed: {exc}") from exc
    except Exception as exc:
        raise ExtractionProcessingError(f"AWS Textract async start failed: {exc}") from exc

    job_id = str(start.get("JobId") or "").strip()
    if not job_id:
        raise ExtractionProcessingError(
            f"AWS Textract async start failed: missing JobId for {document_name or 'document'}"
        )

    deadline = time.time() + 240.0
    next_token: str | None = None
    all_blocks: list[Any] = []
    pages = 0
    last_status = ""

    while True:
        req: dict[str, Any] = {"JobId": job_id}
        if next_token:
            req["NextToken"] = next_token
        try:
            part = client.get_document_text_detection(**req)
        except (ClientError, BotoCoreError) as exc:
            raise ExtractionProcessingError(f"AWS Textract async polling failed: {exc}") from exc
        except Exception as exc:
            raise ExtractionProcessingError(f"AWS Textract async polling failed: {exc}") from exc

        status = str(part.get("JobStatus") or "").upper()
        last_status = status or last_status
        if status == "SUCCEEDED":
            part_blocks = part.get("Blocks")
            if isinstance(part_blocks, list) and part_blocks:
                all_blocks.extend(part_blocks)
            doc_meta = part.get("DocumentMetadata")
            if isinstance(doc_meta, dict):
                try:
                    pages = max(pages, int(doc_meta.get("Pages") or 0))
                except Exception:
                    pass
            token_val = part.get("NextToken")
            next_token = str(token_val).strip() if token_val else None
            if next_token:
                continue
            return {
                "JobId": job_id,
                "JobStatus": status,
                "DocumentMetadata": {"Pages": pages},
                "Blocks": all_blocks,
                "Mode": "async_s3",
            }

        if status in {"FAILED", "PARTIAL_SUCCESS"}:
            status_message = str(part.get("StatusMessage") or "").strip()
            detail = f"status={status}"
            if status_message:
                detail += f", message={status_message}"
            raise ExtractionProcessingError(f"AWS Textract async extraction failed: {detail}")

        if time.time() >= deadline:
            raise ExtractionProcessingError(
                f"AWS Textract async extraction timed out for {document_name or 'document'} (last status: {last_status or 'UNKNOWN'})"
            )
        time.sleep(1.5)


def _extract_text_with_textract(
    document_name: str,
    mime_type: str,
    payload: bytes,
    *,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> tuple[str, dict[str, Any]]:
    if not payload:
        raise ExtractionProcessingError("AWS Textract extraction failed: empty file payload")

    client = _build_textract_client()
    response: dict[str, Any]
    bucket = str(s3_bucket or settings.s3_bucket or "").strip()
    key = str(storage_key or "").strip()
    if _textract_requires_async(document_name, mime_type):
        if not key:
            raise ExtractionProcessingError(
                "AWS Textract async extraction requires storage key for PDF/TIFF documents."
            )
        if not bucket:
            raise ExtractionProcessingError(
                "AWS Textract async extraction requires S3 bucket configuration."
            )
        response = _extract_text_with_textract_async_s3(
            client,
            bucket=bucket,
            key=key,
            document_name=document_name,
        )
    else:
        try:
            response = client.detect_document_text(Document={"Bytes": payload})
            if isinstance(response, dict):
                response["Mode"] = "sync_bytes"
        except (ClientError, BotoCoreError) as exc:
            raise ExtractionProcessingError(f"AWS Textract extraction failed: {exc}") from exc
        except Exception as exc:
            raise ExtractionProcessingError(f"AWS Textract extraction failed: {exc}") from exc

    lines = _collect_textract_lines(response if isinstance(response, dict) else {})

    extracted_text = "\n".join(lines).strip()
    if not extracted_text:
        raise ExtractionProcessingError(
            f"AWS Textract returned no text for document: {document_name or 'document'}"
        )

    return extracted_text, response if isinstance(response, dict) else {}

def _extract_aws_textract(
    document_name: str,
    mime_type: str,
    payload: bytes,
    *,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    extracted_text, raw_textract = _extract_text_with_textract(
        document_name,
        mime_type,
        payload,
        storage_key=storage_key,
        s3_bucket=s3_bucket,
    )
    preview = extracted_text[:2000]

    claim_match = re.search(r"(?:claim\s*(?:id|number)?\s*[:#-]?\s*)([A-Za-z0-9-_/]+)", extracted_text, re.I)
    patient_match = re.search(r"(?:patient\s*(?:name)?\s*[:#-]?\s*)([A-Za-z .'-]{3,80})", extracted_text, re.I)
    diagnosis_match = re.search(r"(?:diagnosis\s*[:#-]?\s*)([^\n\r]{3,140})", extracted_text, re.I)

    entities = {
        "document_name": document_name,
        "mime_type": mime_type,
        "text_source": "aws-textract",
        "claim_reference": claim_match.group(1).strip() if claim_match else None,
        "patient_name": patient_match.group(1).strip() if patient_match else None,
        "diagnosis": diagnosis_match.group(1).strip() if diagnosis_match else None,
        "text_preview": preview,
    }

    evidence: list[dict[str, Any]] = []
    if claim_match:
        evidence.append({"type": "regex", "field": "claim_reference", "snippet": claim_match.group(0)})
    if patient_match:
        evidence.append({"type": "regex", "field": "patient_name", "snippet": patient_match.group(0)})
    if diagnosis_match:
        evidence.append({"type": "regex", "field": "diagnosis", "snippet": diagnosis_match.group(0)})

    line_snippets = [ln.strip() for ln in extracted_text.splitlines() if (ln or "").strip()]
    for line in line_snippets[:8]:
        evidence.append({"type": "textract_line", "field": "line", "snippet": line})

    normalized_entities = _normalize_extracted_entities(entities, extracted_text or preview)
    if _looks_like_kyc_document(document_name, extracted_text):
        normalized_entities = _apply_kyc_exclusion(
            normalized_entities,
            "KYC/identity document excluded from clinical extraction.",
        )
        evidence = [
            {
                "type": "policy",
                "field": "excluded_document",
                "snippet": "KYC/identity document excluded from clinical extraction.",
            }
        ]

    blocks = raw_textract.get("Blocks") if isinstance(raw_textract, dict) else []
    doc_meta = raw_textract.get("DocumentMetadata") if isinstance(raw_textract, dict) else {}
    pages = 0
    if isinstance(doc_meta, dict):
        try:
            pages = int(doc_meta.get("Pages") or 0)
        except Exception:
            pages = 0

    return {
        "provider": ExtractionProvider.aws_textract.value,
        "model_name": (
            "aws-textract-start-document-text-detection"
            if str(raw_textract.get("Mode") or "").strip().lower() == "async_s3"
            else "aws-textract-detect-document-text"
        ),
        "extraction_version": "textract-v1",
        "extracted_entities": normalized_entities,
        "evidence_refs": evidence,
        "confidence": 0.72,
        "raw_response": {
            "source": "aws-textract",
            "mode": str(raw_textract.get("Mode") or "sync_bytes"),
            "pages": pages,
            "blocks": len(blocks) if isinstance(blocks, list) else 0,
            "sample_lines": line_snippets[:25],
        },
    }
def _extract_local(document_name: str, mime_type: str, payload: bytes) -> dict[str, Any]:
    text, text_source = _normalize_document_text(document_name, mime_type, payload)
    preview = text[:2000]

    claim_match = re.search(r"(?:claim\s*(?:id|number)?\s*[:#-]?\s*)([A-Za-z0-9-_/]+)", text, re.I)
    patient_match = re.search(r"(?:patient\s*(?:name)?\s*[:#-]?\s*)([A-Za-z .'-]{3,80})", text, re.I)
    diagnosis_match = re.search(r"(?:diagnosis\s*[:#-]?\s*)([^\n\r]{3,120})", text, re.I)

    entities = {
        "document_name": document_name,
        "mime_type": mime_type,
        "text_source": text_source,
        "claim_reference": claim_match.group(1).strip() if claim_match else None,
        "patient_name": patient_match.group(1).strip() if patient_match else None,
        "diagnosis": diagnosis_match.group(1).strip() if diagnosis_match else None,
        "text_preview": preview,
    }

    evidence = []
    if claim_match:
        evidence.append({"type": "regex", "field": "claim_reference", "snippet": claim_match.group(0)})
    if patient_match:
        evidence.append({"type": "regex", "field": "patient_name", "snippet": patient_match.group(0)})
    if diagnosis_match:
        evidence.append({"type": "regex", "field": "diagnosis", "snippet": diagnosis_match.group(0)})

    normalized_entities = _normalize_extracted_entities(entities, text or preview)
    if _looks_like_kyc_document(document_name, text):
        normalized_entities = _apply_kyc_exclusion(
            normalized_entities,
            "KYC/identity document excluded from clinical extraction.",
        )
        evidence = [{"type": "policy", "field": "excluded_document", "snippet": "KYC/identity document excluded from clinical extraction."}]

    return {
        "provider": ExtractionProvider.local.value,
        "model_name": "local-rule-v1",
        "extraction_version": "local-v1",
        "extracted_entities": normalized_entities,
        "evidence_refs": evidence,
        "confidence": 0.45,
    }


def _extract_openai(
    document_name: str,
    mime_type: str,
    payload: bytes,
    *,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    if not settings.openai_api_key:
        raise ExtractionConfigError("OPENAI_API_KEY is not configured")

    text, text_source = _normalize_document_text(document_name, mime_type, payload)
    text_preview = text[:14000]

    if _looks_like_kyc_document(document_name, text_preview):
        return {
            "provider": ExtractionProvider.openai.value,
            "model_name": "openai-skip-kyc",
            "extraction_version": "openai-v1",
            "extracted_entities": _apply_kyc_exclusion(
                {
                    "document_name": document_name,
                    "mime_type": mime_type,
                    "text_source": text_source,
                },
                "KYC/identity document excluded from clinical extraction.",
            ),
            "evidence_refs": [
                {
                    "type": "policy",
                    "field": "excluded_document",
                    "snippet": "KYC/identity document excluded from clinical extraction.",
                }
            ],
            "confidence": 1.0,
            "raw_response": {
                "source": "policy",
                "used_model": "none",
                "models_tried": [],
                "responses_errors": [],
                "chat_errors": [],
                "model_output_text": "Skipped OpenAI extraction for KYC/identity document.",
            },
        }

    safe_name = (document_name or "document").strip() or "document"
    safe_mime = (mime_type or "application/octet-stream").strip().lower() or "application/octet-stream"
    lower_name = safe_name.lower()
    is_image = safe_mime.startswith("image/") or lower_name.endswith(
        (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif")
    )

    user_prompt = (
        "Extract structured data from this medical claim document with strict segregation. Return strict JSON only.\n\n"
        "JSON schema:\n"
        "{\n"
        '  "extracted_entities": {\n'
        '    "name": "",\n'
        '    "hospital_name": "",\n'
        '    "pharmacy_name": "",\n'
        '    "gst_number": "",\n'
        '    "treating_doctor": "",\n'
        '    "doctor_registration_number": "",\n'
        '    "admission_date": "",\n'
        '    "discharge_date": "",\n'
        '    "diagnosis": "",\n'
        '    "chief_complaints_at_admission": "",\n'
        '    "major_diagnostic_finding": "",\n'
        '    "alcoholism_history": "",\n'
        '    "claim_amount": "",\n'
        '    "clinical_findings": "",\n'
        '    "all_investigation_reports_with_values": [\n'
        '      {"lab_name":"","test_name":"","value":"","unit":"","reference_range":"","flag":"","date":"","line":""}\n'
        '    ],\n'
        '    "date_wise_investigation_reports": [\n'
        '      {"date":"","details":[]}\n'
        '    ],\n'
        '    "deranged_investigation": "",\n'
        '    "medicine_used": "",\n'
        '    "admission_required": "",\n'
        '    "final_recommendation": "",\n'
        '    "conclusion": "",\n'
        '    "recommendation": "",\n'
        '    "hospital_address": "",\n'
        '    "bill_amount": "",\n'
        '    "detailed_conclusion": ""\n'
        "  },\n"
        '  "evidence_refs": [{"type":"text","field":"","snippet":""}],\n'
        '  "confidence": 0.0\n'
        "}\n\n"
        "Segregation rules (strict):\n"
        "1) Keep `chief_complaints_at_admission` only for symptom/history-at-admission text.\n"
        "2) Keep `major_diagnostic_finding` only for objective findings during admission/stay (vitals, exam, diagnostic summary), not complaint text.\n"
        "3) Keep billing/admin/address/provider identity details out of clinical fields.\n"
        "4) Capture all investigation rows with lab_name, value, unit, reference_range, flag, and date whenever available.\n"
        "5) Capture medicine lines as complete medicine entries (do not split into tiny tokens).\n"
        "6) If a value is unavailable, use empty string.\n"
        "7) Read top header separately and extract `hospital_name`, `pharmacy_name`, and `gst_number` from header identity details.\n"
        "Use attached raw file as primary source of truth. Helper text preview is only secondary context.\n\n"
        f"Document name: {safe_name}\n"
        f"MIME type: {safe_mime}\n"
        f"Text source: {text_source}\n"
    )

    user_content: list[dict[str, Any]] = [{"type": "input_text", "text": user_prompt}]
    try:
        encoded_payload = base64.b64encode(payload).decode("ascii")
        data_uri = f"data:{safe_mime};base64,{encoded_payload}"
        if is_image:
            user_content.append({"type": "input_image", "image_url": data_uri})
        else:
            user_content.append({"type": "input_file", "filename": safe_name, "file_data": data_uri})
    except Exception:
        user_content.append(
            {
                "type": "input_text",
                "text": "Raw file attachment failed to encode; continue with text preview.",
            }
        )

    if text_preview:
        user_content.append(
            {
                "type": "input_text",
                "text": "Helper text preview (truncated to 14k chars):\n" + text_preview,
            }
        )

    base_url = settings.openai_base_url.rstrip("/") if settings.openai_base_url else "https://api.openai.com/v1"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    # Force single model for extraction to avoid fallback bursts and keep consistency.
    configured_model = "gpt-4o-mini"
    model_candidates: list[str] = [configured_model]

    model_name = configured_model
    parsed: dict[str, Any] | None = None
    model_output_text = ""
    response_source = ""
    last_openai_body: dict[str, Any] | None = None
    responses_errors: list[str] = []
    chat_errors: list[str] = []
    used_model = configured_model
    rate_limited = False

    def _status_detail(exc: httpx.HTTPStatusError) -> tuple[int | str, str]:
        status = exc.response.status_code if exc.response is not None else "unknown"
        detail = ""
        try:
            detail = (exc.response.text or "")[:800] if exc.response is not None else ""
        except Exception:
            detail = ""
        return status, detail

    def _looks_like_model_not_found(status: int | str, detail: str) -> bool:
        d = (detail or "").lower()
        if status == 404 and ("model" in d or "model_not_found" in d):
            return True
        if status == 400 and ("model_not_found" in d or "does not exist" in d or "do not have access" in d):
            return True
        return False

    responses_url = f"{base_url}/responses"
    for candidate in model_candidates:
        responses_payload = {
            "model": candidate,
            "input": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "You are a medical-claim extraction service. Return strict JSON only with extracted_entities, evidence_refs, confidence.",
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": user_content,
                },
            ],
        }
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(responses_url, headers=headers, json=responses_payload)
                response.raise_for_status()
            body = response.json()
            if isinstance(body, dict):
                last_openai_body = body
                response_source = "responses"
            model_name = str(body.get("model") or candidate)
            used_model = candidate
            model_output_text = _extract_openai_response_text(body)
            if model_output_text:
                parsed = _parse_json_payload(model_output_text)
            if parsed is not None:
                break
        except httpx.HTTPStatusError as exc:
            status, detail = _status_detail(exc)
            responses_errors.append(f"{candidate} => HTTP {status}: {detail}")
            if status == 429:
                rate_limited = True
            if _looks_like_model_not_found(status, detail):
                continue
        except Exception as exc:
            responses_errors.append(f"{candidate} => {exc}")

    if parsed is None:
        fallback_prompt = user_prompt + "\nDocument text preview:\n" + (text_preview or "(none)")
        url = f"{base_url}/chat/completions"
        for candidate in model_candidates:
            request_payload = {
                "model": candidate,
                "response_format": {"type": "json_object"},
                "messages": [
                    {
                        "role": "system",
                        "content": "You are a medical-claim extraction service. Return strict JSON only with extracted_entities, evidence_refs, confidence.",
                    },
                    {"role": "user", "content": fallback_prompt},
                ],
            }
            try:
                with httpx.Client(timeout=90.0) as client:
                    response = client.post(url, headers=headers, json=request_payload)
                    response.raise_for_status()
                body = response.json()
                if isinstance(body, dict):
                    last_openai_body = body
                    response_source = "chat.completions"
                model_name = str(body.get("model") or candidate)
                used_model = candidate
                model_output_text = _extract_openai_response_text(body)
                parsed = _parse_json_payload(model_output_text) if model_output_text else None
                if parsed is not None:
                    break
            except httpx.HTTPStatusError as exc:
                status, detail = _status_detail(exc)
                chat_errors.append(f"{candidate} => HTTP {status}: {detail}")
                if status == 429:
                    rate_limited = True
                if _looks_like_model_not_found(status, detail):
                    continue
            except Exception as exc:
                chat_errors.append(f"{candidate} => {exc}")

    if parsed is None and model_output_text:
        parsed = _parse_unstructured_claim_extraction(model_output_text)

    if parsed is None and rate_limited:
        try:
            textract_result = _extract_aws_textract(
                document_name,
                mime_type,
                payload,
                storage_key=storage_key,
                s3_bucket=s3_bucket,
            )
            raw = textract_result.get("raw_response")
            if not isinstance(raw, dict):
                raw = {}
            raw["openai_rate_limit_fallback"] = {
                "used_fallback_provider": "aws_textract",
                "used_model": used_model,
                "models_tried": model_candidates,
                "responses_errors": responses_errors[:3],
                "chat_errors": chat_errors[:3],
            }
            textract_result["raw_response"] = raw
            return textract_result
        except (ExtractionConfigError, ExtractionProcessingError):
            local_result = _extract_local(document_name, mime_type, payload)
            raw = local_result.get("raw_response")
            if not isinstance(raw, dict):
                raw = {}
            raw["openai_rate_limit_fallback"] = {
                "used_fallback_provider": "local",
                "used_model": used_model,
                "models_tried": model_candidates,
                "responses_errors": responses_errors[:3],
                "chat_errors": chat_errors[:3],
            }
            local_result["raw_response"] = raw
            return local_result

    if parsed is None:
        raise ExtractionProcessingError(
            "OpenAI extraction failed. "
            f"models_tried={model_candidates}; "
            f"responses_errors={responses_errors[:3] or ['none']}; "
            f"chat_errors={chat_errors[:3] or ['none']}"
        )

    entities = parsed.get("extracted_entities", {}) if isinstance(parsed, dict) else {}
    evidence = _normalize_evidence_refs(parsed.get("evidence_refs", []) if isinstance(parsed, dict) else [])
    confidence = parsed.get("confidence") if isinstance(parsed, dict) else None

    entities = _normalize_extracted_entities(entities, text or model_output_text)

    if confidence is not None:
        try:
            confidence = float(confidence)
            if math.isnan(confidence) or math.isinf(confidence):
                confidence = None
            else:
                if confidence > 1.0 and confidence <= 100.0:
                    confidence = confidence / 100.0
                confidence = max(0.0, min(1.0, confidence))
        except (TypeError, ValueError):
            confidence = None

    entities.setdefault("text_source", text_source)
    raw_response: dict[str, Any] = {
        "source": response_source or "unknown",
        "used_model": used_model,
        "models_tried": model_candidates,
        "responses_errors": responses_errors,
        "chat_errors": chat_errors,
        "model_output_text": model_output_text,
    }
    if isinstance(last_openai_body, dict):
        raw_response["response_json"] = last_openai_body

    return {
        "provider": ExtractionProvider.openai.value,
        "model_name": model_name,
        "extraction_version": "openai-v1",
        "extracted_entities": entities,
        "evidence_refs": evidence,
        "confidence": confidence,
        "raw_response": raw_response,
    }

def run_extraction(
    provider: ExtractionProvider,
    document_name: str,
    mime_type: str,
    payload: bytes,
    *,
    storage_key: str | None = None,
    s3_bucket: str | None = None,
) -> dict[str, Any]:
    if provider == ExtractionProvider.local:
        return _extract_local(document_name, mime_type, payload)
    if provider == ExtractionProvider.openai:
        return _extract_openai(
            document_name,
            mime_type,
            payload,
            storage_key=storage_key,
            s3_bucket=s3_bucket,
        )
    if provider == ExtractionProvider.aws_textract:
        return _extract_aws_textract(
            document_name,
            mime_type,
            payload,
            storage_key=storage_key,
            s3_bucket=s3_bucket,
        )

    if settings.openai_api_key:
        try:
            return _extract_openai(
                document_name,
                mime_type,
                payload,
                storage_key=storage_key,
                s3_bucket=s3_bucket,
            )
        except (ExtractionConfigError, ExtractionProcessingError):
            pass

    try:
        return _extract_aws_textract(
            document_name,
            mime_type,
            payload,
            storage_key=storage_key,
            s3_bucket=s3_bucket,
        )
    except (ExtractionConfigError, ExtractionProcessingError):
        pass

    return _extract_local(document_name, mime_type, payload)



























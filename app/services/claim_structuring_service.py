import json
import logging
import re
from collections import Counter
from difflib import SequenceMatcher
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_logger = logging.getLogger(__name__)

from app.core.config import settings


FIELD_KEYS: list[str] = [
    "company_name",
    "claim_type",
    "insured_name",
    "hospital_name",
    "treating_doctor",
    "treating_doctor_registration_number",
    "doa",
    "dod",
    "diagnosis",
    "complaints",
    "findings",
    "investigation_finding_in_details",
    "medicine_used",
    "high_end_antibiotic_for_rejection",
    "deranged_investigation",
    "claim_amount",
    "conclusion",
    "recommendation",
]

STRICT_RULE_BASED_MODE = True

_EMPTY_TEXT_MARKERS = {"", "-", "na", "n/a", "none", "nil", "null", "unknown", "notavailable"}
_HOSPITAL_REQUIRED_TOKENS = (
    "hospital",
    "clinic",
    "centre",
    "center",
    "medical",
    "health",
    "nursing",
    "diagnostic",
    "multispeciality",
    "speciality",
    "care",
    "institute",
    "sanatorium",
    "polyclinic",
)
_NOISY_LABEL_TOKENS = (
    "qualification",
    "registration",
    "patient name",
    "policy number",
    "claim form",
    "have you been",
    "hospitalized in the last",
    "date of admission",
    "date of discharge",
    "address",
)
_FILE_ARTIFACT_EXTENSIONS = ("pdf", "jpg", "jpeg", "png", "tif", "tiff", "webp", "bmp")

HIGH_END_ANTIBIOTICS = [
    "meropenem",
    "imipenem",
    "ertapenem",
    "doripenem",
    "colistin",
    "polymyxin b",
    "tigecycline",
    "linezolid",
    "vancomycin",
    "teicoplanin",
    "daptomycin",
    "piperacillin tazobactam",
    "cefoperazone sulbactam",
    "cefepime tazobactam",
]

_RXNAV_BASE_URL = "https://rxnav.nlm.nih.gov/REST"
_DRUG_API_HIGH_END_CACHE: dict[str, list[str]] = {}


class ClaimStructuringError(Exception):
    pass


class ClaimStructuredDataNotFoundError(ClaimStructuringError):
    pass


def _txt(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join([_txt(v) for v in value if _txt(v)])
    if isinstance(value, dict):
        parts: list[str] = []
        for k, v in value.items():
            t = _txt(v)
            if t:
                parts.append(f"{k}: {t}")
        return " | ".join(parts)
    return str(value).strip()


def _safe_json(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except json.JSONDecodeError:
            return default
    return default


def _looks_like_insurance_history_artifact(value: Any) -> bool:
    low = _normalize_space_text(value).lower()
    if not low:
        return False
    if re.search(r"\bpreviously\s+covered\s+by\s+any\s+other\s+mediclaim\b", low):
        return True
    if re.search(r"\bself\s+inflicted\s+road\s+traffic\s+accident\b", low):
        return True
    if re.search(r"\bsubstance\s+abuse\s*/?\s*alcohol\s+consumption\b", low):
        return True
    if re.search(r"\bdetails\s+of\s+insurance\s+history\b", low) and not re.search(
        r"\b(?:pregnancy|labou?r|lscs|caesarean|cesarean|fetus|foetus|uterine|pv\s*-?\s*water|abdomen|delivery)\b",
        low,
    ):
        return True
    return False


def _sanitize_entities_for_structuring(entities: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(entities or {})
    clinical_context = _normalize_space_text(
        "\n".join(
            _txt(cleaned.get(key))
            for key in (
                "diagnosis",
                "chief_complaints_at_admission",
                "chief_complaints",
                "clinical_findings",
                "major_diagnostic_finding",
                "procedure",
                "treatment",
            )
        )
    ).lower()
    maternity_context = bool(
        re.search(r"\b(?:pregnancy|labou?r|lscs|caesarean|cesarean|fetus|foetus|uterine|pv\s*-?\s*water|delivery)\b", clinical_context)
    )
    for key in (
        "diagnosis",
        "chief_complaints_at_admission",
        "chief_complaints",
        "chief_complaint",
        "presenting_complaints",
        "complaints",
        "clinical_findings",
        "major_diagnostic_finding",
        "detailed_conclusion",
    ):
        if key in cleaned and _looks_like_insurance_history_artifact(cleaned.get(key)) and not maternity_context:
            cleaned[key] = ""
    return cleaned


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _txt(value).lower())


def _normalize_space_text(value: Any) -> str:
    return re.sub(r"\s+", " ", _txt(value)).strip()


def _looks_like_file_artifact_text(value: Any) -> bool:
    text_value = _normalize_space_text(value)
    if not text_value:
        return False
    low = text_value.lower()

    if (low.startswith("http://") or low.startswith("https://")) and (
        "/documents" in low or any(f".{ext}" in low for ext in _FILE_ARTIFACT_EXTENSIONS)
    ):
        return True

    if re.search(
        r"\b[a-z0-9][a-z0-9_.-]{2,}\.(?:pdf|jpg|jpeg|png|tif|tiff|webp|bmp)\b",
        low,
    ):
        if len(low.split()) <= 4:
            return True

    if re.search(r"\bclaim_[0-9a-f]{6,}", low) and re.search(
        r"\.(?:pdf|jpg|jpeg|png|tif|tiff|webp|bmp)\b",
        low,
    ):
        return True

    return False


def _is_empty_marker(value: Any) -> bool:
    normalized = _norm_key(value)
    return normalized in _EMPTY_TEXT_MARKERS


def _has_noisy_label_tokens(value: Any) -> bool:
    low = _normalize_space_text(value).lower()
    if not low:
        return False
    return any(token in low for token in _NOISY_LABEL_TOKENS)


def _looks_like_clean_hospital_name(value: Any) -> bool:
    hospital = _normalize_space_text(value)
    if not hospital or len(hospital) < 4 or len(hospital) > 180:
        return False
    if _is_empty_marker(hospital) or _has_noisy_label_tokens(hospital):
        return False
    if not re.search(r"[A-Za-z]", hospital):
        return False
    low = hospital.lower()
    return any(token in low for token in _HOSPITAL_REQUIRED_TOKENS)


def _clean_hospital_name(value: Any) -> str:
    hospital = _normalize_space_text(value)
    if not hospital or _is_empty_marker(hospital):
        return ""
    m = re.search(
        r"([A-Za-z][A-Za-z .&'\-]{2,90}\b(?:Hospital|Clinic|Nursing Home|Medical Centre|Medical Center|Research Centre|Research Center))",
        hospital,
        re.I,
    )
    if m:
        return _normalize_space_text(m.group(1))
    low = hospital.lower()
    token_hits = len(re.findall(r"\b(?:road|rd|street|st|lane|ln|nagar|colony|city|district|state|pin|pincode|zip|plot|floor|building|plaza|near|opp|opposite|shop|apartment|apt|flat|unit|society|chsl|complex|tower|wing|sector|block|east|west|north|south|taluka|tehsil)\b", low))
    if re.search(r"\b(?:hospital\s*address|full\s*postal\s*address|address\s*of\s*hospital|hospital\s*addr(?:ess)?)\b", low):
        return ""
    if token_hits >= 2 and (re.search(r"\d{3,}", low) or re.search(r"\b(?:pin|pincode|zip)\b", low)):
        return ""
    if token_hits >= 1 and low.count(",") >= 2 and re.search(r"\b\d{6}\b", low):
        return ""
    return hospital


def _clean_doctor_name(value: Any) -> str:
    doctor = _normalize_space_text(value)
    if not doctor:
        return ""
    doctor = re.sub(r"^(?:dr\.?|doctor)\s+", "", doctor, flags=re.I).strip()
    low = doctor.lower()
    if _is_empty_marker(doctor) or _has_noisy_label_tokens(doctor):
        return ""
    if len(doctor) < 4 or len(doctor) > 120:
        return ""
    if not re.search(r"[A-Za-z]", doctor):
        return ""
    if re.search(r"\d", doctor):
        return ""
    if any(token in low for token in ["hospital", "policy", "claim", "patient", "insured", "beneficiary", "admission", "discharge"]):
        return ""
    parts = [p for p in re.split(r"\s+", doctor) if p]
    if len(parts) < 2:
        return ""
    return doctor


def _clean_registration_number(value: Any) -> tuple[str, str]:
    reg = _normalize_space_text(value)
    if not reg:
        return "", ""
    reg = re.sub(
        r"^(?:reg(?:istration)?\s*(?:no|number)?|mci\s*reg(?:istration)?\s*(?:no|number)?|nmc\s*reg(?:istration)?\s*(?:no|number)?)\s*[:#-]?\s*",
        "",
        reg,
        flags=re.I,
    ).strip(" ,.;:-")
    if re.fullmatch(r"\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}", reg) or re.fullmatch(
        r"\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}",
        reg,
    ):
        return "", ""
    reg_norm = _norm_key(reg).upper()
    if not reg_norm:
        return "", ""
    if len(reg_norm) < 4 or len(reg_norm) > 30:
        return "", ""
    if not re.search(r"\d", reg_norm):
        return "", ""
    if re.fullmatch(r"0+", reg_norm):
        return "", ""
    if any(token in reg_norm.lower() for token in ["qualification", "hospital", "patient", "claim", "policy", "address"]):
        return "", ""
    return reg, reg_norm


def _extract_hospital_from_raw_payload(raw_payload: dict[str, Any]) -> str:
    if not isinstance(raw_payload, dict):
        return ""
    context = raw_payload.get("context")
    if not isinstance(context, dict):
        return ""

    legacy = context.get("legacy")
    if isinstance(legacy, dict):
        for key in ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"]:
            candidate = _normalize_space_text(legacy.get(key))
            if candidate:
                return candidate

    claim = context.get("claim")
    if isinstance(claim, dict):
        nested_legacy = _safe_json(claim.get("legacy_payload"), {})
        if isinstance(nested_legacy, dict):
            for key in ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"]:
                candidate = _normalize_space_text(nested_legacy.get(key))
                if candidate:
                    return candidate
    return ""


def _build_clean_provider_registry_payload(
    claim_id: UUID,
    external_claim_id: str,
    fields: dict[str, str],
    source: str,
    confidence: float | None,
    raw_payload: dict[str, Any],
) -> dict[str, Any] | None:
    hospital_candidates = [
        _normalize_space_text(fields.get("hospital_name")),
        _extract_hospital_from_raw_payload(raw_payload),
    ]
    hospital_name = ""
    for candidate in hospital_candidates:
        if _looks_like_clean_hospital_name(candidate):
            hospital_name = candidate
            break

    doctor_name = _clean_doctor_name(fields.get("treating_doctor"))
    registration_number, reg_norm = _clean_registration_number(fields.get("treating_doctor_registration_number"))

    if not hospital_name or not doctor_name or not registration_number or not reg_norm:
        return None

    hospital_norm = _norm_key(hospital_name)
    doctor_norm = _norm_key(doctor_name)
    if not hospital_norm or not doctor_norm:
        return None

    insured_norm = _norm_key(fields.get("insured_name"))
    if insured_norm and (insured_norm == hospital_norm or insured_norm == doctor_norm):
        return None
    if hospital_norm == doctor_norm:
        return None

    return {
        "claim_id": str(claim_id),
        "external_claim_id": _txt(external_claim_id)[:100],
        "hospital_name": hospital_name,
        "treating_doctor": doctor_name,
        "treating_doctor_registration_number": registration_number,
        "hospital_norm": hospital_norm,
        "doctor_norm": doctor_norm,
        "reg_norm": reg_norm,
        "source": (_txt(source) or "claim_structured_data")[:60],
        "confidence": confidence,
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        t = _txt(v)
        if not t:
            continue
        k = t.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(t)
    return out


def _line_fingerprint(value: Any) -> str:
    t = _txt(value).lower()
    if not t:
        return ""
    t = re.sub(r"\btest\s*:\s*", "", t)
    t = re.sub(r"\blab\s*:\s*", "", t)
    t = re.sub(r"\bdate\s*:\s*\d{1,2}[\/\-.]\d{1,2}[\/\-.]\d{2,4}\b", "", t)
    t = re.sub(r"\bdate\s*:\s*\d{4}[\/\-.]\d{1,2}[\/\-.]\d{1,2}\b", "", t)
    t = re.sub(r"[^a-z0-9]+", "", t)
    return t


def _dedupe_by_fingerprint(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        t = _txt(value)
        if not t:
            continue
        fp = _line_fingerprint(t) or t.lower()
        if fp in seen:
            continue
        seen.add(fp)
        out.append(t)
    return out


def _first(*values: Any, default: str = "-") -> str:
    for value in values:
        t = _txt(value)
        if t:
            return t
    return default


def _first_non_artifact(*values: Any, default: str = "-") -> str:
    for value in values:
        t = _txt(value)
        if not t:
            continue
        if _looks_like_file_artifact_text(t):
            continue
        return t
    return default


def _alias_key_matches(key_norm: str, alias_norm: list[str]) -> bool:
    if not key_norm:
        return False
    for alias in alias_norm:
        if not alias:
            continue
        if key_norm == alias:
            return True
        if len(alias) <= 4:
            continue
        if alias in key_norm:
            return True
    return False


def _json_obj(raw: str) -> dict[str, Any] | None:
    text_value = _txt(raw)
    if not text_value:
        return None
    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.I)
        text_value = re.sub(r"\s*```$", "", text_value).strip()
    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start = text_value.find("{")
    end = text_value.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text_value[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _extract_openai_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    direct = body.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    out: list[str] = []
    output = body.get("output")
    if isinstance(output, list):
        for row in output:
            if not isinstance(row, dict):
                continue
            content = row.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, dict):
                    t = item.get("text")
                    if isinstance(t, str) and t.strip():
                        out.append(t.strip())
    if out:
        return "\n".join(out).strip()
    return ""


def _extract_chat_completion_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    first = choices[0] if choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        out: list[str] = []
        for item in content:
            if isinstance(item, dict):
                val = item.get("text") or item.get("content")
                if isinstance(val, str) and val.strip():
                    out.append(val.strip())
        return "\n".join(out).strip()
    return ""


def _resolve_structuring_llm_targets() -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []

    openai_key = _txt(settings.openai_api_key)
    if openai_key:
        openai_base = _txt(settings.openai_base_url).rstrip("/") or "https://api.openai.com/v1"
        openai_models = _dedupe([settings.openai_rag_model, settings.openai_model, "gpt-4.1-mini", "gpt-4o-mini"])
        if openai_models:
            targets.append(
                {
                    "provider": "openai",
                    "base_url": openai_base,
                    "api_key": openai_key,
                    "models": openai_models,
                }
            )

    deepseek_key = _txt(settings.deepseek_api_key)
    if deepseek_key:
        deepseek_base = _txt(settings.deepseek_base_url).rstrip("/") or "https://api.deepseek.com/v1"
        deepseek_models = _dedupe([settings.deepseek_model, "deepseek-v3.2", "deepseek-chat"])
        if deepseek_models:
            targets.append(
                {
                    "provider": "deepseek",
                    "base_url": deepseek_base,
                    "api_key": deepseek_key,
                    "models": deepseek_models,
                }
            )

    return targets


def _ensure_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS claim_structured_data (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                external_claim_id VARCHAR(100) NOT NULL,
                company_name TEXT,
                claim_type TEXT,
                insured_name TEXT,
                hospital_name TEXT,
                treating_doctor TEXT,
                treating_doctor_registration_number TEXT,
                doa TEXT,
                dod TEXT,
                diagnosis TEXT,
                complaints TEXT,
                findings TEXT,
                investigation_finding_in_details TEXT,
                medicine_used TEXT,
                high_end_antibiotic_for_rejection TEXT,
                deranged_investigation TEXT,
                claim_amount TEXT,
                conclusion TEXT,
                recommendation TEXT,
                raw_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                source VARCHAR(40) NOT NULL DEFAULT 'heuristic',
                confidence DOUBLE PRECISION,
                created_by VARCHAR(100),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_claim_structured_data_claim_id ON claim_structured_data(claim_id)"))

    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS claim_provider_registry_clean (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                external_claim_id VARCHAR(100) NOT NULL,
                hospital_name TEXT NOT NULL,
                treating_doctor TEXT NOT NULL,
                treating_doctor_registration_number TEXT NOT NULL,
                hospital_norm TEXT NOT NULL,
                doctor_norm TEXT NOT NULL,
                reg_norm TEXT NOT NULL,
                source VARCHAR(60) NOT NULL DEFAULT 'claim_structured_data',
                confidence DOUBLE PRECISION,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_provider_registry_clean_hospital ON claim_provider_registry_clean(hospital_norm)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_provider_registry_clean_doctor ON claim_provider_registry_clean(doctor_norm)"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_provider_registry_clean_reg ON claim_provider_registry_clean(reg_norm)"))


def _parse_amount(value: Any) -> str:
    t = _txt(value).replace(",", "")
    m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", t)
    if not m:
        return ""
    n = m.group(1)
    return n.rstrip("0").rstrip(".") if "." in n else n

def _strip_html(value: Any) -> str:
    return re.sub(r"<[^>]*>", " ", _txt(value)).replace("&nbsp;", " ")


def _merge_multiline(a: Any, b: Any, limit: int = 120) -> str:
    lines: list[str] = []
    for src in [a, b]:
        for raw in re.split(r"\r\n|\r|\n", _txt(src)):
            t = raw.strip(" -:\t")
            if not t:
                continue
            if t.lower() in {"-", "na", "n/a", "none", "nil"}:
                continue
            lines.append(t)
    return "\n".join(_dedupe(lines)[: max(1, limit)]) if lines else "-"



def _looks_like_billing_or_rate_text(value: Any) -> bool:
    t = _txt(value).lower()
    if not t:
        return False
    if re.search(r"\b(?:rupees?|inr|rs\.?)\b", t):
        return True
    if re.search(r"\b(?:bill(?:ing)?|invoice|receipt|cash|discount|paid|payment|charges?|rate|price)\b", t):
        return True
    if re.search(r"\b(?:total\s*(?:bill|amount|sum|payment)|final\s*payment|amount\s*claimed|claim(?:ed)?\s*amount)\b", t):
        return True
    if re.search(r"\bvalue\s*:\s*\d+(?:\.\d+)?\s*(?:rupees?|inr|rs\.?)\b", t):
        return True
    return False


def _looks_like_hospital_address_text(value: Any) -> bool:
    t = _txt(value)
    if not t:
        return False
    low = t.lower()
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


def _clean_findings_text(value: Any) -> str:
    rows: list[str] = []
    for raw in re.split(r"\r\n|\r|\n", _txt(value)):
        line = raw.strip(" -:\t")
        if not line:
            continue
        low = line.lower()
        if _looks_like_billing_or_rate_text(line):
            continue
        if re.search(r"^(?:doa|dod)\s*[:\-]", low):
            continue
        if re.search(r"^(?:date\s*[:\-]|time\s*[:\-])", low):
            continue
        if re.search(r"^(?:details?\s+of\s+medication|follow\s*up\s+recommendation|ipd\s+medicine\s+bill|medicine\s+name)\b", low):
            continue
        if re.search(r"^(?:name\s*of\s*the\s*(?:manager|establishment)|signatures?)\b", low):
            continue
        if _looks_like_hospital_address_text(line):
            continue
        if re.search(r"\b(?:follow\s*up(?:\s*to)?|review\s+after|in\s+case\s+of\s+emergency|call\s+us|contact\s*(?:us|no)?|helpline|mobile(?:\s*number)?|phone(?:\s*number)?)\b", low):
            continue
        if re.search(r"(?:\+?91[\s\-]*)?[6-9]\d{2}[\s\-]?\d{3}[\s\-]?\d{4}\b", low):
            continue
        if re.search(r"^(patient\s*name|hospital\b|name\s*of\s*the\s*establishment|full\s*postal\s*address|add\s*:|admit(?:ting|ing)\s*dr|consultant\s*name|received\s+with\s+thanks)\b", low):
            continue
        rows.append(line)
    return "\n".join(_dedupe_by_fingerprint(rows)) if rows else "-"
def _extract_doctor_from_text_blob(text_blob: str) -> str:
    src = _strip_html(text_blob)
    patterns = [
        r"(?:treat(?:ing)?\s*doctor|consultant\s*doctor|attending\s*doctor|admit\s*dr|treated\s*by)\s*[:\-]\s*([^\n\r]{3,140})",
        r"\b(Dr\.?\s*[A-Z][A-Za-z\s\.]{2,80})\b",
    ]
    for pat in patterns:
        m = re.search(pat, src, re.I)
        if not m:
            continue
        doctor = _txt(m.group(1))
        doctor = re.sub(r"\s{2,}", " ", doctor)
        doctor = re.sub(r"\b(reg(?:istration)?\s*no\.?|mci\s*reg(?:istration)?\s*no\.?|nmc\s*reg(?:istration)?\s*no\.?)\b.*$", "", doctor, flags=re.I).strip(" ,.;:-")
        if doctor and len(doctor) >= 4:
            return doctor
    return ""


def _extract_registration_from_text_blob(text_blob: str) -> str:
    src = _strip_html(text_blob)
    patterns = [
        r"(?:reg(?:istration)?\s*(?:no|number)?|mci\s*reg(?:istration)?\s*(?:no|number)?|nmc\s*reg(?:istration)?\s*(?:no|number)?)\s*[:#\-]?\s*([A-Za-z0-9\-/\.]{4,40})",
        r"\b([A-Za-z]{1,6}\d{3,}[A-Za-z0-9\-/\.]*)\b",
    ]
    for pat in patterns:
        m = re.search(pat, src, re.I)
        if not m:
            continue
        reg = _txt(m.group(1)).strip(" ,.;:-")
        if reg and len(reg) >= 4:
            return reg
    return ""


def _extract_medicines_from_text_blob(text_blob: str, limit: int = 80) -> list[str]:
    src = _strip_html(text_blob)
    lines = [ln.strip(" -:\t") for ln in re.split(r"\r\n|\r|\n", src) if _txt(ln)]
    out: list[str] = []
    in_med_section = False
    med_tokens = r"\b(tab|tablet|cap|capsule|inj|injection|syrup|drop|drops|iv|po|od|bd|tid|qid|hs|stat|mg|g|ml|mcg|iu)\b"

    for line in lines:
        low = line.lower()
        if re.search(r"\b(treatment\s+medicines?|medications?|prescriptions?|rx|drug\s+chart)\b", low):
            in_med_section = True
            continue
        if in_med_section and re.match(r"^[A-Z][A-Z\s\/\-]{6,}$", line):
            in_med_section = False
        if re.search(med_tokens, low) or in_med_section:
            cleaned = re.sub(r"\s{2,}", " ", line).strip(" ,.;")
            normalized = _normalize_medicine_line(cleaned)
            if normalized:
                out.append(normalized)

    return _dedupe(out)[: max(1, limit)]


def _clean_control_text(value: Any) -> str:
    text_value = _txt(value).replace("\x00", "")
    text_value = re.sub(r"[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]", " ", text_value)
    text_value = re.sub(r"[ \t]{2,}", " ", text_value)
    return text_value.strip()


def _is_garbled_text(value: Any) -> bool:
    text_value = _clean_control_text(value)
    if not text_value:
        return False
    sample = text_value[:1200]
    total = len(sample)
    if total < 24:
        return False
    ascii_printable = sum(1 for ch in sample if 32 <= ord(ch) <= 126)
    non_ascii = sum(1 for ch in sample if ord(ch) > 126)
    alpha_count = sum(1 for ch in sample if ch.isalpha())
    ascii_alpha = sum(1 for ch in sample if ch.isascii() and ch.isalpha())
    vowel_count = sum(1 for ch in sample if ch.isascii() and ch.isalpha() and ch.lower() in "aeiou")
    weird_count = sum(
        1
        for ch in sample
        if not ch.isalnum() and ch not in " \t\r\n.,;:|/\\-+()[]%&'\"_*"
    )
    if ascii_printable / total < 0.70:
        return True
    if non_ascii / total > 0.08:
        return True
    if weird_count / total > 0.22:
        return True
    if alpha_count / total < 0.12:
        return True
    if ascii_alpha >= 24 and (vowel_count / ascii_alpha) < 0.18:
        return True
    return False


def _normalize_medicine_line(value: Any) -> str:
    line = _clean_control_text(value).strip(" -:\t,;")
    if not line:
        return ""
    line_ascii = line.encode("ascii", "ignore").decode("ascii")
    if not line_ascii:
        return ""
    if len(line_ascii) < max(3, int(len(line) * 0.60)):
        return ""
    line = re.sub(r"\s+", " ", line_ascii).strip(" -:\t,;")
    if not line:
        return ""
    if len(line) > 220:
        line = line[:220].rstrip()
    if _is_garbled_text(line):
        return ""

    punctuation_count = sum(1 for ch in line if (not ch.isalnum()) and (not ch.isspace()))
    if punctuation_count / max(1, len(line)) > 0.12:
        return ""

    low = line.lower()
    med_token = r"\b(tab|tablet|cap|capsule|inj|injection|syrup|drop|drops|iv|po|od|bd|tid|qid|hs|stat|mg|gm|ml|mcg|iu|unit|units)\b"
    drug_like = r"\b(paracetamol|pcm|rabep|pantop|amik|amikacin|cef|ceph|cillin|flox|cipro|metrogyl|monocef|linezolid|vancomycin|meropenem|insulin|dolo|augmentin|azithro|oflox|montair|levocet|ranitidine|omeprazole)\b"
    has_med_cue = bool(re.search(med_token, low))
    has_drug_like = bool(re.search(drug_like, low))
    if not has_med_cue and not has_drug_like:
        return ""

    return line
def _filter_medicine_lines(values: list[str], limit: int = 80) -> list[str]:
    rows: list[str] = []
    for raw in values:
        for line in re.split(r"\r\n|\r|\n", _txt(raw)):
            normalized = _normalize_medicine_line(line)
            if normalized:
                rows.append(normalized)
    return _dedupe(rows)[: max(1, limit)]

def _collect_investigation_rows(entity_docs: list[dict[str, Any]], evidence_lines: list[str] | None = None) -> list[str]:
    alias_keys = [
        "all_investigation_reports_with_values", "all_investigation_report_lines", "investigation_reports",
        "investigations", "investigation_finding_in_details", "lab_results", "test_results", "deranged_investigation",
    ]
    alias_norm = [_norm_key(a) for a in alias_keys]
    rows: list[str] = []

    def add_row(raw_line: Any) -> None:
        t = _txt(raw_line).strip(" -")
        if not t:
            return
        t = re.sub(r"^\s*test\s*:\s*", "", t, flags=re.I).strip()
        t = re.sub(r"\s+\|\s+date\s*:\s*$", "", t, flags=re.I).strip(" |")
        if t.startswith("{") and t.endswith("}"):
            return
        if t.lower() in {"na", "n/a", "none", "nil", "-"}:
            return
        if re.fullmatch(r"date\s*:\s*[\w:\/\-. ]+", t, flags=re.I):
            return
        if _looks_like_billing_or_rate_text(t):
            return
        rows.append(t)

    for entities in entity_docs:
        if not isinstance(entities, dict):
            continue
        for key, value in entities.items():
            k = _norm_key(key)
            if not _alias_key_matches(k, alias_norm):
                continue

            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        lab_name = _txt(item.get("lab_name") or item.get("laboratory") or item.get("lab") or item.get("pathology_name"))
                        test_name = _txt(item.get("test_name") or item.get("test") or item.get("name") or item.get("parameter"))
                        test_value = _txt(item.get("value") or item.get("result") or item.get("finding"))
                        unit = _txt(item.get("unit"))
                        ref = _txt(item.get("reference_range") or item.get("range") or item.get("normal_range"))
                        flag = _txt(item.get("flag") or item.get("status"))
                        dt = _txt(item.get("date") or item.get("observed_at"))
                        provided_line = _txt(item.get("line"))
                        if _looks_like_billing_or_rate_text(test_value):
                            test_value = ""
                            unit = ""
                        if _looks_like_billing_or_rate_text(provided_line):
                            provided_line = ""
                        parts: list[str] = []
                        if lab_name:
                            parts.append(f"Lab: {lab_name}")
                        if test_name:
                            parts.append(f"Test: {test_name}")
                        if test_value:
                            value_with_unit = f"{test_value} {unit}" if unit else test_value
                            parts.append(f"Value: {value_with_unit}")
                        if ref and not _looks_like_billing_or_rate_text(ref):
                            parts.append(f"Range: {ref}")
                        if flag:
                            parts.append(f"Flag: {flag}")
                        if dt:
                            parts.append(f"Date: {dt}")
                        merged_line = " | ".join(parts).strip()
                        add_row(merged_line or provided_line)
                    else:
                        for ln in re.split(r"\r\n|\r|\n", _txt(item)):
                            add_row(ln)
            else:
                for ln in re.split(r"\r\n|\r|\n", _txt(value)):
                    add_row(ln)

    for line in (evidence_lines or []):
        t = _txt(line)
        if not t:
            continue
        if re.search(r"\b(hb|hgb|wbc|rbc|platelet|esr|crp|creatinine|urea|bilirubin|sgot|sgpt|sodium|potassium|lab|reference\s*range|range)\b", t, re.I):
            add_row(t)

    return _dedupe_by_fingerprint(rows)[:200]


def _extract_lab_fingerprints(investigation_text: str) -> set[str]:
    fps: set[str] = set()
    for raw in re.split(r"\r\n|\r|\n", _txt(investigation_text)):
        line = raw.strip()
        if not line:
            continue
        test = ""
        range_text = ""
        m_test = re.search(r"(?:test\s*:\s*)([^|]+)", line, re.I)
        if m_test:
            test = _txt(m_test.group(1))
        else:
            test = _txt(line.split("|")[0])
        m_range = re.search(r"(?:range|reference\s*range)\s*:\s*([^|]+)", line, re.I)
        if m_range:
            range_text = _txt(m_range.group(1))
        test_key = _norm_key(test)
        range_key = _norm_key(range_text)
        if test_key and range_key:
            fps.add(test_key + "|" + range_key)
        elif test_key:
            fps.add(test_key)
    return fps


def _find_values(entity_docs: list[dict[str, Any]], aliases: list[str], limit: int = 20) -> list[str]:
    alias_keys = [_norm_key(a) for a in aliases]
    out: list[str] = []
    for entities in entity_docs:
        if not isinstance(entities, dict):
            continue
        for key, value in entities.items():
            k = _norm_key(key)
            if not k:
                continue
            if not _alias_key_matches(k, alias_keys):
                continue
            if isinstance(value, list):
                for item in value:
                    t = _txt(item)
                    if t and not _looks_like_file_artifact_text(t):
                        out.append(t)
            else:
                t = _txt(value)
                if t and not _looks_like_file_artifact_text(t):
                    out.append(t)
    return _dedupe(out)[: max(1, limit)]


def _investigation_lines(entity_docs: list[dict[str, Any]], evidence_lines: list[str] | None = None) -> list[str]:
    rows = _collect_investigation_rows(entity_docs, evidence_lines)
    if rows:
        return rows
    lines = _find_values(
        entity_docs,
        [
            "all_investigation_reports_with_values",
            "all_investigation_report_lines",
            "investigation_reports",
            "investigations",
            "lab_results",
            "test_results",
        ],
        limit=200,
    )
    out: list[str] = []
    for line in lines:
        if line.startswith("{") and line.endswith("}"):
            continue
        out.append(line)
    return _dedupe_by_fingerprint(out)


def _deranged(lines: list[str]) -> str:
    matched = [x for x in lines if re.search(r"\b(high|low|elevated|decreased|abnormal|deranged|positive|negative)\b", x, re.I)]
    if not matched:
        return "No deranged investigation values found."
    return "\n".join(matched[:40])


def _high_end_antibiotic(text_blob: str) -> str:
    src = _txt(text_blob).lower()
    found: list[str] = []
    for name in HIGH_END_ANTIBIOTICS:
        pattern = r"\b" + re.escape(name).replace(r"\ ", r"\s+") + r"\b"
        if re.search(pattern, src, re.I):
            found.append(name.title())
    found = _dedupe(found)
    return "Yes: " + ", ".join(found) if found else "No"


def _split_medicine_aliases(value: Any) -> list[str]:
    raw = _txt(value)
    if not raw:
        return []
    parts = re.split(r"[,;/+|]|\band\b|\bwith\b", raw, flags=re.I)
    out: list[str] = []
    for part in parts:
        t = re.sub(r"\s+", " ", _txt(part)).strip(" .:-")
        if not t:
            continue
        t_norm = re.sub(r"[^a-z0-9]+", "", t.lower())
        if len(t_norm) < 4:
            continue
        if t_norm in {"inj", "injiv", "tablet", "tab", "capsule", "cap", "syrup", "syp"}:
            continue
        out.append(t)
    return _dedupe(out)[:50]


def _load_high_end_medicine_catalog(db: Session) -> list[dict[str, Any]]:
    like_params: dict[str, Any] = {}
    clauses: list[str] = ["is_high_end_antibiotic = TRUE"]
    for idx, name in enumerate(HIGH_END_ANTIBIOTICS):
        key = f"h{idx}"
        clauses.append(f"LOWER(COALESCE(components, '')) LIKE :{key}")
        like_params[key] = "%" + name.lower() + "%"

    sql = (
        "SELECT medicine_name, components, is_high_end_antibiotic "
        "FROM medicine_component_lookup "
        + "WHERE " + " OR ".join(clauses) + " "
        + "ORDER BY medicine_name ASC"
    )

    try:
        rows = db.execute(text(sql), like_params).mappings().all()
    except Exception:
        return []

    catalog: list[dict[str, Any]] = []
    for row in rows:
        medicine_name = _txt(row.get("medicine_name"))
        components = _txt(row.get("components"))
        searchable = "\n".join([medicine_name, components])
        aliases = _split_medicine_aliases(searchable)
        if not aliases and medicine_name:
            aliases = [medicine_name]
        if not aliases:
            continue

        component_hits = _match_high_end_medicines(searchable, HIGH_END_ANTIBIOTICS)
        if bool(row.get("is_high_end_antibiotic")):
            display_name = medicine_name or (component_hits[0] if component_hits else aliases[0])
        elif component_hits:
            display_name = component_hits[0]
        else:
            display_name = medicine_name or aliases[0]

        catalog.append({"medicine_name": display_name, "aliases": aliases})

    return catalog


def _extract_medicine_lookup_candidates(medicine_text: str, evidence_blob: str, limit: int = 25) -> list[str]:
    stop_exact = {
        "ns",
        "normal saline",
        "rl",
        "ringer lactate",
        "iv fluids",
        "cash bill",
        "medicine",
    }
    out: list[str] = []
    raw_lines: list[str] = []
    for value in [medicine_text, evidence_blob]:
        for line in re.split(r"\r\n|\r|\n|;", _txt(value)):
            t = _txt(line)
            if t:
                raw_lines.append(t)

    for raw in raw_lines:
        normalized = _normalize_medicine_line(raw) or _clean_control_text(raw)
        if not normalized:
            continue

        candidate = re.sub(r"^\s*\d+[.)\-:]*\s*", "", normalized)
        candidate = re.sub(r"\b(?:inj(?:ection)?|tab(?:let)?|cap(?:sule)?|syrup|drop(?:s)?|iv|im|po|od|bd|tid|qid|hs|stat|sos)\b\.?", " ", candidate, flags=re.I)
        candidate = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|gm|g|ml|mcg|iu|units?)\b", " ", candidate, flags=re.I)
        candidate = re.sub(r"\b(?:qty|x|for\s+\d+\s+days?)\b.*$", "", candidate, flags=re.I)
        candidate = re.sub(r"[^A-Za-z0-9+/\- ]", " ", candidate)
        candidate = re.sub(r"\s{2,}", " ", candidate).strip(" .,:;-")

        for token in (_split_medicine_aliases(candidate) or [candidate]):
            token_clean = re.sub(r"\s{2,}", " ", _txt(token)).strip(" .,:;-")
            if not token_clean:
                continue
            token_low = token_clean.lower()
            if token_low in stop_exact:
                continue
            if not re.search(r"[a-z]", token_low):
                continue
            if len(re.sub(r"[^a-z0-9]+", "", token_low)) < 4:
                continue
            out.append(token_clean)

    return _dedupe(out)[: max(1, limit)]


def _flatten_text_values(value: Any, depth: int = 0, max_depth: int = 5) -> list[str]:
    if depth > max_depth:
        return []
    if isinstance(value, dict):
        out: list[str] = []
        for v in value.values():
            out.extend(_flatten_text_values(v, depth + 1, max_depth))
        return out
    if isinstance(value, list):
        out: list[str] = []
        for item in value:
            out.extend(_flatten_text_values(item, depth + 1, max_depth))
        return out
    t = _txt(value)
    return [t] if t else []


def _lookup_high_end_for_candidate_via_custom_api(candidate: str, client: httpx.Client) -> list[str]:
    base_url = _txt(settings.drug_lookup_api_url)
    if not base_url:
        return []

    headers: dict[str, str] = {}
    api_key = _txt(settings.drug_lookup_api_key)
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["x-api-key"] = api_key

    try:
        resp = client.get(base_url, params={"name": candidate}, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        _logger.warning("Drug lookup API failed for %r: %s", candidate, exc)
        return []

    text_blob = "\n".join(_flatten_text_values(payload))
    return _match_high_end_medicines(text_blob, HIGH_END_ANTIBIOTICS)


def _lookup_high_end_for_candidate_via_rxnav(candidate: str, client: httpx.Client) -> list[str]:
    try:
        approx_resp = client.get(f"{_RXNAV_BASE_URL}/approximateTerm.json", params={"term": candidate, "maxEntries": 3})
        approx_resp.raise_for_status()
        approx_payload = approx_resp.json()
    except Exception as exc:
        _logger.warning("RxNav lookup failed for %r: %s", candidate, exc)
        return []

    candidates = (
        approx_payload.get("approximateGroup", {}).get("candidate", [])
        if isinstance(approx_payload, dict)
        else []
    )
    rxcuis: list[str] = []
    for item in candidates[:3]:
        if isinstance(item, dict):
            rxcui = _txt(item.get("rxcui"))
            if rxcui:
                rxcuis.append(rxcui)

    names: list[str] = [candidate]
    for rxcui in _dedupe(rxcuis)[:3]:
        try:
            rel_resp = client.get(f"{_RXNAV_BASE_URL}/rxcui/{rxcui}/allrelated.json")
            rel_resp.raise_for_status()
            rel_payload = rel_resp.json()
        except Exception:
            continue

        groups = (
            rel_payload.get("allRelatedGroup", {}).get("conceptGroup", [])
            if isinstance(rel_payload, dict)
            else []
        )
        for group in groups:
            if not isinstance(group, dict):
                continue
            props = group.get("conceptProperties") if isinstance(group.get("conceptProperties"), list) else []
            for prop in props:
                if not isinstance(prop, dict):
                    continue
                name = _txt(prop.get("name"))
                if name:
                    names.append(name)

    return _match_high_end_medicines("\n".join(names), HIGH_END_ANTIBIOTICS)


def _lookup_high_end_medicines_via_api(candidates: list[str]) -> tuple[list[str], bool]:
    if not bool(getattr(settings, "drug_lookup_api_enabled", True)):
        return [], False

    candidate_list = _dedupe([_txt(x) for x in candidates if _txt(x)])[:25]
    if not candidate_list:
        return [], False

    timeout_s = float(getattr(settings, "drug_lookup_api_timeout_seconds", 8.0) or 8.0)
    timeout_s = max(2.0, min(timeout_s, 30.0))

    matched: list[str] = []
    used_api_lookup = False

    try:
        with httpx.Client(timeout=timeout_s) as client:
            for candidate in candidate_list:
                ck = _norm_key(candidate)
                if ck and ck in _DRUG_API_HIGH_END_CACHE:
                    matched.extend(_DRUG_API_HIGH_END_CACHE.get(ck) or [])
                    continue

                hits: list[str] = []
                if _txt(getattr(settings, "drug_lookup_api_url", "")):
                    used_api_lookup = True
                    hits = _lookup_high_end_for_candidate_via_custom_api(candidate, client)

                if (not hits) and bool(getattr(settings, "drug_lookup_use_rxnav_fallback", True)):
                    used_api_lookup = True
                    hits = _lookup_high_end_for_candidate_via_rxnav(candidate, client)

                hits = _dedupe(hits)
                if ck:
                    _DRUG_API_HIGH_END_CACHE[ck] = hits
                matched.extend(hits)
    except Exception:
        return _dedupe(matched), used_api_lookup

    return _dedupe(matched), used_api_lookup


def _fuzzy_match_high_end_candidates(candidates: list[str], catalog: list[dict[str, Any]]) -> list[str]:
    candidate_list = _dedupe([_txt(x) for x in candidates if _txt(x)])
    if not candidate_list:
        return []

    alias_index: list[tuple[str, str]] = []
    for entry in catalog:
        display_name = _txt(entry.get("medicine_name"))
        aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
        for alias in aliases:
            alias_norm = _norm_key(alias)
            if len(alias_norm) >= 6 and display_name:
                alias_index.append((alias_norm, display_name))

    found: list[str] = []
    for cand in candidate_list:
        cand_norm = _norm_key(cand)
        if len(cand_norm) < 6:
            continue

        for high in HIGH_END_ANTIBIOTICS:
            high_norm = _norm_key(high)
            if len(high_norm) < 6:
                continue
            similarity = SequenceMatcher(None, cand_norm, high_norm).ratio()
            if cand_norm[:2] == high_norm[:2] and similarity >= 0.62:
                found.append(high.title())

        best_name = ""
        best_score = 0.0
        for alias_norm, display_name in alias_index:
            if abs(len(alias_norm) - len(cand_norm)) > max(5, int(max(len(alias_norm), len(cand_norm)) * 0.45)):
                continue
            score = SequenceMatcher(None, cand_norm, alias_norm).ratio()
            if score > best_score:
                best_score = score
                best_name = display_name

        if best_name and best_score >= 0.86:
            found.append(best_name)

    return _dedupe(found)


def _match_candidates_against_medicine_catalog(
    candidates: list[str], catalog: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    candidate_list = _dedupe([_txt(x) for x in candidates if _txt(x)])
    if not candidate_list or not catalog:
        return [], candidate_list

    catalog_index: list[tuple[str, list[str]]] = []
    for entry in catalog:
        display_name = _txt(entry.get("medicine_name"))
        aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
        alias_norms: list[str] = []
        for alias in aliases or [display_name]:
            alias_norm = _norm_key(alias)
            if len(alias_norm) >= 4 and alias_norm not in alias_norms:
                alias_norms.append(alias_norm)
        display_norm = _norm_key(display_name)
        if len(display_norm) >= 4 and display_norm not in alias_norms:
            alias_norms.append(display_norm)
        if display_name and alias_norms:
            catalog_index.append((display_name, alias_norms))

    matched_names: list[str] = []
    unresolved: list[str] = []
    for candidate in candidate_list:
        candidate_norm = _norm_key(candidate)
        if len(candidate_norm) < 4:
            unresolved.append(candidate)
            continue

        matched_display = ""
        for display_name, alias_norms in catalog_index:
            if any(candidate_norm == alias_norm for alias_norm in alias_norms):
                matched_display = display_name
                break
            if len(candidate_norm) >= 6 and any(
                candidate_norm in alias_norm or alias_norm in candidate_norm
                for alias_norm in alias_norms
                if len(alias_norm) >= 6
            ):
                matched_display = display_name
                break

        if matched_display:
            matched_names.append(matched_display)
        else:
            unresolved.append(candidate)

    return _dedupe(matched_names), _dedupe(unresolved)


def _match_high_end_medicines(text_blob: str, names_or_catalog: list[Any]) -> list[str]:
    src = _txt(text_blob).lower()
    if not src:
        return []

    found: list[str] = []
    for entry in names_or_catalog:
        if isinstance(entry, dict):
            display_name = _txt(entry.get("medicine_name"))
            aliases = entry.get("aliases") if isinstance(entry.get("aliases"), list) else []
            candidate_aliases = aliases or [display_name]
        else:
            display_name = _txt(entry)
            candidate_aliases = [display_name]

        matched = False
        for alias in candidate_aliases:
            a = _txt(alias).lower()
            if not a:
                continue
            pat = r"\b" + re.escape(a).replace(r"\ ", r"\s+") + r"\b"
            if re.search(pat, src, re.I):
                matched = True
                break

        if matched and display_name:
            found.append(display_name)

    return _dedupe(found)


def _append_sentence_unique(base_text: Any, sentence: Any) -> str:
    base = _txt(base_text)
    add = _txt(sentence)
    if not add:
        return base or "-"
    if not base or base == "-":
        return add
    if add.lower() in base.lower():
        return base
    sep = "" if base.endswith((".", "!", "?")) else "."
    return f"{base}{sep} {add}"


def _assess_high_end_antibiotic_justification(db: Session, fields: dict[str, str], ctx: dict[str, Any]) -> dict[str, Any]:
    medicine_text = _txt(fields.get("medicine_used"))
    investigations_text = _txt(fields.get("investigation_finding_in_details"))
    findings_text = _txt(fields.get("findings"))
    evidence_lines = ctx.get("evidence_lines") if isinstance(ctx.get("evidence_lines"), list) else []
    latest_report_text = _txt(ctx.get("latest_report_text"))
    evidence_blob = "\n".join([medicine_text, investigations_text, findings_text, "\n".join(evidence_lines[:200]), latest_report_text])

    catalog = _load_high_end_medicine_catalog(db)
    matched_db = _match_high_end_medicines(evidence_blob, catalog)
    matched_static = _match_high_end_medicines(evidence_blob, HIGH_END_ANTIBIOTICS)

    lookup_candidates = _extract_medicine_lookup_candidates(medicine_text, "", limit=25)
    if not lookup_candidates:
        lookup_candidates = _extract_medicine_lookup_candidates("", evidence_blob, limit=25)
    matched_from_db_candidates, unresolved_candidates = _match_candidates_against_medicine_catalog(lookup_candidates, catalog)
    matched_fuzzy = _fuzzy_match_high_end_candidates(unresolved_candidates, catalog)
    matched_fuzzy_norms = {_norm_key(name) for name in matched_fuzzy if _norm_key(name)}
    unresolved_after_db = [
        cand
        for cand in unresolved_candidates
        if _norm_key(cand) not in matched_fuzzy_norms
    ]
    matched_api, used_api_lookup = _lookup_high_end_medicines_via_api(unresolved_after_db)

    matched = _dedupe(
        (matched_db or [])
        + (matched_from_db_candidates or [])
        + (matched_static or [])
        + (matched_fuzzy or [])
        + (matched_api or [])
    )

    if not matched:
        return {
            "matched": [],
            "label": "No",
            "justification_present": True,
            "missing_evidence": [],
            "used_db_catalog": bool(catalog),
            "used_api_lookup": bool(used_api_lookup),
            "api_candidates_checked": len(unresolved_after_db),
            "db_candidates_checked": len(lookup_candidates),
        }

    low = evidence_blob.lower()

    has_culture_positive = bool(re.search(r"\b(?:blood|urine|pus|sputum)\s*culture\b", low))
    has_culture_negative = bool(
        re.search(r"\b(?:no|without|not\s+done|not\s+available|missing|absent)\s+(?:blood\s+|urine\s+|pus\s+|sputum\s+)?culture\b", low)
        or re.search(r"\bculture\s*(?:report)?\s*(?:not\s+done|not\s+available|missing|absent)\b", low)
    )
    has_sensitivity_positive = bool(re.search(r"\b(?:sensitivity|susceptibility|antibiogram|c\s*&\s*s)\b", low))
    has_sensitivity_negative = bool(re.search(r"\b(?:no|without|not\s+done|not\s+available|missing|absent)\s+(?:culture\s+)?(?:sensitivity|susceptibility|antibiogram)\b", low))

    has_culture_or_sensitivity = (has_culture_positive and not has_culture_negative) or (has_sensitivity_positive and not has_sensitivity_negative)

    has_sepsis_support = bool(
        re.search(r"\b(sepsis|septic\s+shock|organ\s+dysfunction|hypotension|vasopressor|qsofa|sofa|procalcitonin|lactate|shock)\b", low)
    )

    justification_present = bool(has_culture_or_sensitivity or has_sepsis_support)
    missing: list[str] = []
    if not has_culture_or_sensitivity:
        missing.append("culture/sensitivity evidence")
    if not has_sepsis_support:
        missing.append("documented sepsis severity evidence")

    label = "Yes: " + ", ".join(matched)
    label += " | Justification: " + ("Documented" if justification_present else "Missing")

    return {
        "matched": matched,
        "label": label,
        "justification_present": justification_present,
        "missing_evidence": missing,
        "used_db_catalog": bool(catalog),
        "used_api_lookup": bool(used_api_lookup),
        "api_candidates_checked": len(unresolved_after_db),
        "db_candidates_checked": len(lookup_candidates),
    }


def _apply_high_end_antibiotic_guardrail(fields: dict[str, str], assessment: dict[str, Any]) -> dict[str, str]:
    out = dict(fields)
    label = _txt((assessment or {}).get("label")) or "No"
    out["high_end_antibiotic_for_rejection"] = label

    matched = (assessment or {}).get("matched") if isinstance((assessment or {}).get("matched"), list) else []
    if not matched:
        return out

    if bool((assessment or {}).get("justification_present")):
        return out

    missing_list = ", ".join((assessment or {}).get("missing_evidence") or [])
    med_list = ", ".join([_txt(x) for x in matched if _txt(x)])
    concern = (
        f"High-end antibiotic ({med_list}) detected without documented justification"
        + (f" ({missing_list})." if missing_list else ".")
    )

    out["conclusion"] = _append_sentence_unique(out.get("conclusion"), concern)

    rec_low = _txt(out.get("recommendation")).lower()
    if any(tok in rec_low for tok in ["rejection", "reject", "inadmissible"]):
        out["recommendation"] = _append_sentence_unique(out.get("recommendation"), concern)
    elif any(tok in rec_low for tok in ["payable", "admissible", "approve"]):
        out["recommendation"] = "Claim should be kept in query/review until high-end antibiotic justification is documented."
    else:
        out["recommendation"] = _append_sentence_unique(
            out.get("recommendation"),
            "Please provide antibiotic justification (culture/sensitivity/sepsis evidence) before final approval."
        )

    return out

def _normalize_recommendation_bucket(value: Any) -> str:
    txt = _txt(value).lower()
    if not txt:
        return "unknown"
    if any(token in txt for token in ["reject", "rejection", "inadmiss", "not justified"]):
        return "reject"
    if any(token in txt for token in ["approve", "admissible", "payable"]):
        return "approve"
    if any(token in txt for token in ["query", "need_more_evidence", "manual_review", "manual review"]):
        return "query"
    return "unknown"


def _recommendation_sentence_for_bucket(bucket: str) -> str:
    if bucket == "reject":
        return "Not Justified + Inadmissible"
    if bucket == "approve":
        return "Justified + Admissible"
    return "Not Justified + Inadmissible"


def _clean_rule_note_text(value: Any) -> str:
    txt = _txt(value)
    if not txt:
        return ""
    txt = re.sub(r"\bOPENAI_MERGED_REVIEW\b", "", txt, flags=re.I)
    txt = re.sub(r"\bRule-wise medical review.*$", "", txt, flags=re.I)
    txt = re.sub(r"\bTriggered points?\s*:\s*", "", txt, flags=re.I)
    txt = re.sub(r"\b[Rr]\d{3}\b\s*[-:]\s*", "", txt)
    txt = re.sub(r"\bDX\d{3}\b\s*[-:]\s*", "", txt)
    txt = re.sub(r"\bMissing evidence\s*:[^.;\n]*(?:[.;]|$)", " ", txt, flags=re.I)
    txt = re.sub(r"\bLearning signal\s*:[^.]*\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\btherefore,\s*one or more clinical rules are triggered\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\btherefore,\s*this rule is triggered\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\b(?:one or more\s+)?clinical rules?\s+are\s+triggered\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bthis rule is triggered\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bAuto-learned rule from auditor conclusion and extraction context\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bSuper-admin approval required\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bSuggested outcome\s*:\s*[A-Z_]+\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bTriggered condition identified\.?", " ", txt, flags=re.I)
    if re.search(r"\bChecklist condition matched and evidence pattern triggered\b", txt, flags=re.I):
        return ""
    if re.search(r"\bGST guardrail triggered\b", txt, flags=re.I):
        return ""
    if re.search(r"\b(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion)\b", txt, flags=re.I):
        return ""
    machine_term = r"(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion|complaints)"
    if re.fullmatch(rf"{machine_term}(?:\s*,\s*{machine_term})*", txt.strip(), flags=re.I):
        return ""
    txt = re.sub(r"\s+", " ", txt).strip(" .;:-")
    return txt
def _rulewise_conclusion_from_decision(decision: dict[str, Any], decision_payload: dict[str, Any]) -> str:
    payload = decision_payload if isinstance(decision_payload, dict) else {}
    reporting = payload.get("source_summary", {}).get("reporting") if isinstance(payload.get("source_summary"), dict) else {}
    if isinstance(reporting, dict):
        txt = _clean_rule_note_text(reporting.get("conclusion"))
        if txt:
            return txt

    direct = _clean_rule_note_text(payload.get("conclusion"))
    if direct:
        return direct

    hits = _safe_json((decision or {}).get("rule_hits"), [])
    if not isinstance(hits, list):
        hits = []

    points: list[str] = []
    missing_items: list[str] = []
    seen_points: set[str] = set()
    seen_missing: set[str] = set()
    for item in hits[:12]:
        if not isinstance(item, dict):
            continue

        source_name = _txt(item.get("source")).lower()
        if source_name and source_name not in {"openai_claim_rules", "openai_diagnosis_criteria"}:
            continue

        code = _txt(item.get("code") or item.get("rule_id") or "RULE").upper()
        if source_name == "openai_claim_rules" and not re.fullmatch(r"R\d{3}", code):
            continue
        if code == "OPENAI_MERGED_REVIEW":
            continue
        if re.search(r"\blearning from conclusion and extraction\b", _txt(item.get("name") or item.get("title")), flags=re.I):
            continue

        note = _clean_rule_note_text(item.get("note") or item.get("why_triggered") or item.get("summary") or item.get("reason"))
        missing = item.get("missing_evidence") if isinstance(item.get("missing_evidence"), list) else []
        if note:
            key = note.lower()
            if key not in seen_points:
                seen_points.add(key)
                points.append(note)
        for miss in missing:
            cleaned = _clean_rule_note_text(miss)
            if not cleaned:
                continue
            key = cleaned.lower()
            if key not in seen_missing:
                seen_missing.add(key)
                missing_items.append(cleaned)

    bucket = _normalize_recommendation_bucket((decision or {}).get("recommendation"))
    if bucket == "approve":
        prefix = "Rule-wise medical review supports admissibility on available records."
    else:
        prefix = "The case requires additional clinical correlation before final admissibility decision."

    if not points:
        return ""

    parts: list[str] = [prefix]
    if points:
        parts.append("Key clinical observations: " + "; ".join(points[:3]) + ".")
    return " ".join(parts).strip()

def _load_context(db: Session, claim_id: UUID) -> dict[str, Any]:
    claim = db.execute(
        text(
            """
            SELECT c.id, c.external_claim_id, c.patient_name, c.patient_identifier,
                   c.tags,
                   c.status, c.assigned_doctor_id, l.legacy_payload
            FROM claims c
            LEFT JOIN claim_legacy_data l ON l.claim_id = c.id
            WHERE c.id = :claim_id
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if claim is None:
        raise ClaimStructuringError("claim not found")

    decision = db.execute(
        text(
            """
            SELECT recommendation, decision_payload, rule_hits, explanation_summary, generated_at
            FROM decision_results
            WHERE claim_id = :claim_id
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()

    latest_report = db.execute(
        text(
            """
            SELECT report_markdown
            FROM report_versions
            WHERE claim_id = :claim_id
            ORDER BY created_at DESC, version_no DESC
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()

    docs = db.execute(
        text(
            """
            SELECT cd.id AS document_id, cd.file_name, cd.mime_type, cd.uploaded_at,
                   de.extracted_entities, de.evidence_refs, de.confidence
            FROM claim_documents cd
            LEFT JOIN LATERAL (
                SELECT extracted_entities, evidence_refs, confidence
                FROM document_extractions dex
                WHERE dex.document_id = cd.id
                ORDER BY dex.created_at DESC
                LIMIT 1
            ) de ON TRUE
            WHERE cd.claim_id = :claim_id
            ORDER BY cd.uploaded_at ASC
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().all()

    entity_docs: list[dict[str, Any]] = []
    evidence_lines: list[str] = []
    for row in docs:
        entities = _safe_json(row.get("extracted_entities"), {})
        if isinstance(entities, dict) and entities:
            entity_docs.append(_sanitize_entities_for_structuring(entities))
        refs = _safe_json(row.get("evidence_refs"), [])
        if isinstance(refs, list):
            for item in refs:
                if isinstance(item, dict):
                    s = _txt(item.get("snippet") or item.get("text") or item.get("value"))
                else:
                    s = _txt(item)
                if s:
                    evidence_lines.append(s)

    return {
        "claim": dict(claim),
        "legacy": _safe_json(claim.get("legacy_payload"), {}),
        "decision": dict(decision) if decision else {},
        "entity_docs": entity_docs,
        "evidence_lines": _dedupe(evidence_lines)[:300],
        "latest_report_text": "",
    }


def _heuristic_fields(ctx: dict[str, Any]) -> dict[str, str]:
    claim = ctx.get("claim") or {}
    claim_tags = claim.get("tags") if isinstance(claim.get("tags"), list) else []
    legacy = ctx.get("legacy") if isinstance(ctx.get("legacy"), dict) else {}
    decision = ctx.get("decision") if isinstance(ctx.get("decision"), dict) else {}
    entity_docs = ctx.get("entity_docs") if isinstance(ctx.get("entity_docs"), list) else []
    evidence_lines = ctx.get("evidence_lines") if isinstance(ctx.get("evidence_lines"), list) else []
    latest_report_text = _txt(ctx.get("latest_report_text"))

    investigations = _investigation_lines(entity_docs, evidence_lines)
    medicines_from_entities = _filter_medicine_lines(
        _find_values(
            entity_docs,
            [
                "medicine_used",
                "medicines",
                "medications",
                "treatment_medicines",
                "drugs",
                "rx",
                "prescription",
                "treatment",
            ],
            120,
        ),
        120,
    )
    medicines_from_blob = _filter_medicine_lines(
        _extract_medicines_from_text_blob(
            "\n".join([latest_report_text, "\n".join(evidence_lines)]),
            limit=120,
        ),
        120,
    )
    medicines = _merge_multiline("\n".join(medicines_from_entities), "\n".join(medicines_from_blob), 120)

    doctor_from_entities = (
        _find_values(
            entity_docs,
            [
                "treating_doctor",
                "treating_doctor_name",
                "doctor_name",
                "attending_doctor",
                "consultant_doctor",
                "consulting_doctor",
                "admit_doctor",
                "admit_dr",
            ],
            2,
        )
        or [""]
    )[0]
    doctor_from_blob = _extract_doctor_from_text_blob("\n".join([latest_report_text, "\n".join(evidence_lines)]))

    registration_from_entities = (
        _find_values(
            entity_docs,
            [
                "doctor_registration_number",
                "treating_doctor_registration_number",
                "doctor_reg_no",
                "registration_no",
                "registration_number",
                "mci_reg_no",
                "nmc_reg_no",
                "doctor_registration",
            ],
            2,
        )
        or [""]
    )[0]
    registration_from_blob = _extract_registration_from_text_blob("\n".join([latest_report_text, "\n".join(evidence_lines)]))
    registration_from_entities_clean = _clean_registration_number(registration_from_entities)[0]
    registration_from_blob_clean = _clean_registration_number(registration_from_blob)[0]
    legacy_registration_clean = _clean_registration_number(legacy.get("doctor_registration_number"))[0]

    decision_payload = _safe_json(decision.get("decision_payload"), {}) if isinstance(decision, dict) else {}
    recommendation_raw = _first(
        decision.get("recommendation"),
        decision_payload.get("recommendation"),
        _find_values(entity_docs, ["final_recommendation", "recommendation"], 1)[0] if _find_values(entity_docs, ["final_recommendation", "recommendation"], 1) else "",
        default="need_more_evidence",
    )
    recommendation_bucket = _normalize_recommendation_bucket(recommendation_raw)

    reporting_block = (
        decision_payload.get("source_summary", {}).get("reporting")
        if isinstance(decision_payload.get("source_summary"), dict)
        else {}
    )
    recommendation = _first(
        reporting_block.get("recommendation_text") if isinstance(reporting_block, dict) else "",
        _recommendation_sentence_for_bucket(recommendation_bucket),
        "Not Justified + Inadmissible",
    )
    if re.search(r"\b(recommended\s+for\s+rejection|reject(?:ion|ed)?|inadmissible)\b", recommendation, flags=re.I):
        recommendation = "Not Justified + Inadmissible"

    decision_summary = _txt((decision or {}).get("explanation_summary"))
    if re.match(r"^(reject triggers|query triggers|approval signals)\s*:\s*", decision_summary, flags=re.I):
        decision_summary = ""

    rulewise_conclusion = _rulewise_conclusion_from_decision(decision if isinstance(decision, dict) else {}, decision_payload)
    findings_text = _clean_findings_text(
        _merge_multiline(
            _find_values(entity_docs, ["major_diagnostic_finding", "clinical_findings", "diagnostic_finding", "hospital_finding", "summary"], 8),
            "\n".join(evidence_lines[:40]),
            80,
        )
    )

    fields = {
        "company_name": _first_non_artifact(
            _find_values(entity_docs, ["company_name", "insurance_company", "insurer", "tpa", "insurance_tpa", "payer_name"], 1)[0] if _find_values(entity_docs, ["company_name", "insurance_company", "insurer", "tpa", "insurance_tpa", "payer_name"], 1) else "",
            legacy.get("company_name"),
            legacy.get("insurer_name"),
            legacy.get("insurance_company"),
            default="Medi Assist Insurance TPA Pvt. Ltd.",
        ),
        "claim_type": _first(_find_values(entity_docs, ["claim_type", "case_type"], 1)[0] if _find_values(entity_docs, ["claim_type", "case_type"], 1) else "", legacy.get("claim_type"), "-"),
        "insured_name": _first_non_artifact(
            _find_values(entity_docs, ["insured_name", "name", "patient_name", "insured", "beneficiary", "policy_holder_name"], 1)[0] if _find_values(entity_docs, ["insured_name", "name", "patient_name", "insured", "beneficiary", "policy_holder_name"], 1) else "",
            legacy.get("benef_name"),
            legacy.get("benefname"),
            legacy.get("beneficiary_name"),
            legacy.get("insured_name"),
            claim.get("patient_name"),
            default="-",
        ),
        "hospital_name": _clean_hospital_name(
            _first_non_artifact(
                claim_tags[2] if len(claim_tags) > 2 else "",
                _find_values(entity_docs, ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"], 1)[0] if _find_values(entity_docs, ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"], 1) else "",
                legacy.get("hospital_name"),
                default="-",
            )
        ) or "-",
        "treating_doctor": _first_non_artifact(doctor_from_blob, doctor_from_entities, legacy.get("treating_doctor"), default="-"),
        "treating_doctor_registration_number": _first_non_artifact(
            registration_from_blob_clean,
            registration_from_entities_clean,
            legacy_registration_clean,
            default="-",
        ),
        "doa": _first(legacy.get("doa_date"), legacy.get("date_of_admission"), legacy.get("admission_date"), _find_values(entity_docs, ["doa", "doa_date", "date_of_admission", "admission_date"], 1)[0] if _find_values(entity_docs, ["doa", "doa_date", "date_of_admission", "admission_date"], 1) else ""),
        "dod": _first(legacy.get("dod_date"), legacy.get("date_of_discharge"), legacy.get("discharge_date"), _find_values(entity_docs, ["dod", "dod_date", "date_of_discharge", "discharge_date"], 1)[0] if _find_values(entity_docs, ["dod", "dod_date", "date_of_discharge", "discharge_date"], 1) else ""),
        "diagnosis": _first(_find_values(entity_docs, ["diagnosis", "final_diagnosis", "provisional_diagnosis", "primary_diagnosis"], 1)[0] if _find_values(entity_docs, ["diagnosis", "final_diagnosis", "provisional_diagnosis", "primary_diagnosis"], 1) else "", legacy.get("diagnosis"), "-"),
        "complaints": _first(_find_values(entity_docs, ["chief_complaints_at_admission", "chief_complaints", "chief_complaint", "presenting_complaints", "complaints"], 1)[0] if _find_values(entity_docs, ["chief_complaints_at_admission", "chief_complaints", "chief_complaint", "presenting_complaints", "complaints"], 1) else "", legacy.get("complaints"), "-"),
        "findings": findings_text,
        "investigation_finding_in_details": "\n".join(investigations[:120]) if investigations else "No investigation reports available.",
        "medicine_used": medicines,
        "high_end_antibiotic_for_rejection": _high_end_antibiotic("\n".join([medicines, "\n".join(investigations[:120]), "\n".join(evidence_lines[:120])])),
        "deranged_investigation": _deranged(investigations),
        "claim_amount": _first(_parse_amount(legacy.get("claim_amount")), _parse_amount(legacy.get("claimed_amount")), _parse_amount(legacy.get("bill_amount")), _parse_amount(_find_values(entity_docs, ["claim_amount", "claimed_amount", "bill_amount", "amount_claimed"], 1)[0] if _find_values(entity_docs, ["claim_amount", "claimed_amount", "bill_amount", "amount_claimed"], 1) else ""), "-"),
        "conclusion": _first(rulewise_conclusion, decision_summary, _find_values(entity_docs, ["detailed_conclusion", "conclusion", "rationale", "decision"], 1)[0] if _find_values(entity_docs, ["detailed_conclusion", "conclusion", "rationale", "decision"], 1) else "", "Claim reviewed based on available records."),
        "recommendation": _first(recommendation, "Claim kept in query for medical clarification."),
    }

    out: dict[str, str] = {}
    for key in FIELD_KEYS:
        val = _txt(fields.get(key))
        if key == "claim_type" and re.match(r"^[a-z]+/[a-z0-9.+-]+$", val, re.I):
            val = "-"
        out[key] = val or "-"
    return out


def _merge_llm_with_heuristic_fields(llm_fields: dict[str, str], heuristic_fields: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    multiline_keys = {"findings", "investigation_finding_in_details", "medicine_used", "conclusion"}

    for key in FIELD_KEYS:
        llm_val = _txt((llm_fields or {}).get(key)) or "-"
        heur_val = _txt((heuristic_fields or {}).get(key)) or "-"

        if key in {"conclusion", "recommendation"} and heur_val != "-":
            merged = heur_val
        elif llm_val == "-":
            merged = heur_val
        elif key in multiline_keys and heur_val != "-":
            merged = _merge_multiline(llm_val, heur_val, 120)
        else:
            merged = llm_val

        if key in {"treating_doctor", "treating_doctor_registration_number"} and merged == "-":
            merged = heur_val

        out[key] = merged or "-"

    out["high_end_antibiotic_for_rejection"] = _high_end_antibiotic(
        "\n".join([out.get("medicine_used", ""), out.get("investigation_finding_in_details", "")])
    )
    return out


def _clean_investigation_report_rows(value: Any) -> str:
    text_value = _txt(value)
    if not text_value or text_value == "-":
        return "No investigation reports available."

    kept: list[str] = []
    for raw_line in re.split(r"[\r\n]+", text_value):
        line = _normalize_space_text(raw_line).strip(" -")
        if not line:
            continue
        if re.search(r"\bno\s+investigation\s+reports?\s+available\b", line, flags=re.I):
            continue

        value_match = re.search(r"\bvalue\s*:\s*([^|]+)", line, flags=re.I)
        range_match = re.search(r"\breference[_\s-]*range\s*:\s*([^|]+)", line, flags=re.I)
        value_text = _normalize_space_text(value_match.group(1) if value_match else "")
        range_text = _normalize_space_text(range_match.group(1) if range_match else "")

        has_empty_value = bool(value_match) and value_text in {"", "-", "na", "n/a", "nil", "none"}
        has_empty_range = bool(range_match) and range_text in {"", "-", "na", "n/a", "nil", "none"}
        if value_match and range_match and has_empty_value and has_empty_range:
            continue
        has_value_result = bool(value_match) and not has_empty_value
        has_range_result = bool(range_match) and not has_empty_range
        has_word_result = bool(
            re.search(
                r"\b(?:positive|negative|reactive|non[-\s]*reactive|detected|not\s+detected|normal|abnormal|high|low|elevated|decreased|impression|finding|opacity|edema|haemorrhage|hemorrhage|acuity)\b",
                line,
                flags=re.I,
            )
        )
        has_only_date_without_result = bool(re.search(r"\bdate\s*:", line, flags=re.I)) and not (has_value_result or has_range_result or has_word_result)
        if has_only_date_without_result:
            continue

        line = re.sub(r"\blab_name\s*:\s*-\s*\|\s*", "", line, flags=re.I)
        line = re.sub(r"\blab\s*name\s*:\s*-\s*\|\s*", "", line, flags=re.I)
        line = re.sub(r"\blab\s*:\s*-\s*\|\s*", "", line, flags=re.I)
        line = re.sub(r"\btest\s*:\s*", "", line, flags=re.I)
        line = re.sub(r"\bvalue\s*:\s*", "Value: ", line, flags=re.I)
        line = re.sub(r"\breference[_\s-]*range\s*:\s*", "Range: ", line, flags=re.I)
        kept.append(line)

    return "\n".join(_dedupe(kept)) if kept else "No investigation reports available."


def _postprocess_structured_fields(fields: dict[str, str]) -> dict[str, str]:
    out = dict(fields or {})
    out["investigation_finding_in_details"] = _clean_investigation_report_rows(out.get("investigation_finding_in_details"))
    if re.search(r"\bno\s+investigation\s+reports?\s+available\b", out["investigation_finding_in_details"], flags=re.I):
        deranged = _txt(out.get("deranged_investigation"))
        if deranged == "-" or re.search(r"\b(?:normal|abnormal|high|low|deranged|elevated|decreased)\b", deranged, flags=re.I) is None:
            out["deranged_investigation"] = "No deranged investigation values found."
    return out


def _fraud_pattern_compare(db: Session, claim_id: UUID, fields: dict[str, str], claim_ctx: dict[str, Any]) -> dict[str, Any]:
    insured_name = _txt(fields.get("insured_name"))
    patient_name = _txt((claim_ctx or {}).get("patient_name"))
    patient_identifier = _txt((claim_ctx or {}).get("patient_identifier"))

    if not insured_name and not patient_name and not patient_identifier:
        return {"suspicious": False, "matched_claims": [], "notes": []}

    rows = db.execute(
        text(
            """
            SELECT csd.claim_id, c.external_claim_id, csd.hospital_name, csd.treating_doctor,
                   csd.treating_doctor_registration_number, csd.diagnosis,
                   csd.investigation_finding_in_details, csd.claim_amount, csd.doa, csd.dod
            FROM claim_structured_data csd
            JOIN claims c ON c.id = csd.claim_id
            WHERE csd.claim_id <> :claim_id
              AND (
                    (:insured_name <> '' AND LOWER(csd.insured_name) = LOWER(:insured_name))
                 OR (:patient_name <> '' AND LOWER(COALESCE(c.patient_name, '')) = LOWER(:patient_name))
                 OR (:patient_identifier <> '' AND LOWER(COALESCE(c.patient_identifier, '')) = LOWER(:patient_identifier))
              )
            ORDER BY csd.updated_at DESC
            LIMIT 80
            """
        ),
        {
            "claim_id": str(claim_id),
            "insured_name": insured_name,
            "patient_name": patient_name,
            "patient_identifier": patient_identifier,
        },
    ).mappings().all()

    current_lab = _extract_lab_fingerprints(_txt(fields.get("investigation_finding_in_details")))
    current_reg = _norm_key(fields.get("treating_doctor_registration_number"))
    current_doc = _norm_key(fields.get("treating_doctor"))
    current_hospital = _norm_key(fields.get("hospital_name"))
    current_diag = _norm_key(fields.get("diagnosis"))

    matched_claims: list[str] = []
    notes: list[str] = []

    for row in rows:
        prev_claim_id = _txt(row.get("external_claim_id") or row.get("claim_id"))
        prev_lab = _extract_lab_fingerprints(_txt(row.get("investigation_finding_in_details")))
        overlap = len(current_lab.intersection(prev_lab)) if current_lab and prev_lab else 0
        min_size = min(len(current_lab), len(prev_lab)) if current_lab and prev_lab else 0
        overlap_ratio = (overlap / min_size) if min_size else 0.0

        prev_reg = _norm_key(row.get("treating_doctor_registration_number"))
        prev_doc = _norm_key(row.get("treating_doctor"))
        prev_hospital = _norm_key(row.get("hospital_name"))
        prev_diag = _norm_key(row.get("diagnosis"))

        doctor_match = bool(current_reg and prev_reg and current_reg == prev_reg) or bool(current_doc and prev_doc and current_doc == prev_doc)
        facility_match = bool(current_hospital and prev_hospital and current_hospital == prev_hospital)
        diag_match = bool(current_diag and prev_diag and current_diag == prev_diag)

        suspicious = False
        if overlap >= 3 and overlap_ratio >= 0.60 and doctor_match:
            suspicious = True
            notes.append(f"High overlap in lab pattern with claim {prev_claim_id} (overlap {overlap}/{min_size}).")
        elif overlap >= 4 and overlap_ratio >= 0.70 and facility_match and diag_match:
            suspicious = True
            notes.append(f"Lab+diagnosis+facility pattern similar to claim {prev_claim_id}.")

        if suspicious:
            matched_claims.append(prev_claim_id)

    matched_claims = _dedupe(matched_claims)[:10]
    notes = _dedupe(notes)[:10]
    return {
        "suspicious": bool(matched_claims),
        "matched_claims": matched_claims,
        "notes": notes,
    }



def _hospital_trend_compare(db: Session, claim_id: UUID, fields: dict[str, str]) -> dict[str, Any]:
    hospital_name = _txt(fields.get("hospital_name"))
    if not hospital_name or hospital_name == "-":
        return {
            "hospital_name": "",
            "trend_alert": False,
            "notes": [],
            "overall": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
            "recent": {"window": 0, "total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
            "doctor_recent": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
            "recent_claim_ids": [],
            "top_diagnosis": [],
        }

    rows = db.execute(
        text(
            """
            SELECT c.external_claim_id,
                   csd.hospital_name,
                   csd.treating_doctor,
                   csd.treating_doctor_registration_number,
                   csd.diagnosis,
                   dr.recommendation,
                   dr.generated_at
            FROM claim_structured_data csd
            JOIN claims c ON c.id = csd.claim_id
            LEFT JOIN LATERAL (
                SELECT recommendation, generated_at
                FROM decision_results
                WHERE claim_id = csd.claim_id
                ORDER BY generated_at DESC
                LIMIT 1
            ) dr ON TRUE
            WHERE csd.claim_id <> :claim_id
              AND LOWER(COALESCE(csd.hospital_name, '')) = LOWER(:hospital_name)
            ORDER BY COALESCE(dr.generated_at, csd.updated_at) DESC
            LIMIT 300
            """
        ),
        {"claim_id": str(claim_id), "hospital_name": hospital_name},
    ).mappings().all()

    def _metrics(data_rows: list[dict[str, Any]]) -> dict[str, Any]:
        total = len(data_rows)
        reject = 0
        approve = 0
        query = 0
        unknown = 0
        for r in data_rows:
            bucket = _normalize_recommendation_bucket(r.get("recommendation"))
            if bucket == "reject":
                reject += 1
            elif bucket == "approve":
                approve += 1
            elif bucket == "query":
                query += 1
            else:
                unknown += 1
        reject_rate = round((reject * 100.0 / total), 2) if total else 0.0
        query_rate = round((query * 100.0 / total), 2) if total else 0.0
        return {
            "total": total,
            "reject": reject,
            "approve": approve,
            "query": query,
            "unknown": unknown,
            "reject_rate": reject_rate,
            "query_rate": query_rate,
        }

    row_dicts = [dict(r) for r in rows]
    overall = _metrics(row_dicts)
    recent_window = 40
    recent_rows = row_dicts[:recent_window]
    recent = _metrics(recent_rows)
    recent["window"] = recent_window

    current_doc = _norm_key(fields.get("treating_doctor"))
    current_reg = _norm_key(fields.get("treating_doctor_registration_number"))
    same_doctor_rows: list[dict[str, Any]] = []
    if current_doc or current_reg:
        for r in row_dicts:
            prev_doc = _norm_key(r.get("treating_doctor"))
            prev_reg = _norm_key(r.get("treating_doctor_registration_number"))
            if (current_reg and prev_reg and current_reg == prev_reg) or (current_doc and prev_doc and current_doc == prev_doc):
                same_doctor_rows.append(r)
    doctor_recent = _metrics(same_doctor_rows[:recent_window]) if same_doctor_rows else _metrics([])

    diag_counter: Counter[str] = Counter()
    for r in recent_rows:
        d = _txt(r.get("diagnosis"))
        if d and d != "-":
            diag_counter[d] += 1
    top_diagnosis = [name for (name, _count) in diag_counter.most_common(5)]
    recent_claim_ids = _dedupe([_txt(r.get("external_claim_id")) for r in recent_rows if _txt(r.get("external_claim_id"))])[:10]

    notes: list[str] = []
    trend_alert = False
    if recent.get("total", 0) >= 12 and recent.get("reject_rate", 0.0) >= 55.0:
        trend_alert = True
        notes.append(
            f"Hospital rejection trend high: {recent['reject']} of {recent['total']} recent claims rejected ({recent['reject_rate']}%)."
        )
    if recent.get("total", 0) >= 12 and recent.get("query_rate", 0.0) >= 55.0:
        notes.append(
            f"Hospital query trend high: {recent['query']} of {recent['total']} recent claims queried ({recent['query_rate']}%)."
        )
    if doctor_recent.get("total", 0) >= 5 and doctor_recent.get("reject_rate", 0.0) >= 70.0:
        trend_alert = True
        notes.append(
            f"Same treating doctor trend high: {doctor_recent['reject']} of {doctor_recent['total']} hospital-linked claims rejected ({doctor_recent['reject_rate']}%)."
        )

    return {
        "hospital_name": hospital_name,
        "trend_alert": trend_alert,
        "notes": _dedupe(notes)[:10],
        "overall": overall,
        "recent": recent,
        "doctor_recent": doctor_recent,
        "recent_claim_ids": recent_claim_ids,
        "top_diagnosis": top_diagnosis,
    }
def _apply_fraud_signals_to_fields(fields: dict[str, str], fraud_check: dict[str, Any]) -> dict[str, str]:
    out = dict(fields)
    if not bool((fraud_check or {}).get("suspicious")):
        return out

    matched = ", ".join((fraud_check or {}).get("matched_claims") or [])
    notes = "\n".join((fraud_check or {}).get("notes") or [])
    fraud_note = "Fraud pattern alert: Similar previous claim data detected"
    if matched:
        fraud_note += f" (claim(s): {matched})"
    if notes:
        fraud_note += f". {notes}"

    out["findings"] = _merge_multiline(out.get("findings"), fraud_note, 120)
    out["conclusion"] = _merge_multiline(out.get("conclusion"), fraud_note, 120)

    rec = _txt(out.get("recommendation")).lower()
    if "rejection" not in rec and "manual" not in rec and "query" not in rec:
        out["recommendation"] = _merge_multiline(
            out.get("recommendation"),
            "Potential fraud similarity found from previous claims. Manual fraud review recommended.",
            20,
        )
    return out

def _apply_hospital_trend_signals_to_fields(fields: dict[str, str], hospital_trend: dict[str, Any]) -> dict[str, str]:
    out = dict(fields)
    if not bool((hospital_trend or {}).get("trend_alert")):
        return out

    recent = (hospital_trend or {}).get("recent") if isinstance((hospital_trend or {}).get("recent"), dict) else {}
    notes = "\n".join((hospital_trend or {}).get("notes") or [])
    hospital_name = _txt((hospital_trend or {}).get("hospital_name")) or _txt(out.get("hospital_name"))
    trend_note = (
        f"Hospital learning alert: {hospital_name} shows higher rejection trend "
        f"({recent.get('reject', 0)}/{recent.get('total', 0)} in recent cases, {recent.get('reject_rate', 0.0)}%)."
    )
    if notes:
        trend_note += f" {notes}"

    out["findings"] = _merge_multiline(out.get("findings"), trend_note, 120)
    out["conclusion"] = _merge_multiline(out.get("conclusion"), trend_note, 120)

    rec = _txt(out.get("recommendation")).lower()
    if "rejection" not in rec and "manual" not in rec and "query" not in rec:
        out["recommendation"] = _merge_multiline(
            out.get("recommendation"),
            "Hospital historical rejection trend is high. Manual fraud/medical audit review recommended before final decision.",
            20,
        )
    return out

def _extract_rule_names_from_hits(rule_hits: Any, limit: int = 80) -> list[str]:
    hits = _safe_json(rule_hits, [])
    if not isinstance(hits, list):
        return []

    names: list[str] = []
    for hit in hits:
        if isinstance(hit, dict):
            triggered = hit.get("triggered")
            if triggered is False:
                continue
            name = _first(
                hit.get("rule_name"),
                hit.get("name"),
                hit.get("title"),
                hit.get("condition"),
                hit.get("rule_id"),
                hit.get("id"),
                default="",
            )
        else:
            name = _txt(hit)
        name = re.sub(r"\s{2,}", " ", _txt(name)).strip(" ,.;:-")
        if name:
            names.append(name)
    return _dedupe(names)[: max(1, limit)]


def _previous_rule_learning(db: Session, claim_id: UUID, fields: dict[str, str]) -> dict[str, Any]:
    hospital_name = _txt(fields.get("hospital_name"))
    doctor_norm = _norm_key(fields.get("treating_doctor"))
    reg_norm = _norm_key(fields.get("treating_doctor_registration_number"))

    if not hospital_name and not doctor_norm and not reg_norm:
        return {
            "sample_size": 0,
            "matched_claim_ids": [],
            "top_triggered_rules": [],
            "recommendation_mix": {"reject": 0, "approve": 0, "query": 0, "unknown": 0},
            "reject_rate": 0.0,
            "learning_alert": False,
            "notes": [],
        }

    rows = db.execute(
        text(
            """
            SELECT c.external_claim_id,
                   dr.recommendation,
                   dr.rule_hits,
                   dr.generated_at
            FROM decision_results dr
            JOIN claims c ON c.id = dr.claim_id
            LEFT JOIN claim_structured_data csd ON csd.claim_id = dr.claim_id
            WHERE dr.claim_id <> :claim_id
              AND dr.generated_by = 'checklist_pipeline'
              AND dr.rule_hits IS NOT NULL
              AND (
                    (:hospital_name <> '' AND LOWER(COALESCE(csd.hospital_name, '')) = LOWER(:hospital_name))
                 OR (:doctor_norm <> '' AND LOWER(REGEXP_REPLACE(COALESCE(csd.treating_doctor, ''), '[^a-z0-9]+', '', 'g')) = :doctor_norm)
                 OR (:reg_norm <> '' AND LOWER(REGEXP_REPLACE(COALESCE(csd.treating_doctor_registration_number, ''), '[^a-z0-9]+', '', 'g')) = :reg_norm)
              )
            ORDER BY dr.generated_at DESC
            LIMIT 300
            """
        ),
        {
            "claim_id": str(claim_id),
            "hospital_name": hospital_name,
            "doctor_norm": doctor_norm,
            "reg_norm": reg_norm,
        },
    ).mappings().all()

    if not rows:
        return {
            "sample_size": 0,
            "matched_claim_ids": [],
            "top_triggered_rules": [],
            "recommendation_mix": {"reject": 0, "approve": 0, "query": 0, "unknown": 0},
            "reject_rate": 0.0,
            "learning_alert": False,
            "notes": [],
        }

    rule_counter: Counter[str] = Counter()
    recommendation_mix: dict[str, int] = {"reject": 0, "approve": 0, "query": 0, "unknown": 0}
    matched_claim_ids: list[str] = []

    for row in rows:
        claim_ref = _txt(row.get("external_claim_id"))
        if claim_ref:
            matched_claim_ids.append(claim_ref)

        bucket = _normalize_recommendation_bucket(row.get("recommendation"))
        recommendation_mix[bucket] = int(recommendation_mix.get(bucket, 0)) + 1

        for rule_name in _extract_rule_names_from_hits(row.get("rule_hits"), 80):
            rule_counter[rule_name] += 1

    sample_size = len(rows)
    reject_rate = round((recommendation_mix.get("reject", 0) * 100.0 / sample_size), 2) if sample_size else 0.0
    top_triggered_rules = [
        {"rule": name, "count": int(count)}
        for (name, count) in rule_counter.most_common(10)
    ]

    notes: list[str] = []
    if top_triggered_rules:
        top = top_triggered_rules[0]
        notes.append(
            f"Most common previous trigger: {top['rule']} ({top['count']} of {sample_size} matched decisions)."
        )
    if sample_size >= 8:
        notes.append(
            f"Previous matched decisions: reject={recommendation_mix.get('reject', 0)}, approve={recommendation_mix.get('approve', 0)}, query={recommendation_mix.get('query', 0)} (reject rate {reject_rate}%)."
        )

    learning_alert = bool(sample_size >= 8 and reject_rate >= 60.0 and top_triggered_rules)

    return {
        "sample_size": sample_size,
        "matched_claim_ids": _dedupe(matched_claim_ids)[:20],
        "top_triggered_rules": top_triggered_rules,
        "recommendation_mix": recommendation_mix,
        "reject_rate": reject_rate,
        "learning_alert": learning_alert,
        "notes": _dedupe(notes)[:10],
    }
def _llm_fields(ctx: dict[str, Any]) -> tuple[dict[str, str], float | None]:
    targets = _resolve_structuring_llm_targets()
    if not targets:
        raise ClaimStructuringError("DEEPSEEK_API_KEY / OPENAI_API_KEY not configured")

    prompt = (
        "You are a medical-claim data segregation engine. Return strict JSON only.\n"
        "Never output raw JSON fragments from OCR, file names, MIME values, timestamps, or paths.\n"
        "Use '-' for unknown values.\n"
        "Capture medicines as complete lines (do not split one medicine into many tiny tokens).\n"
        "Capture treating_doctor and treating_doctor_registration_number whenever present.\n"
        "For investigation_finding_in_details include lab name, test, value, and reference range when available.\n"
        "Segregation: complaints must go only in complaints; objective admission/stay observations must go only in findings.\n"
        "Ignore insurance-history/claim-form checkbox text such as 'Previously covered by any other Mediclaim', 'Self inflicted road traffic accident', or 'Substance abuse/alcohol consumption' unless supported by the actual admission/discharge clinical record.\n"
        "For maternity claims, preserve pregnancy/labour/LSCS/caesarean/delivery diagnosis, complaints, fetal findings, vitals, and procedure details.\n"
        "Map dates strictly: admission->doa, discharge->dod.\n"
        "Conclusion fields: conclusion and recommendation must be concise and decision-ready.\n"
        "Exact keys required: " + ", ".join(FIELD_KEYS) + "\n\n"
        + json.dumps(ctx, ensure_ascii=False, default=str)
    )
    parsed: dict[str, Any] | None = None
    errors: list[str] = []
    for target in targets:
        provider = _txt(target.get("provider")) or "llm"
        base_url = _txt(target.get("base_url")).rstrip("/")
        api_key = _txt(target.get("api_key"))
        models = target.get("models") if isinstance(target.get("models"), list) else []
        if not base_url or not api_key or not models:
            continue
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        url = f"{base_url}/chat/completions"

        for model in models:
            model_name = _txt(model)
            if not model_name:
                continue
            request_payload = {
                "model": model_name,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "Return strict JSON only."},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.0,
            }
            try:
                with httpx.Client(timeout=120.0) as client:
                    response = client.post(url, headers=headers, json=request_payload)
                    response.raise_for_status()
                body = response.json()
                parsed = _json_obj(_extract_chat_completion_text(body))
                if isinstance(parsed, dict):
                    break
                errors.append(f"{provider}:{model_name}: invalid_json")
            except Exception as exc:
                errors.append(f"{provider}:{model_name}: {exc}")

        if isinstance(parsed, dict):
            break

    if not isinstance(parsed, dict):
        raise ClaimStructuringError(f"LLM structured segregation failed: {errors[:3] or ['no_model_available']}")

    out: dict[str, str] = {}
    for key in FIELD_KEYS:
        out[key] = _txt(parsed.get(key)) or "-"

    confidence = None
    raw_conf = parsed.get("confidence")
    if raw_conf is not None:
        try:
            confidence = float(raw_conf)
            if confidence > 1 and confidence <= 100:
                confidence = confidence / 100.0
            if confidence < 0 or confidence > 1:
                confidence = None
        except (TypeError, ValueError):
            confidence = None

    return out, confidence


def sync_clean_provider_registry_for_claim(
    db: Session,
    claim_id: UUID,
    external_claim_id: str,
    fields: dict[str, str],
    source: str,
    confidence: float | None,
    raw_payload: dict[str, Any],
) -> bool:
    payload = _build_clean_provider_registry_payload(
        claim_id=claim_id,
        external_claim_id=external_claim_id,
        fields=fields,
        source=source,
        confidence=confidence,
        raw_payload=raw_payload,
    )

    if not payload:
        db.execute(
            text(
                """
                DELETE FROM claim_provider_registry_clean
                WHERE claim_id = :claim_id
                """
            ),
            {"claim_id": str(claim_id)},
        )
        return False

    db.execute(
        text(
            """
            INSERT INTO claim_provider_registry_clean (
                claim_id,
                external_claim_id,
                hospital_name,
                treating_doctor,
                treating_doctor_registration_number,
                hospital_norm,
                doctor_norm,
                reg_norm,
                source,
                confidence,
                updated_at
            )
            VALUES (
                :claim_id,
                :external_claim_id,
                :hospital_name,
                :treating_doctor,
                :treating_doctor_registration_number,
                :hospital_norm,
                :doctor_norm,
                :reg_norm,
                :source,
                :confidence,
                NOW()
            )
            ON CONFLICT (claim_id)
            DO UPDATE SET
                external_claim_id = EXCLUDED.external_claim_id,
                hospital_name = EXCLUDED.hospital_name,
                treating_doctor = EXCLUDED.treating_doctor,
                treating_doctor_registration_number = EXCLUDED.treating_doctor_registration_number,
                hospital_norm = EXCLUDED.hospital_norm,
                doctor_norm = EXCLUDED.doctor_norm,
                reg_norm = EXCLUDED.reg_norm,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                updated_at = NOW()
            """
        ),
        payload,
    )
    return True


def _persist(db: Session, claim_id: UUID, external_claim_id: str, fields: dict[str, str], source: str, confidence: float | None, actor_id: str, raw_payload: dict[str, Any]) -> dict[str, Any]:
    row = db.execute(
        text(
            """
            INSERT INTO claim_structured_data (
                claim_id, external_claim_id, company_name, claim_type, insured_name, hospital_name,
                treating_doctor, treating_doctor_registration_number, doa, dod, diagnosis, complaints,
                findings, investigation_finding_in_details, medicine_used, high_end_antibiotic_for_rejection,
                deranged_investigation, claim_amount, conclusion, recommendation, raw_payload,
                source, confidence, created_by, updated_at
            )
            VALUES (
                :claim_id, :external_claim_id, :company_name, :claim_type, :insured_name, :hospital_name,
                :treating_doctor, :treating_doctor_registration_number, :doa, :dod, :diagnosis, :complaints,
                :findings, :investigation_finding_in_details, :medicine_used, :high_end_antibiotic_for_rejection,
                :deranged_investigation, :claim_amount, :conclusion, :recommendation, CAST(:raw_payload AS jsonb),
                :source, :confidence, :created_by, NOW()
            )
            ON CONFLICT (claim_id)
            DO UPDATE SET
                external_claim_id = EXCLUDED.external_claim_id,
                company_name = EXCLUDED.company_name,
                claim_type = EXCLUDED.claim_type,
                insured_name = EXCLUDED.insured_name,
                hospital_name = EXCLUDED.hospital_name,
                treating_doctor = EXCLUDED.treating_doctor,
                treating_doctor_registration_number = EXCLUDED.treating_doctor_registration_number,
                doa = EXCLUDED.doa,
                dod = EXCLUDED.dod,
                diagnosis = EXCLUDED.diagnosis,
                complaints = EXCLUDED.complaints,
                findings = EXCLUDED.findings,
                investigation_finding_in_details = EXCLUDED.investigation_finding_in_details,
                medicine_used = EXCLUDED.medicine_used,
                high_end_antibiotic_for_rejection = EXCLUDED.high_end_antibiotic_for_rejection,
                deranged_investigation = EXCLUDED.deranged_investigation,
                claim_amount = EXCLUDED.claim_amount,
                conclusion = EXCLUDED.conclusion,
                recommendation = EXCLUDED.recommendation,
                raw_payload = EXCLUDED.raw_payload,
                source = EXCLUDED.source,
                confidence = EXCLUDED.confidence,
                created_by = EXCLUDED.created_by,
                updated_at = NOW()
            RETURNING claim_id, external_claim_id, company_name, claim_type, insured_name, hospital_name,
                treating_doctor, treating_doctor_registration_number, doa, dod, diagnosis, complaints,
                findings, investigation_finding_in_details, medicine_used, high_end_antibiotic_for_rejection,
                deranged_investigation, claim_amount, conclusion, recommendation, raw_payload,
                source, confidence, created_at, updated_at
            """
        ),
        {
            "claim_id": str(claim_id),
            "external_claim_id": external_claim_id,
            "company_name": fields.get("company_name"),
            "claim_type": fields.get("claim_type"),
            "insured_name": fields.get("insured_name"),
            "hospital_name": fields.get("hospital_name"),
            "treating_doctor": fields.get("treating_doctor"),
            "treating_doctor_registration_number": fields.get("treating_doctor_registration_number"),
            "doa": fields.get("doa"),
            "dod": fields.get("dod"),
            "diagnosis": fields.get("diagnosis"),
            "complaints": fields.get("complaints"),
            "findings": fields.get("findings"),
            "investigation_finding_in_details": fields.get("investigation_finding_in_details"),
            "medicine_used": fields.get("medicine_used"),
            "high_end_antibiotic_for_rejection": fields.get("high_end_antibiotic_for_rejection"),
            "deranged_investigation": fields.get("deranged_investigation"),
            "claim_amount": fields.get("claim_amount"),
            "conclusion": fields.get("conclusion"),
            "recommendation": fields.get("recommendation"),
            "raw_payload": json.dumps(raw_payload, ensure_ascii=False, default=str),
            "source": _txt(source)[:40],
            "confidence": confidence,
            "created_by": actor_id,
        },
    ).mappings().one()

    clean_registry_synced = False
    clean_registry_error = ""
    try:
        clean_registry_synced = sync_clean_provider_registry_for_claim(
            db=db,
            claim_id=claim_id,
            external_claim_id=external_claim_id,
            fields=fields,
            source=source,
            confidence=confidence,
            raw_payload=raw_payload,
        )
    except SQLAlchemyError as exc:
        clean_registry_error = str(exc)

    db.execute(
        text(
            """
            INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
            VALUES (:claim_id, 'user', :actor_id, 'claim_structured_data_saved', CAST(:event_payload AS jsonb))
            """
        ),
        {
            "claim_id": str(claim_id),
            "actor_id": actor_id,
            "event_payload": json.dumps(
                {
                    "source": source,
                    "confidence": confidence,
                    "fields_saved": FIELD_KEYS,
                    "clean_provider_registry_synced": clean_registry_synced,
                    "clean_provider_registry_sync_error": clean_registry_error,
                },
                ensure_ascii=False,
            ),
        },
    )

    return dict(row)


def _to_response(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    out["raw_payload"] = _safe_json(out.get("raw_payload"), {})
    for key in FIELD_KEYS:
        out[key] = _txt(out.get(key)) or "-"
    out["source"] = _txt(out.get("source")) or "heuristic"
    return out


def get_claim_structured_data(db: Session, claim_id: UUID) -> dict[str, Any]:
    _ensure_table(db)
    row = db.execute(
        text(
            """
            SELECT claim_id, external_claim_id, company_name, claim_type, insured_name, hospital_name,
                   treating_doctor, treating_doctor_registration_number, doa, dod, diagnosis, complaints,
                   findings, investigation_finding_in_details, medicine_used, high_end_antibiotic_for_rejection,
                   deranged_investigation, claim_amount, conclusion, recommendation, raw_payload,
                   source, confidence, created_at, updated_at
            FROM claim_structured_data
            WHERE claim_id = :claim_id
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if row is None:
        raise ClaimStructuredDataNotFoundError("structured data not found")
    return _to_response(dict(row))


def generate_claim_structured_data(db: Session, claim_id: UUID, actor_id: str, use_llm: bool = True, force_refresh: bool = True, require_llm: bool = False) -> dict[str, Any]:
    _ensure_table(db)
    if not force_refresh:
        try:
            return get_claim_structured_data(db, claim_id)
        except ClaimStructuredDataNotFoundError:
            pass

    try:
        ctx = _load_context(db, claim_id)
    except SQLAlchemyError as exc:
        raise ClaimStructuringError(f"failed to load claim context: {exc}") from exc

    claim = ctx.get("claim") or {}
    external_claim_id = _txt(claim.get("external_claim_id"))
    if not external_claim_id:
        raise ClaimStructuringError("external_claim_id missing")

    llm_error = ""
    source = "heuristic"
    confidence = None

    heuristic_fields = _heuristic_fields(ctx)
    fields = heuristic_fields

    if use_llm:
        try:
            llm_fields, confidence = _llm_fields(ctx)
            fields = dict(llm_fields)
            fields["claim_amount"] = heuristic_fields.get("claim_amount") or fields.get("claim_amount") or "-"
            source = "verifai_structured"
        except Exception as exc:
            llm_error = str(exc)
            if require_llm:
                raise ClaimStructuringError(f"VerifAI structured extraction failed: {llm_error}") from exc
            fields = heuristic_fields
            source = "heuristic_fallback"
    else:
        fields = heuristic_fields

    fields = _postprocess_structured_fields(fields)

    fraud_check: dict[str, Any] = {"suspicious": False, "matched_claims": [], "notes": []}
    if not STRICT_RULE_BASED_MODE:
        try:
            fraud_check = _fraud_pattern_compare(db, claim_id, fields, claim)
            fields = _apply_fraud_signals_to_fields(fields, fraud_check)
            if bool(fraud_check.get("suspicious")):
                source = source + "+fraud_check"
        except Exception as exc:
            fraud_check = {"suspicious": False, "matched_claims": [], "notes": [f"fraud_check_error: {exc}"]}

    hospital_trend: dict[str, Any] = {
        "hospital_name": _txt(fields.get("hospital_name")),
        "trend_alert": False,
        "notes": [],
        "overall": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
        "recent": {"window": 0, "total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
        "doctor_recent": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
        "recent_claim_ids": [],
        "top_diagnosis": [],
    }
    if not STRICT_RULE_BASED_MODE:
        try:
            hospital_trend = _hospital_trend_compare(db, claim_id, fields)
            fields = _apply_hospital_trend_signals_to_fields(fields, hospital_trend)
            if bool(hospital_trend.get("trend_alert")) and "hospital_learning" not in source:
                source = source + "+hospital_learning"
        except Exception as exc:
            hospital_trend = {
                "hospital_name": _txt(fields.get("hospital_name")),
                "trend_alert": False,
                "notes": [f"hospital_trend_error: {exc}"],
                "overall": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
                "recent": {"window": 0, "total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
                "doctor_recent": {"total": 0, "reject": 0, "approve": 0, "query": 0, "unknown": 0, "reject_rate": 0.0, "query_rate": 0.0},
                "recent_claim_ids": [],
                "top_diagnosis": [],
            }

    previous_rule_learning: dict[str, Any] = {
        "sample_size": 0,
        "matched_claim_ids": [],
        "top_triggered_rules": [],
        "recommendation_mix": {"reject": 0, "approve": 0, "query": 0, "unknown": 0},
        "reject_rate": 0.0,
        "learning_alert": False,
        "notes": [],
    }
    if not STRICT_RULE_BASED_MODE:
        try:
            previous_rule_learning = _previous_rule_learning(db, claim_id, fields)
            if bool(previous_rule_learning.get("learning_alert")) and "rule_learning" not in source:
                source = source + "+rule_learning"
        except Exception as exc:
            previous_rule_learning = {
                "sample_size": 0,
                "matched_claim_ids": [],
                "top_triggered_rules": [],
                "recommendation_mix": {"reject": 0, "approve": 0, "query": 0, "unknown": 0},
                "reject_rate": 0.0,
                "learning_alert": False,
                "notes": [f"rule_learning_error: {exc}"],
            }

    high_end_assessment: dict[str, Any] = {
        "matched": [],
        "label": _txt(fields.get("high_end_antibiotic_for_rejection")) or "No",
        "justification_present": True,
        "missing_evidence": [],
        "used_db_catalog": False,
        "used_api_lookup": False,
        "api_candidates_checked": 0,
    }
    try:
        high_end_assessment = _assess_high_end_antibiotic_justification(db, fields, ctx)
        fields = _apply_high_end_antibiotic_guardrail(fields, high_end_assessment)
        if bool(high_end_assessment.get("used_db_catalog")) and "medicine_db_lookup" not in source:
            source = source + "+medicine_db_lookup"
        if bool(high_end_assessment.get("used_api_lookup")) and "medicine_api_lookup" not in source:
            source = source + "+medicine_api_lookup"
    except Exception as exc:
        high_end_assessment = {
            "matched": [],
            "label": _txt(fields.get("high_end_antibiotic_for_rejection")) or "No",
            "justification_present": True,
            "missing_evidence": [],
            "used_db_catalog": False,
            "used_api_lookup": False,
            "api_candidates_checked": 0,
            "error": str(exc),
        }

    learning_signals = {
        "fraud_similarity_alert": bool(fraud_check.get("suspicious")),
        "hospital_trend_alert": bool(hospital_trend.get("trend_alert")),
        "previous_rule_learning_alert": bool(previous_rule_learning.get("learning_alert")),
        "high_end_antibiotic_alert": bool((high_end_assessment.get("matched") or []) and not bool(high_end_assessment.get("justification_present"))),
        "previous_rule_sample_size": int(previous_rule_learning.get("sample_size") or 0),
        "previous_rule_reject_rate": float(previous_rule_learning.get("reject_rate") or 0.0),
        "previous_rule_top_hits": (previous_rule_learning.get("top_triggered_rules") or [])[:5],
        "risk_level": "high" if (
            bool(fraud_check.get("suspicious"))
            or bool(hospital_trend.get("trend_alert"))
            or bool(previous_rule_learning.get("learning_alert"))
            or bool((high_end_assessment.get("matched") or []) and not bool(high_end_assessment.get("justification_present")))
        ) else "normal",
    }

    raw_payload = {
        "context": ctx,
        "llm_error": llm_error,
        "fraud_check": fraud_check,
        "hospital_trend": hospital_trend,
        "previous_rule_learning": previous_rule_learning,
        "high_end_assessment": high_end_assessment,
        "learning_signals": learning_signals,
        "generated_at": datetime.utcnow().isoformat(),
    }

    try:
        saved = _persist(
            db=db,
            claim_id=claim_id,
            external_claim_id=external_claim_id,
            fields=fields,
            source=source,
            confidence=confidence,
            actor_id=actor_id,
            raw_payload=raw_payload,
        )
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        raise ClaimStructuringError(f"failed to save structured data: {exc}") from exc

    return _to_response(saved)






































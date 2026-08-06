import json
import re
from collections import Counter
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

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


def _norm_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", _txt(value).lower())


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
            if not k or not any(k == a or k.find(a) >= 0 or a.find(k) >= 0 for a in alias_norm):
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
            if not any(k == a or k.find(a) >= 0 or a.find(k) >= 0 for a in alias_keys):
                continue
            if isinstance(value, list):
                for item in value:
                    t = _txt(item)
                    if t:
                        out.append(t)
            else:
                t = _txt(value)
                if t:
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

def _load_context(db: Session, claim_id: UUID) -> dict[str, Any]:
    claim = db.execute(
        text(
            """
            SELECT c.id, c.external_claim_id, c.patient_name, c.patient_identifier,
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
            SELECT recommendation, decision_payload, rule_hits, generated_at
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
            entity_docs.append(entities)
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
        "latest_report_text": _strip_html((latest_report or {}).get("report_markdown")) if latest_report else "",
    }


def _heuristic_fields(ctx: dict[str, Any]) -> dict[str, str]:
    claim = ctx.get("claim") or {}
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

    decision_payload = _safe_json(decision.get("decision_payload"), {}) if isinstance(decision, dict) else {}
    recommendation = _first(
        decision.get("recommendation"),
        decision_payload.get("recommendation"),
        _find_values(entity_docs, ["final_recommendation", "recommendation"], 1)[0] if _find_values(entity_docs, ["final_recommendation", "recommendation"], 1) else "",
        default="Claim kept in query for medical clarification.",
    )
    if recommendation.lower() in {"approve", "admissible", "payable"}:
        recommendation = "Claim is payable."
    elif recommendation.lower() in {"reject", "inadmissible", "not justified"}:
        recommendation = "Claim is recommended for rejection."
    findings_text = _clean_findings_text(
        _merge_multiline(
            _find_values(entity_docs, ["major_diagnostic_finding", "clinical_findings", "diagnostic_finding", "hospital_finding", "summary"], 8),
            "\n".join(evidence_lines[:40]),
            80,
        )
    )

    fields = {
        "company_name": _first(_find_values(entity_docs, ["company_name", "insurance_company", "insurer", "tpa", "insurance_tpa", "payer_name"], 1)[0] if _find_values(entity_docs, ["company_name", "insurance_company", "insurer", "tpa", "insurance_tpa", "payer_name"], 1) else "", legacy.get("company_name"), "Medi Assist Insurance TPA Pvt. Ltd."),
        "claim_type": _first(_find_values(entity_docs, ["claim_type", "case_type"], 1)[0] if _find_values(entity_docs, ["claim_type", "case_type"], 1) else "", legacy.get("claim_type"), "-"),
        "insured_name": _first(_find_values(entity_docs, ["insured_name", "name", "patient_name", "insured", "beneficiary", "policy_holder_name"], 1)[0] if _find_values(entity_docs, ["insured_name", "name", "patient_name", "insured", "beneficiary", "policy_holder_name"], 1) else "", legacy.get("benef_name"), claim.get("patient_name"), "-"),
        "hospital_name": _first(_find_values(entity_docs, ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"], 1)[0] if _find_values(entity_docs, ["hospital_name", "hospital", "provider_hospital", "treating_hospital", "facility_name"], 1) else "", legacy.get("hospital_name"), "-"),
        "treating_doctor": _first(doctor_from_entities, doctor_from_blob, legacy.get("treating_doctor"), "-"),
        "treating_doctor_registration_number": _first(registration_from_entities, registration_from_blob, legacy.get("doctor_registration_number"), "-"),
        "doa": _first(legacy.get("doa_date"), legacy.get("date_of_admission"), legacy.get("admission_date"), _find_values(entity_docs, ["doa", "doa_date", "date_of_admission", "admission_date"], 1)[0] if _find_values(entity_docs, ["doa", "doa_date", "date_of_admission", "admission_date"], 1) else ""),
        "dod": _first(legacy.get("dod_date"), legacy.get("date_of_discharge"), legacy.get("discharge_date"), _find_values(entity_docs, ["dod", "dod_date", "date_of_discharge", "discharge_date"], 1)[0] if _find_values(entity_docs, ["dod", "dod_date", "date_of_discharge", "discharge_date"], 1) else ""),
        "diagnosis": _first(_find_values(entity_docs, ["diagnosis", "final_diagnosis", "provisional_diagnosis", "primary_diagnosis"], 1)[0] if _find_values(entity_docs, ["diagnosis", "final_diagnosis", "provisional_diagnosis", "primary_diagnosis"], 1) else "", legacy.get("diagnosis"), "-"),
        "complaints": _first(_find_values(entity_docs, ["chief_complaints", "chief_complaint", "presenting_complaints", "complaints"], 1)[0] if _find_values(entity_docs, ["chief_complaints", "chief_complaint", "presenting_complaints", "complaints"], 1) else "", legacy.get("complaints"), "-"),
        "findings": findings_text,
        "investigation_finding_in_details": "\n".join(investigations[:120]) if investigations else "No investigation reports available.",
        "medicine_used": medicines,
        "high_end_antibiotic_for_rejection": _high_end_antibiotic("\n".join([medicines, "\n".join(investigations[:120]), "\n".join(evidence_lines[:120])])),
        "deranged_investigation": _deranged(investigations),
        "claim_amount": _first(_parse_amount(legacy.get("claim_amount")), _parse_amount(legacy.get("claimed_amount")), _parse_amount(legacy.get("bill_amount")), _parse_amount(_find_values(entity_docs, ["claim_amount", "claimed_amount", "bill_amount", "amount_claimed"], 1)[0] if _find_values(entity_docs, ["claim_amount", "claimed_amount", "bill_amount", "amount_claimed"], 1) else ""), "-"),
        "conclusion": _first(decision_payload.get("conclusion") if isinstance(decision_payload, dict) else "", _find_values(entity_docs, ["detailed_conclusion", "conclusion", "rationale", "decision"], 1)[0] if _find_values(entity_docs, ["detailed_conclusion", "conclusion", "rationale", "decision"], 1) else "", "Claim reviewed based on available records."),
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

        if llm_val == "-":
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
    if not settings.openai_api_key:
        raise ClaimStructuringError("OPENAI_API_KEY not configured")

    model = str(settings.openai_rag_model or settings.openai_model or "gpt-4o-mini").strip() or "gpt-4o-mini"
    base_url = settings.openai_base_url.rstrip("/") if settings.openai_base_url else "https://api.openai.com/v1"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}", "Content-Type": "application/json"}

    prompt = (
        "You are a medical-claim data segregation engine. Return strict JSON only.\n"
        "Never output raw JSON fragments from OCR, file names, MIME values, timestamps, or paths.\n"
        "Use '-' for unknown values.\n"
        "Capture medicines as complete lines (do not split one medicine into many tiny tokens).\n"
        "Capture treating_doctor and treating_doctor_registration_number whenever present.\n"
        "For investigation_finding_in_details include lab name, test, value, and reference range when available.\n"
        "Segregation: complaints must go only in complaints; objective admission/stay observations must go only in findings.\n"
        "Map dates strictly: admission->doa, discharge->dod.\n"
        "Conclusion fields: conclusion and recommendation must be concise and decision-ready.\n"
        "Exact keys required: " + ", ".join(FIELD_KEYS) + "\n\n"
        + json.dumps(ctx, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "input": [
            {"role": "system", "content": [{"type": "input_text", "text": "Return strict JSON only."}]},
            {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        ],
    }

    try:
        with httpx.Client(timeout=120.0) as client:
            response = client.post(f"{base_url}/responses", headers=headers, json=payload)
            response.raise_for_status()
        body = response.json()
    except Exception as exc:
        raise ClaimStructuringError(f"LLM structured segregation failed: {exc}") from exc

    parsed = _json_obj(_extract_openai_text(body))
    if not isinstance(parsed, dict):
        raise ClaimStructuringError("LLM structured segregation returned invalid JSON")

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
            "source": source,
            "confidence": confidence,
            "created_by": actor_id,
        },
    ).mappings().one()

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
            "event_payload": json.dumps({"source": source, "confidence": confidence, "fields_saved": FIELD_KEYS}, ensure_ascii=False),
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


def generate_claim_structured_data(db: Session, claim_id: UUID, actor_id: str, use_llm: bool = True, force_refresh: bool = True) -> dict[str, Any]:
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
            fields = _merge_llm_with_heuristic_fields(llm_fields, heuristic_fields)
            source = "llm+heuristic"
        except Exception as exc:
            llm_error = str(exc)
            fields = heuristic_fields
            source = "heuristic_fallback"
    else:
        fields = heuristic_fields

    fraud_check: dict[str, Any] = {"suspicious": False, "matched_claims": [], "notes": []}
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

    learning_signals = {
        "fraud_similarity_alert": bool(fraud_check.get("suspicious")),
        "hospital_trend_alert": bool(hospital_trend.get("trend_alert")),
        "previous_rule_learning_alert": bool(previous_rule_learning.get("learning_alert")),
        "previous_rule_sample_size": int(previous_rule_learning.get("sample_size") or 0),
        "previous_rule_reject_rate": float(previous_rule_learning.get("reject_rate") or 0.0),
        "previous_rule_top_hits": (previous_rule_learning.get("top_triggered_rules") or [])[:5],
        "risk_level": "high" if (
            bool(fraud_check.get("suspicious"))
            or bool(hospital_trend.get("trend_alert"))
            or bool(previous_rule_learning.get("learning_alert"))
        ) else "normal",
    }

    raw_payload = {
        "context": ctx,
        "llm_error": llm_error,
        "fraud_check": fraud_check,
        "hospital_trend": hospital_trend,
        "previous_rule_learning": previous_rule_learning,
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





















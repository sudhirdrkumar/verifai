from __future__ import annotations

import json
import re
import time
from calendar import monthrange
from datetime import date, datetime
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.checklist import (
    ChecklistDecision,
    ChecklistEntry,
    ChecklistLatestResponse,
    ChecklistRunResponse,
)
from app.services.legacy_checklist_source import get_checklist_catalog
from app.services.ml_claim_model import (
    HYBRID_LABEL_TYPE,
    predict_claim_recommendation,
    recommendation_to_feedback_label,
    upsert_feedback_label,
)


class ClaimNotFoundError(Exception):
    pass

_OPENAI_MERGED_AUDIT_DISCLAIMER = (
    "This is an AI-assisted review and should be validated by a qualified medical professional."
)
_OPENAI_MERGED_RATE_LIMIT_MARKER = "OPENAI_RATE_LIMITED"
_OPENAI_MERGED_RATE_LIMIT_COOLDOWN_SECONDS = 300
_openai_merged_rate_limited_until = 0.0
STRICT_RULE_BASED_MODE = True

_EXPIRY_MARKER_PATTERN = re.compile(
    r"\b(?:exp(?:iry)?(?:\s*(?:date|dt))?|date\s+of\s+expiry|use\s+before|expires?\s+on?)\b",
    flags=re.I,
)
_EXPIRY_VALUE_PATTERNS = [
    re.compile(r"\b\d{4}[/-]\d{1,2}[/-]\d{1,2}\b"),
    re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b"),
    re.compile(r"\b\d{1,2}[/-]\d{2,4}\b"),
    re.compile(
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*[\s/-]+\d{2,4}\b",
        flags=re.I,
    ),
    re.compile(r"\b(?:0[1-9]|1[0-2])(?:\d{2}|\d{4})\b"),
]
_GSTIN_PATTERN = re.compile(r"\b[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]\b", flags=re.I)
_GST_INVALID_ERROR_CODES = {"INVALID_INPUT", "INVALID_GSTIN", "GSTIN_NOT_FOUND", "NOT_FOUND", "INVALID_UID"}
_GST_VENDOR_KEYS = (
    "pharmacy_name",
    "chemist_name",
    "medical_store_name",
    "shop_name",
    "supplier_name",
    "vendor_name",
    "vendor",
)
_GSTIN_LEGACY_KEYS = (
    "vendor_gstin",
    "vendor_gst",
    "vendor_gst_no",
    "vendor_gst_number",
    "pharmacy_gstin",
    "chemist_gstin",
    "gstin",
    "gst_no",
    "gst_number",
)
_GST_NAME_STOPWORDS = {
    "pvt",
    "private",
    "ltd",
    "limited",
    "llp",
    "and",
    "the",
    "india",
    "co",
    "company",
    "enterprises",
    "enterprise",
    "traders",
    "trader",
    "store",
}
_GST_BUSINESS_HINT_WORDS = {
    "pharmacy",
    "pharma",
    "medical",
    "medicals",
    "chemist",
    "store",
    "hospital",
    "clinic",
    "healthcare",
    "health",
    "care",
    "diagnostic",
    "diagnostics",
    "lab",
    "laboratory",
    "labs",
    "enterprise",
    "enterprises",
    "traders",
    "trader",
    "agency",
    "agencies",
    "mart",
}
_GST_VERIFY_CACHE: dict[str, dict[str, Any]] = {}
_GST_VENDOR_IGNORE_PATTERNS = (
    re.compile(r"\brightworks\b", flags=re.I),
    re.compile(r"\bteamrightworks\b", flags=re.I),
)
_GST_EMPTY_MARKERS = {
    "",
    "na",
    "n a",
    "none",
    "null",
    "nil",
    "not available",
    "not applicable",
    "unknown",
    "-",
    "--",
    "---",
}


def _normalize_year(year: int) -> int:
    if year < 100:
        return 2000 + year
    return year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _parse_expiry_value(raw_value: str) -> date | None:
    value = str(raw_value or "").strip().strip(".,;:()[]{}")
    if not value:
        return None

    month_name_match = re.fullmatch(r"(?i)([A-Za-z]{3,9})[\s/-]+(\d{2,4})", value)
    if month_name_match:
        month_token = month_name_match.group(1)
        year_val = _normalize_year(int(month_name_match.group(2)))
        month_num = None
        for fmt in ("%b", "%B"):
            try:
                month_num = datetime.strptime(month_token[:3] if fmt == "%b" else month_token, fmt).month
                break
            except ValueError:
                continue
        if month_num:
            last_day = monthrange(year_val, month_num)[1]
            return _safe_date(year_val, month_num, last_day)

    m = re.fullmatch(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", value)
    if m:
        year_val, month_val, day_val = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return _safe_date(year_val, month_val, day_val)

    m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", value)
    if m:
        first, second, year_raw = int(m.group(1)), int(m.group(2)), int(m.group(3))
        year_val = _normalize_year(year_raw)
        # Default to DD/MM/YYYY for Indian medical bills; fallback to MM/DD/YYYY when necessary.
        day_val, month_val = first, second
        if first <= 12 and second > 12:
            month_val, day_val = first, second
        return _safe_date(year_val, month_val, day_val)

    m = re.fullmatch(r"(\d{1,2})[/-](\d{2,4})", value)
    if m:
        month_val, year_raw = int(m.group(1)), int(m.group(2))
        if 1 <= month_val <= 12:
            year_val = _normalize_year(year_raw)
            last_day = monthrange(year_val, month_val)[1]
            return _safe_date(year_val, month_val, last_day)
        return None

    m = re.fullmatch(r"(0[1-9]|1[0-2])(\d{2}|\d{4})", value)
    if m:
        month_val = int(m.group(1))
        year_val = _normalize_year(int(m.group(2)))
        last_day = monthrange(year_val, month_val)[1]
        return _safe_date(year_val, month_val, last_day)

    return None


def _detect_expired_medicine_hits(text_value: str, as_of: date) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()

    for line in re.split(r"\r\n|\r|\n", str(text_value or "")):
        raw_line = str(line or "").strip()
        if not raw_line:
            continue
        marker = _EXPIRY_MARKER_PATTERN.search(raw_line)
        if not marker:
            continue

        tail = raw_line[marker.end() : marker.end() + 40]
        for value_pattern in _EXPIRY_VALUE_PATTERNS:
            for value_match in value_pattern.finditer(tail):
                expiry_token = value_match.group(0).strip()
                expiry_date = _parse_expiry_value(expiry_token)
                if not expiry_date or expiry_date >= as_of:
                    continue
                key = (raw_line.lower(), expiry_date.isoformat())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                hits.append(
                    {
                        "line": raw_line[:260],
                        "expiry_text": expiry_token,
                        "expiry_date": expiry_date.isoformat(),
                    }
                )

    return hits


def _build_expired_medicine_guardrail_entry(
    expired_hits: list[dict[str, str]],
    as_of_date: date,
) -> ChecklistEntry:
    triggered = bool(expired_hits)
    if triggered:
        samples = [
            f"{item.get('expiry_text')} ({item.get('expiry_date')})"
            for item in expired_hits[:3]
            if str(item.get("expiry_text") or "").strip()
        ]
        sample_text = "; ".join(samples) if samples else "expired date found in medicine bill"
        note = (
            "Expired medicine check triggered: medicine bill contains expiry date(s) older than "
            f"{as_of_date.isoformat()} ({sample_text})."
        )
    else:
        note = "Checklist condition not triggered."

    return ChecklistEntry(
        code="R017",
        name="Medicine bill contains expired medicine entries",
        decision=ChecklistDecision.query,
        severity="SOFT_QUERY",
        source="openai_claim_rules",
        matched_scope=True,
        triggered=triggered,
        status="QUERY" if triggered else "NOT_MET",
        missing_evidence=[],
        note=note,
    )


def _text(value: Any) -> str:
    return str(value or "").strip()


def _safe_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def _normalize_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", " ", _text(value).lower()).strip()
    if not normalized:
        return ""
    tokens = [tok for tok in normalized.split(" ") if tok and tok not in _GST_NAME_STOPWORDS]
    return " ".join(tokens)


def _name_tokens(value: str) -> set[str]:
    return {tok for tok in _normalize_name(value).split(" ") if tok}


def _edit_distance_le_one(left: str, right: str) -> bool:
    a = str(left or "")
    b = str(right or "")
    if a == b:
        return True
    la = len(a)
    lb = len(b)
    if abs(la - lb) > 1:
        return False

    if la == lb:
        mismatches = sum(1 for i in range(la) if a[i] != b[i])
        return mismatches <= 1

    if la > lb:
        a, b = b, a
        la, lb = lb, la

    i = 0
    j = 0
    edits = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        edits += 1
        if edits > 1:
            return False
        j += 1
    return True


def _is_weak_vendor_name_for_match(value: str) -> bool:
    tokens = _name_tokens(value)
    if not tokens:
        return True
    if _looks_like_person_name(value):
        return True
    if not any(tok in _GST_BUSINESS_HINT_WORDS for tok in tokens):
        # If it lacks business indicators, avoid hard mismatch rejects.
        return True
    if len(tokens) > 1:
        return False
    only = next(iter(tokens))
    return len(only) <= 4


def _looks_like_person_name(value: str) -> bool:
    raw = _text(value)
    if not raw:
        return False
    if re.search(r"\d", raw):
        return False
    norm_tokens = [tok for tok in _normalize_name(raw).split(" ") if tok]
    if len(norm_tokens) < 2 or len(norm_tokens) > 4:
        return False
    if any(tok in _GST_BUSINESS_HINT_WORDS for tok in norm_tokens):
        return False
    # Short multi-token alpha names are likely proprietor/person names.
    if all(re.fullmatch(r"[a-z]{2,15}", tok) for tok in norm_tokens):
        return True
    return False


def _names_match(left: str, right: str) -> bool:
    a = _normalize_name(left)
    b = _normalize_name(right)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a) >= 5 and a in b:
        return True
    if len(b) >= 5 and b in a:
        return True
    ta = _name_tokens(a)
    tb = _name_tokens(b)
    if not ta or not tb:
        return False
    overlap = ta.intersection(tb)
    if len(overlap) >= 2:
        return True
    ratio = len(overlap) / float(max(len(ta), len(tb)))
    if ratio >= 0.6 and len(overlap) >= 1:
        return True
    for token_a in ta:
        if len(token_a) < 4:
            continue
        for token_b in tb:
            if len(token_b) < 4:
                continue
            if _edit_distance_le_one(token_a, token_b):
                return True
    return False


def _is_non_pharmacy_vendor_name(value: str) -> bool:
    candidate = _text(value)
    if not candidate:
        return True
    norm = _normalize_phrase(candidate)
    if norm in _GST_EMPTY_MARKERS:
        return True
    if any(pat.search(candidate) for pat in _GST_VENDOR_IGNORE_PATTERNS):
        return True
    low = candidate.lower()
    if re.fullmatch(r"(?:https?://)?(?:www\.)?[a-z0-9][a-z0-9.-]*\.[a-z]{2,}(?:/.*)?", low):
        return True
    return False


def _extract_vendor_name(
    legacy_payload: dict[str, Any],
    claim_row: dict[str, Any],
    extracted_vendor_name: str = "",
) -> str:
    preferred = _text(extracted_vendor_name)
    if preferred and not _is_non_pharmacy_vendor_name(preferred):
        return preferred
    for key in _GST_VENDOR_KEYS:
        candidate = _text(legacy_payload.get(key))
        if candidate and not _is_non_pharmacy_vendor_name(candidate):
            return candidate
    source_channel = _text(claim_row.get("source_channel"))
    if source_channel and "." not in source_channel and "/" not in source_channel and "\\" not in source_channel and not _is_non_pharmacy_vendor_name(source_channel):
        return source_channel
    return ""


def _extract_gstin_candidates_from_legacy(legacy_payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in _GSTIN_LEGACY_KEYS:
        candidate = _text(legacy_payload.get(key)).upper()
        if candidate and _GSTIN_PATTERN.fullmatch(candidate) and candidate not in seen:
            seen.add(candidate)
            out.append(candidate)
    return out


def _extract_invalid_gstin_inputs_from_legacy(legacy_payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for key in _GSTIN_LEGACY_KEYS:
        raw = _text(legacy_payload.get(key))
        if not raw:
            continue
        raw_norm = _normalize_phrase(raw)
        if raw_norm in _GST_EMPTY_MARKERS:
            continue
        compact = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
        if not compact:
            continue
        if _GSTIN_PATTERN.fullmatch(compact):
            continue
        if compact not in seen:
            seen.add(compact)
            out.append(compact)
    return out


def _extract_gstin_candidates(text_value: str, legacy_payload: dict[str, Any]) -> list[str]:
    prioritized = _extract_gstin_candidates_from_legacy(legacy_payload)
    seen = {v for v in prioritized}
    from_text = [m.group(0).upper() for m in _GSTIN_PATTERN.finditer(str(text_value or ""))]
    for gstin in from_text:
        if gstin not in seen:
            seen.add(gstin)
            prioritized.append(gstin)
    return prioritized


def _fetch_cleartax_gstin_profile(gstin: str) -> dict[str, Any]:
    code = _text(gstin).upper()
    if not code:
        return {"gstin": "", "valid": False, "invalid_input": True, "message": "GSTIN missing"}
    cached = _GST_VERIFY_CACHE.get(code)
    if isinstance(cached, dict):
        return dict(cached)

    if not bool(getattr(settings, "cleartax_gst_verify_enabled", True)):
        result = {
            "gstin": code,
            "valid": False,
            "invalid_input": False,
            "service_error": True,
            "message": "gst verification disabled",
            "status_code": None,
            "trade_name": "",
            "legal_name": "",
            "error_code": "",
        }
        _GST_VERIFY_CACHE[code] = dict(result)
        return result

    base_url = _text(getattr(settings, "cleartax_gst_base_url", "https://cleartax.in")).rstrip("/") or "https://cleartax.in"
    timeout_sec = float(getattr(settings, "cleartax_gst_timeout_seconds", 10.0) or 10.0)
    target_url = f"{base_url}/f/compliance-report/{code}/"

    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            response = client.get(target_url, headers={"Accept": "application/json"})
    except Exception as exc:
        result = {
            "gstin": code,
            "valid": False,
            "invalid_input": False,
            "service_error": True,
            "message": f"gst service error: {exc}",
            "status_code": None,
            "trade_name": "",
            "legal_name": "",
            "error_code": "",
        }
        _GST_VERIFY_CACHE[code] = dict(result)
        return result

    status_code = int(response.status_code)
    payload: dict[str, Any] = {}
    try:
        raw = response.json()
        if isinstance(raw, dict):
            payload = raw
    except Exception:
        payload = {}

    taxpayer = payload.get("taxpayerInfo") if isinstance(payload.get("taxpayerInfo"), dict) else {}
    error_info = payload.get("errorResponse") if isinstance(payload.get("errorResponse"), dict) else {}
    error_code = _text(error_info.get("errorCode")).upper()
    error_message = _text(error_info.get("message")) or _text(taxpayer.get("errorMsg"))
    trade_name = _text(taxpayer.get("tradeNam"))
    legal_name = _text(taxpayer.get("lgnm"))
    valid = bool(_text(taxpayer.get("gstin"))) and not bool(error_code)
    invalid_input = bool(error_code in _GST_INVALID_ERROR_CODES or re.search(r"\binvalid\s+gstin\b|\binvalid\s+gstin\s*/\s*uid\b|\binvalid\s+input\b", error_message, flags=re.I))
    service_error = bool(status_code >= 500)

    result = {
        "gstin": code,
        "valid": valid,
        "invalid_input": invalid_input,
        "service_error": service_error,
        "message": error_message,
        "status_code": status_code,
        "trade_name": trade_name,
        "legal_name": legal_name,
        "error_code": error_code,
    }
    _GST_VERIFY_CACHE[code] = dict(result)
    return result


def _assess_gst_guardrail(context: dict[str, Any]) -> dict[str, Any]:
    claim_row = context.get("claim") if isinstance(context.get("claim"), dict) else {}
    legacy_payload = context.get("legacy_payload") if isinstance(context.get("legacy_payload"), dict) else {}
    full_text = _text(context.get("text"))

    vendor_name = _extract_vendor_name(
        legacy_payload,
        claim_row,
        extracted_vendor_name=_text(context.get("extracted_vendor_name")),
    )
    invalid_legacy_gst = _extract_invalid_gstin_inputs_from_legacy(legacy_payload)
    if invalid_legacy_gst:
        return {
            "triggered": True,
            "trigger_type": "invalid_gst_format",
            "reason": "invalid gst format",
            "vendor_name": vendor_name,
            "gstins_checked": [],
            "profiles": [],
            "invalid_format_values": invalid_legacy_gst[:3],
            "service_error": False,
        }

    gstins = _extract_gstin_candidates(full_text, legacy_payload)
    if not gstins:
        return {
            "triggered": False,
            "reason": "",
            "trigger_type": "",
            "vendor_name": vendor_name,
            "gstins_checked": [],
            "profiles": [],
            "invalid_format_values": [],
            "service_error": False,
        }

    profiles: list[dict[str, Any]] = []
    invalid_hits: list[dict[str, Any]] = []
    valid_hits: list[dict[str, Any]] = []
    service_errors = False
    for gstin in gstins[:5]:
        profile = _fetch_cleartax_gstin_profile(gstin)
        profiles.append(profile)
        if bool(profile.get("service_error")):
            service_errors = True
            continue
        if bool(profile.get("valid")):
            valid_hits.append(profile)
        elif bool(profile.get("invalid_input")):
            invalid_hits.append(profile)

    if invalid_hits:
        first_invalid = invalid_hits[0]
        return {
            "triggered": True,
            "trigger_type": "invalid_gst",
            "reason": _text(first_invalid.get("message")) or "invalid gstin",
            "vendor_name": vendor_name,
            "gstins_checked": gstins[:5],
            "profiles": profiles,
            "invalid_format_values": [],
            "service_error": service_errors,
        }

    if vendor_name and valid_hits and not _is_weak_vendor_name_for_match(vendor_name):
        vendor_matches = False
        for profile in valid_hits:
            if _names_match(vendor_name, _text(profile.get("trade_name"))) or _names_match(vendor_name, _text(profile.get("legal_name"))):
                vendor_matches = True
                break
        if not vendor_matches:
            return {
                "triggered": True,
                "trigger_type": "gst_vendor_mismatch",
                "reason": "gst legal/trade name does not match vendor name",
                "vendor_name": vendor_name,
                "gstins_checked": gstins[:5],
                "profiles": profiles,
                "invalid_format_values": [],
                "service_error": service_errors,
            }

    return {
        "triggered": False,
        "trigger_type": "",
        "reason": "",
        "vendor_name": vendor_name,
        "gstins_checked": gstins[:5],
        "profiles": profiles,
        "invalid_format_values": [],
        "service_error": service_errors,
    }


def _build_gst_guardrail_entry(assessment: dict[str, Any]) -> ChecklistEntry:
    triggered = bool(assessment.get("triggered"))
    trigger_type = _text(assessment.get("trigger_type"))
    profiles = assessment.get("profiles") if isinstance(assessment.get("profiles"), list) else []
    vendor_name = _text(assessment.get("vendor_name"))

    if not triggered:
        note = "Checklist condition not triggered."
    elif trigger_type == "invalid_gst_format":
        bad_values = assessment.get("invalid_format_values") if isinstance(assessment.get("invalid_format_values"), list) else []
        bad_preview = ", ".join([_text(v) for v in bad_values[:3] if _text(v)]) or "invalid format"
        note = f"GST guardrail triggered: GST number format is invalid ({bad_preview})."
    elif trigger_type == "invalid_gst":
        invalid_profile = next((p for p in profiles if not bool((p or {}).get("valid"))), {})
        note = (
            "GST guardrail triggered: GST number appears invalid "
            f"({_text((invalid_profile or {}).get('gstin'))}: {_text((invalid_profile or {}).get('message')) or 'invalid gstin'})."
        )
    else:
        checked_names: list[str] = []
        for profile in profiles[:3]:
            trade = _text((profile or {}).get("trade_name"))
            legal = _text((profile or {}).get("legal_name"))
            name = trade or legal
            if name:
                checked_names.append(name)
        checked = "; ".join(checked_names) if checked_names else "verified GST profile"
        note = (
            "GST guardrail triggered: vendor name and GST profile mismatch "
            f"(vendor: {vendor_name or '-'}, gst profile: {checked})."
        )

    return ChecklistEntry(
        code="R018",
        name="GST invalid or GST-vendor mismatch",
        decision=ChecklistDecision.reject,
        severity="HARD_REJECT",
        source="openai_claim_rules",
        matched_scope=True,
        triggered=triggered,
        status="REJECT" if triggered else "NOT_MET",
        missing_evidence=[],
        note=note,
    )


def _flatten_text(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return out
        norm_s = _normalize_phrase(s)
        if s in {"-", "--", "---"} or not norm_s or norm_s in {"na", "n a", "none", "null", "nil", "not available", "not applicable", "not_applicable"}:
            return out
        out.append(s)
        return out
    if isinstance(value, (int, float, bool)):
        out.append(str(value))
        return out
    if isinstance(value, list):
        for item in value:
            out.extend(_flatten_text(item))
        return out
    if isinstance(value, dict):
        # Exclude feedback/report fields so prior checklist output does not re-trigger rules.
        skip_keys = {
            "conclusion", "detailed conclusion", "recommendation", "recommendation text",
            "final recommendation", "opinion", "exact reason", "rule hits", "checklist",
            "source summary", "triggered points", "why triggered", "summary",
        }
        for k, v in value.items():
            k_norm = _normalize_phrase(str(k or ""))
            if k_norm in skip_keys:
                continue
            out.extend(_flatten_text(v))
        return out
    return out

def _extract_openai_response_text(body: Any) -> str:
    if not isinstance(body, dict):
        return ""

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


def _parse_json_dict_from_text(raw_text: str) -> dict[str, Any] | None:
    text_value = str(raw_text or "").strip()
    if not text_value:
        return None

    if text_value.startswith("```"):
        text_value = re.sub(r"^```(?:json)?\s*", "", text_value, flags=re.I)
        text_value = re.sub(r"\s*```$", "", text_value)
        text_value = text_value.strip()

    try:
        parsed = json.loads(text_value)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    first = text_value.find("{")
    last = text_value.rfind("}")
    if first >= 0 and last > first:
        candidate = text_value[first : last + 1]
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _dedup_text_list(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text_item = str(item or "").strip()
        if not text_item:
            continue
        key = text_item.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text_item)
        if len(out) >= max(1, int(limit or 1)):
            break
    return out


def _prepare_claim_text_for_openai(claim_text: str, max_chars: int = 60000) -> str:
    raw = str(claim_text or "").strip()
    if not raw:
        return ""
    lines = []
    seen: set[str] = set()
    for line in re.split(r"\r\n|\r|\n", raw):
        t = line.strip()
        if not t:
            continue
        key = t.lower()
        if key in seen:
            continue
        seen.add(key)
        lines.append(t)
    compact = "\n".join(lines).strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars]


def _map_admission_required_to_pipeline(admission_required: str) -> tuple[str, str, bool, int, ChecklistDecision]:
    value = str(admission_required or "uncertain").strip().lower()
    if value == "yes":
        return "approve", "auto_approve_queue", False, 4, ChecklistDecision.approve
    if value == "no":
        return "reject", "reject_queue", True, 1, ChecklistDecision.reject
    return "need_more_evidence", "query_queue", True, 2, ChecklistDecision.query


def _run_openai_merged_medical_audit(claim_text: str) -> dict[str, Any]:
    global _openai_merged_rate_limited_until

    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY not configured")

    now_ts = time.time()
    if now_ts < _openai_merged_rate_limited_until:
        remaining = int(max(1, _openai_merged_rate_limited_until - now_ts))
        raise RuntimeError(f"{_OPENAI_MERGED_RATE_LIMIT_MARKER}: cooldown {remaining}s")


    merged_text = _prepare_claim_text_for_openai(claim_text)
    if not merged_text:
        raise RuntimeError("No extracted claim text available for merged review")

    user_prompt = (
        "You are an expert medical auditor and claim reviewer.\n\n"
        "Your task is to analyze medical documents (including OCR-extracted text, prescriptions, reports, and bills) and determine whether the hospitalization and treatment are justified.\n\n"
        "Follow these strict instructions:\n\n"
        "1. Admission Justification:\n"
        "- Evaluate whether hospital admission was medically necessary.\n"
        '- Classify as: "yes", "no", or "uncertain".\n\n'
        "2. Treatment Evaluation:\n"
        "- Check if medicines and procedures are appropriate for the diagnosis.\n"
        "- Flag unnecessary or excessive treatments.\n\n"
        "3. Diagnosis Validation:\n"
        "- Verify whether investigations/tests support the diagnosis.\n"
        "- Highlight mismatches or missing diagnostic evidence.\n\n"
        "4. Length of Stay (LOS):\n"
        "- Assess whether the duration of hospitalization is justified.\n\n"
        "5. Doctor Validity:\n"
        "- Comment if doctor details suggest mismatch (if available).\n\n"
        "6. Evidence Extraction:\n"
        "- Extract key supporting data:\n"
        "  - Symptoms\n"
        "  - Vitals\n"
        "  - Lab results\n"
        "  - Medicines\n"
        "  - Procedures\n\n"
        "7. Missing Information:\n"
        "- Clearly list any missing documents or data required for proper assessment.\n\n"
        "8. Rules:\n"
        "- DO NOT invent information.\n"
        '- If unclear text exists (OCR issues), mark as "[unclear]".\n'
        "- Be conservative and evidence-based.\n\n"
        "9. Conclusion Style (MANDATORY):\n"
        "- In `rationale`, write a clear medical conclusion in this style:\n"
        '  "<age> year old patient diagnosed with <diagnosis> with chief complaints of <complaints>, having deranged investigation values of <important abnormal values>, and treated with following medicines <medicine list>."\n'
        "- If any part is not available, write [unclear] for that part.\n\n"
        "10. Output Format (STRICT JSON ONLY):\n\n"
        "{\n"
        '  "admission_required": "yes | no | uncertain",\n'
        '  "confidence": 0-100,\n'
        '  "rationale": "One-paragraph conclusion in required format",\n'
        '  "evidence": [\n'
        '    "point 1",\n'
        '    "point 2"\n'
        "  ],\n"
        '  "missing_information": [\n'
        '    "missing item 1"\n'
        "  ],\n"
        f'  "disclaimer": "{_OPENAI_MERGED_AUDIT_DISCLAIMER}"\n'
        "}\n\n"
        "Input:\n"
        + merged_text
    )

    base_url = settings.openai_base_url.rstrip("/") if settings.openai_base_url else "https://api.openai.com/v1"
    url = f"{base_url}/chat/completions"
    headers = {
        "Authorization": f"Bearer {settings.openai_api_key}",
        "Content-Type": "application/json",
    }

    # Force single model for merged audit to prevent fallback bursts.
    configured_model = "gpt-4.1-mini"
    model_candidates: list[str] = [configured_model]

    errors: list[str] = []
    used_model = configured_model
    parsed: dict[str, Any] | None = None
    raw_output = ""

    for candidate in model_candidates:
        request_payload = {
            "model": candidate,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": "You are an expert medical auditor and claim reviewer. Return strict JSON only.",
                },
                {"role": "user", "content": user_prompt},
            ],
        }
        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(url, headers=headers, json=request_payload)
                response.raise_for_status()
            body = response.json()
            used_model = str(body.get("model") or candidate)
            raw_output = _extract_openai_response_text(body)
            parsed = _parse_json_dict_from_text(raw_output)
            if isinstance(parsed, dict):
                break
            errors.append(f"{candidate} => invalid_json")
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code if exc.response is not None else 0
            if status_code == 429:
                _openai_merged_rate_limited_until = time.time() + _OPENAI_MERGED_RATE_LIMIT_COOLDOWN_SECONDS
                raise RuntimeError(_OPENAI_MERGED_RATE_LIMIT_MARKER)
            errors.append(f"{candidate} => HTTP {status_code}: {exc}")
        except Exception as exc:
            errors.append(f"{candidate} => {exc}")

    if not isinstance(parsed, dict):
        raise RuntimeError(
            "Merged OpenAI medical audit failed. "
            f"models_tried={model_candidates}; errors={errors[:3] or ['none']}"
        )

    admission_required = str(parsed.get("admission_required") or "uncertain").strip().lower()
    if admission_required not in {"yes", "no", "uncertain"}:
        admission_required = "uncertain"

    confidence_raw = parsed.get("confidence")
    confidence = 0.0
    try:
        confidence = float(confidence_raw)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence < 0.0:
        confidence = 0.0
    if confidence > 100.0:
        confidence = 100.0

    rationale = str(parsed.get("rationale") or "").strip()
    evidence = _dedup_text_list(parsed.get("evidence"), limit=30)
    missing_information = _dedup_text_list(parsed.get("missing_information"), limit=30)
    disclaimer = str(parsed.get("disclaimer") or "").strip() or _OPENAI_MERGED_AUDIT_DISCLAIMER

    return {
        "admission_required": admission_required,
        "confidence": confidence,
        "rationale": rationale,
        "evidence": evidence,
        "missing_information": missing_information,
        "disclaimer": disclaimer,
        "used_model": used_model,
        "models_tried": model_candidates,
        "errors": errors,
        "raw_output": raw_output,
    }


def _normalize_phrase(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", (text or "").lower())).strip()


def _tokenize_norm(text: str) -> list[str]:
    return [t for t in _normalize_phrase(text).split(" ") if t]


def _contains_token_non_negated(text_tokens: list[str], token: str) -> bool:
    for idx, tok in enumerate(text_tokens):
        if tok != token:
            continue
        prev_tok = text_tokens[idx - 1] if idx > 0 else ""
        # Avoid matching "surgical" from "non surgical" style negatives.
        if prev_tok == "non":
            continue
        return True
    return False


def _contains_contiguous_phrase(text_tokens: list[str], phrase_tokens: list[str]) -> bool:
    if not phrase_tokens:
        return False
    n = len(phrase_tokens)
    if n > len(text_tokens):
        return False
    for idx in range(0, len(text_tokens) - n + 1):
        if text_tokens[idx : idx + n] == phrase_tokens:
            return True
    return False


def _contains_ordered_tokens_with_max_gap(
    text_tokens: list[str],
    phrase_tokens: list[str],
    max_gap: int,
) -> bool:
    if not phrase_tokens:
        return False
    if len(phrase_tokens) == 1:
        return _contains_token_non_negated(text_tokens, phrase_tokens[0])

    start_positions = [idx for idx, tok in enumerate(text_tokens) if tok == phrase_tokens[0]]
    if not start_positions:
        return False

    for start in start_positions:
        prev = start
        ok = True
        for token in phrase_tokens[1:]:
            found_idx = -1
            upper = min(len(text_tokens), prev + max_gap + 2)
            for j in range(prev + 1, upper):
                if text_tokens[j] == token:
                    found_idx = j
                    break
            if found_idx < 0:
                ok = False
                break
            prev = found_idx
        if ok:
            return True
    return False


def _phrase_match(text_norm: str, phrase: str) -> bool:
    phrase_norm = _normalize_phrase(phrase)
    if not phrase_norm:
        return False

    phrase_tokens = [t for t in phrase_norm.split(" ") if t]
    if not phrase_tokens:
        return False

    text_tokens = [t for t in str(text_norm or "").split(" ") if t]
    if not text_tokens:
        text_tokens = _tokenize_norm(text_norm)

    if len(phrase_tokens) == 1:
        return _contains_token_non_negated(text_tokens, phrase_tokens[0])

    if _contains_contiguous_phrase(text_tokens, phrase_tokens):
        return True

    # Balanced fallback: allow ordered phrase tokens with small distance.
    return _contains_ordered_tokens_with_max_gap(text_tokens, phrase_tokens, max_gap=3)

_EVIDENCE_ALIASES: dict[str, list[str]] = {
    "x ray ct displacement details": [
        "displaced fracture",
        "slightly displaced fracture",
        "fracture displacement",
        "displacement noted",
        "x ray shows displaced",
        "ct shows displaced",
    ],
    "instability or neurovascular risk": [
        "unstable fracture",
        "instability",
        "neurovascular deficit",
        "neurovascular compromise",
        "neurovascular injury",
        "displaced fracture",
    ],
    "ot operative note": [
        "ot note",
        "operative note",
        "operation note",
        "procedure note",
        "orif",
        "open reduction internal fixation",
        "k wire fixation",
        "postoperative status",
    ],
    "fracture description": [
        "proximal humerus fracture",
        "neck of humerus fracture",
        "fracture neck humerus",
        "humerus fracture",
        "fracture",
    ],
    "procedure note bill showing orif k wire": [
        "orif",
        "open reduction internal fixation",
        "k wire",
        "plating",
        "surgery done",
        "operative note",
        "procedure note",
        "operation note",
    ],
    "surgical indication": [
        "displaced fracture",
        "unstable fracture",
        "orif advised",
        "planned for orif",
        "surgical fixation",
        "operative management",
    ],
    "imaging displacement status": [
        "displaced fracture",
        "slightly displaced fracture",
        "undisplaced fracture",
        "no displacement",
        "displacement noted",
    ],
    "indication for orif k wire": [
        "orif indicated",
        "orif advised",
        "surgical fixation advised",
        "displaced fracture",
        "unstable fracture",
    ],
    "operative record": [
        "ot note",
        "operative note",
        "operation note",
        "procedure note",
        "postoperative status",
        "orif",
    ],
    "x ray radiology report": [
        "x ray",
        "xray",
        "radiology report",
        "imaging report",
        "ct report",
        "mri report",
    ],
    "x ray radiology bill line items": [
        "x ray bill",
        "radiology bill",
        "xray charges",
        "imaging charges",
        "radiology charges",
    ],
}


def _evidence_match(text_norm: str, evidence: str) -> bool:
    evidence_text = str(evidence or "").strip()
    if not evidence_text:
        return True

    if _phrase_match(text_norm, evidence_text):
        return True

    ev_norm = _normalize_phrase(evidence_text)
    aliases = _EVIDENCE_ALIASES.get(ev_norm, [])
    return any(_phrase_match(text_norm, alias) for alias in aliases)




def _strip_checklist_feedback_noise(raw_text: str) -> str:
    text_value = str(raw_text or "")
    if not text_value:
        return ""

    drop_patterns = [
        r"\bOPENAI_MERGED_REVIEW\b",
        r"\bRule-wise medical review\b",
        r"\bTriggered points?\b",
        r"\bMissing evidence\b",
        r"\bKey clinical observations\b",
        r"\bRequired clarifications\b",
        r"\bopenai_claim_rules\b",
        r"\bopenai_diagnosis_criteria\b",
        r"\bR\d{3}\b",
        r"\bDX\d{3}\b",
    ]

    kept: list[str] = []
    for line in re.split(r"\r\n|\r|\n", text_value):
        ln = str(line or "").strip()
        if not ln:
            continue
        if any(re.search(pat, ln, flags=re.I) for pat in drop_patterns):
            continue
        kept.append(ln)

    return "\n".join(kept).strip()

def _collect_claim_context(db: Session, claim_id: UUID) -> dict[str, Any]:
    claim_row = db.execute(
        text(
            """
            SELECT id, external_claim_id, patient_name, patient_identifier, status, priority, source_channel, tags
            FROM claims
            WHERE id = :claim_id
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()

    if claim_row is None:
        raise ClaimNotFoundError

    extraction_rows = db.execute(
        text(
            """
            WITH latest_per_document AS (
                SELECT
                    id,
                    document_id,
                    extracted_entities,
                    evidence_refs,
                    model_name,
                    extraction_version,
                    created_at,
                    ROW_NUMBER() OVER (PARTITION BY document_id ORDER BY created_at DESC) AS rn
                FROM document_extractions
                WHERE claim_id = :claim_id
            )
            SELECT id, document_id, extracted_entities, evidence_refs, model_name, extraction_version, created_at
            FROM latest_per_document
            WHERE rn = 1
            ORDER BY created_at DESC
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().all()
    legacy_payload: dict[str, Any] = {}
    try:
        legacy_row = db.execute(
            text(
                """
                SELECT legacy_payload
                FROM claim_legacy_data
                WHERE claim_id = :claim_id
                ORDER BY updated_at DESC NULLS LAST
                LIMIT 1
                """
            ),
            {"claim_id": str(claim_id)},
        ).mappings().first()
        if legacy_row is not None:
            legacy_payload = _safe_json_dict(legacy_row.get("legacy_payload"))
    except Exception:
        legacy_payload = {}

    parts: list[str] = []
    parts.extend(
        _flatten_text(
            {
                "external_claim_id": claim_row.get("external_claim_id"),
                "patient_name": claim_row.get("patient_name"),
                "patient_identifier": claim_row.get("patient_identifier"),
                "status": claim_row.get("status"),
                "priority": claim_row.get("priority"),
                "source_channel": claim_row.get("source_channel"),
                "tags": claim_row.get("tags") if claim_row.get("tags") is not None else [],
            }
        )
    )
    parts.extend(_flatten_text(legacy_payload))

    extraction_id = None
    extracted_vendor_name = ""
    for idx, extraction_row in enumerate(extraction_rows):
        if idx == 0:
            extraction_id = extraction_row.get("id")

        extracted_entities = extraction_row.get("extracted_entities")
        evidence_refs = extraction_row.get("evidence_refs")

        if isinstance(extracted_entities, str):
            try:
                extracted_entities = json.loads(extracted_entities)
            except json.JSONDecodeError:
                pass
        if isinstance(evidence_refs, str):
            try:
                evidence_refs = json.loads(evidence_refs)
            except json.JSONDecodeError:
                pass

        if not extracted_vendor_name and isinstance(extracted_entities, dict):
            for vendor_key in _GST_VENDOR_KEYS:
                candidate = _text(extracted_entities.get(vendor_key))
                if candidate and not _is_non_pharmacy_vendor_name(candidate):
                    extracted_vendor_name = candidate
                    break

        parts.extend(_flatten_text(extracted_entities))
        parts.extend(_flatten_text(evidence_refs))

    raw_full_text = "\n".join(parts)
    full_text = _strip_checklist_feedback_noise(raw_full_text)
    text_norm = _normalize_phrase(full_text)
    expiry_check_date = date.today()
    expired_medicine_hits = _detect_expired_medicine_hits(full_text, as_of=expiry_check_date)

    return {
        "claim": dict(claim_row),
        "extraction_id": extraction_id,
        "extraction_count": len(extraction_rows),
        "text": full_text,
        "text_norm": text_norm,
        "legacy_payload": legacy_payload,
        "extracted_vendor_name": extracted_vendor_name,
        "expiry_check_date": expiry_check_date.isoformat(),
        "expired_medicine_hits": expired_medicine_hits,
    }

def _humanize_missing_evidence_term(value: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    norm = _normalize_phrase(text_value)
    if norm in {"objective infection labs culture", "objective infection lab culture"}:
        return "culture sensitivity test and sepsis markers"
    if norm in {"tpr chart", "serial tpr chart", "tpr"}:
        return "serial TPR charting"
    if norm in {"bp trend", "blood pressure trend"}:
        return "serial BP trend"
    if norm in {"fever tachycardia tachypnea evidence"}:
        return "sustained fever/tachycardia/tachypnea trend"
    return text_value


def _format_entry_note(entry_name: str, rule_remark: str, missing_evidence: list[str], triggered: bool, decision: str) -> str:
    if not triggered:
        return "Checklist condition not triggered."

    note_parts: list[str] = []
    remark = str(rule_remark or "").strip()
    if remark:
        remark = re.sub(
            r"Auto-learned rule from auditor conclusion and extraction context\.\s*"
            r"Super-admin approval required\.\s*Suggested outcome:\s*[A-Z_]+\.\s*",
            "",
            remark,
            flags=re.I,
        ).strip(" .;:-")
    if remark:
        note_parts.append(remark)

    if not missing_evidence and str(decision or "").upper() == "APPROVE":
        if not note_parts and entry_name:
            note_parts.append(f"{entry_name}: Required evidence is present.")
        elif not note_parts:
            note_parts.append("Required evidence is present.")
    out = "; ".join([p for p in note_parts if p])
    return out


def _is_runtime_supported_claim_rule(rule: dict[str, Any]) -> bool:
    rule_id = str((rule or {}).get("rule_id") or "").strip().upper()
    rule_name = str((rule or {}).get("name") or "").strip()
    if not re.fullmatch(r"R\d{3}", rule_id):
        return False
    if re.search(r"\blearning from conclusion and extraction\b", rule_name, flags=re.I):
        return False
    return True



def _rule_scope_matched(rule_id: str, scope_terms: list[str], text_norm: str) -> bool:
    rid = str(rule_id or "").strip().upper()

    # Rule-specific tightening to avoid unrelated false triggers.
    if rid in {"R002", "R009", "R010"}:
        # Fracture-procedure family should only apply when fracture context is present.
        fracture_terms = [
            "fracture",
            "hairline fracture",
            "undisplaced fracture",
            "displaced fracture",
            "comminuted fracture",
            "avulsion fracture",
            "greenstick fracture",
            "broken bone",
        ]
        return any(_phrase_match(text_norm, term) for term in fracture_terms)

    if rid == "R008":
        alcohol_present = any(
            _phrase_match(text_norm, term)
            for term in ["alcoholism", "alcohol use", "alcohol dependence", "chronic alcohol"]
        )
        cld_present = any(
            _phrase_match(text_norm, term)
            for term in ["cld", "chronic liver disease", "cirrhosis", "hepatic disease"]
        )
        return alcohol_present and cld_present

    if rid == "R007":
        ayush_terms = [
            "ayurvedic",
            "ayuervedic",
            "ayurveda",
            "ayush",
            "bams",
            "bhms",
            "bums",
            "bsms",
            "panchakarma",
            "ashtang",
            "ashtanga",
            "siddha",
            "unani",
            "homeopathy",
            "homeopathic",
            "naturopathy",
        ]
        matched_count = sum(1 for t in ayush_terms if _phrase_match(text_norm, t))
        has_ayush_hospital_context = any(
            _phrase_match(text_norm, phrase)
            for phrase in ["ayurvedic hospital", "ayush hospital", "ayush treatment", "ayurvedic treatment"]
        )
        return matched_count >= 1 or has_ayush_hospital_context

    generic_scope_tokens = {"query", "policy exclusion", "hospital credentials", "flow override"}
    effective_scope = [
        s for s in scope_terms
        if _normalize_phrase(str(s or "")) not in generic_scope_tokens
    ]
    if not effective_scope:
        return False
    return any(_phrase_match(text_norm, str(s)) for s in effective_scope)
def _evaluate_checklist(
    text_norm: str,
    rules: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
    expired_medicine_hits: list[dict[str, str]] | None = None,
    expiry_check_date: str | None = None,
) -> list[ChecklistEntry]:
    entries: list[ChecklistEntry] = []

    for rule in sorted(rules, key=lambda r: int(r.get("priority") or 999)):
        if not _is_runtime_supported_claim_rule(rule):
            continue
        scope = [s for s in (rule.get("scope") or []) if str(s).strip()]
        required_evidence = [e for e in (rule.get("required_evidence") or []) if str(e).strip()]

        matched_scope = _rule_scope_matched(str(rule.get("rule_id") or ""), scope, text_norm) if scope else True
        missing_evidence = [str(ev) for ev in required_evidence if not _evidence_match(text_norm, str(ev))]

        decision = str(rule.get("decision") or "QUERY").upper()
        if decision not in {"APPROVE", "QUERY", "REJECT"}:
            decision = "QUERY"

        if not matched_scope:
            triggered = False
        elif decision == "APPROVE":
            triggered = len(missing_evidence) == 0
        else:
            triggered = len(missing_evidence) > 0

        status = decision if triggered else "NOT_MET"
        note = _format_entry_note(
            entry_name=str(rule.get("name") or "Legacy claim rule").strip(),
            rule_remark=str(rule.get("remark_template") or "").strip(),
            missing_evidence=missing_evidence,
            triggered=triggered,
            decision=decision,
        )

        entries.append(
            ChecklistEntry(
                code=str(rule.get("rule_id") or "").strip().upper() or "RULE",
                name=str(rule.get("name") or "Legacy claim rule").strip(),
                decision=ChecklistDecision(decision),
                severity=str(rule.get("severity") or "SOFT_QUERY").strip().upper(),
                source="openai_claim_rules",
                matched_scope=matched_scope,
                triggered=triggered,
                status=status,
                missing_evidence=missing_evidence,
                note=note,
            )
        )

    for criterion in sorted(criteria, key=lambda c: int(c.get("priority") or 999)):
        aliases = [a for a in (criterion.get("aliases") or []) if str(a).strip()]
        required_evidence = [e for e in (criterion.get("required_evidence") or []) if str(e).strip()]

        matched_scope = any(_phrase_match(text_norm, str(alias)) for alias in aliases)
        missing_evidence = [str(ev) for ev in required_evidence if not _evidence_match(text_norm, str(ev))]

        decision = str(criterion.get("decision") or "QUERY").upper()
        if decision not in {"APPROVE", "QUERY", "REJECT"}:
            decision = "QUERY"

        if not matched_scope:
            triggered = False
        elif decision == "APPROVE":
            triggered = len(missing_evidence) == 0
        else:
            triggered = len(missing_evidence) > 0

        status = decision if triggered else "NOT_MET"
        note = _format_entry_note(
            entry_name=str(criterion.get("diagnosis_name") or "Diagnosis criteria").strip(),
            rule_remark=str(criterion.get("remark_template") or "").strip(),
            missing_evidence=missing_evidence,
            triggered=triggered,
            decision=decision,
        )

        entries.append(
            ChecklistEntry(
                code=str(criterion.get("criteria_id") or "").strip().upper() or "DX",
                name=str(criterion.get("diagnosis_name") or "Diagnosis criteria").strip(),
                decision=ChecklistDecision(decision),
                severity=str(criterion.get("severity") or "SOFT_QUERY").strip().upper(),
                source="openai_diagnosis_criteria",
                matched_scope=matched_scope,
                triggered=triggered,
                status=status,
                missing_evidence=missing_evidence,
                note=note,
            )
        )

    as_of_date = date.today()
    if expiry_check_date:
        try:
            as_of_date = date.fromisoformat(str(expiry_check_date))
        except ValueError:
            as_of_date = date.today()
    entries.append(
        _build_expired_medicine_guardrail_entry(
            expired_hits=expired_medicine_hits or [],
            as_of_date=as_of_date,
        )
    )

    return entries


def _derive_recommendation(entries: list[ChecklistEntry]) -> tuple[str, str, bool, int, str]:
    triggered = [e for e in entries if e.triggered]
    triggered_reject = [e for e in triggered if e.decision == ChecklistDecision.reject]
    triggered_query = [e for e in triggered if e.decision == ChecklistDecision.query]
    triggered_approve = [e for e in triggered if e.decision == ChecklistDecision.approve]

    if triggered_reject:
        preview = "; ".join([f"{e.code} ({e.name})" for e in triggered_reject[:5]])
        return (
            "reject",
            "reject_queue",
            True,
            1,
            f"Reject triggers: {preview}" if preview else "Reject trigger matched",
        )
    if triggered_query:
        preview = "; ".join([f"{e.code} ({e.name})" for e in triggered_query[:5]])
        return (
            "need_more_evidence",
            "query_queue",
            True,
            2,
            f"Query triggers: {preview}" if preview else "Query trigger matched",
        )
    if triggered_approve:
        preview = "; ".join([f"{e.code} ({e.name})" for e in triggered_approve[:5]])
        return (
            "approve",
            "auto_approve_queue",
            False,
            4,
            f"Approval signals: {preview}" if preview else "Approval signal matched",
        )

    return ("approve", "auto_approve_queue", False, 4, "No checklist trigger matched")



def _combine_rule_and_ml(
    recommendation: str,
    route_target: str,
    manual_review_required: bool,
    review_priority: int,
    summary_text: str,
    ml_pred: dict[str, Any],
) -> tuple[str, str, bool, int, str]:
    # Rule-based decision is authoritative. Learning stays advisory.
    if not ml_pred.get("available"):
        return recommendation, route_target, manual_review_required, review_priority, summary_text

    ml_label = str(ml_pred.get("label") or "").strip().lower()
    ml_conf = float(ml_pred.get("confidence") or 0.0)
    if not ml_label:
        return recommendation, route_target, manual_review_required, review_priority, summary_text

    ml_note = f"Learning signal: {ml_label} ({ml_conf * 100.0:.1f}% confidence)."
    if summary_text:
        summary_text = summary_text.rstrip(" .") + ". " + ml_note
    else:
        summary_text = ml_note
    return recommendation, route_target, manual_review_required, review_priority, summary_text


def _recommendation_sentence(recommendation: str) -> str:
    value = str(recommendation or "").strip().lower()
    if value == "approve":
        return "Justified + Admissible"
    if value == "reject":
        return "Not Justified + Inadmissible"
    return "Not Justified + Inadmissible"


def _clean_reporting_note(value: str) -> str:
    note = str(value or "").strip()
    if not note:
        return ""
    note = re.sub(r"\bOPENAI_MERGED_REVIEW\b", "", note, flags=re.I)
    note = re.sub(r"\bRule-wise medical review.*$", "", note, flags=re.I)
    note = re.sub(r"\bTriggered points?\s*:\s*", "", note, flags=re.I)
    note = re.sub(r"\b[Rr]\d{3}\b\s*[-:]\s*", "", note)
    note = re.sub(r"\bDX\d{3}\b\s*[-:]\s*", "", note)
    note = re.sub(r"\bMissing evidence\s*:[^.;\n]*(?:[.;]|$)", " ", note, flags=re.I)
    note = re.sub(r"\bLearning signal\s*:[^.]*\.?", " ", note, flags=re.I)
    note = re.sub(r"\btherefore,\s*one or more clinical rules are triggered\.?", " ", note, flags=re.I)
    note = re.sub(r"\btherefore,\s*this rule is triggered\.?", " ", note, flags=re.I)
    note = re.sub(r"\b(?:one or more\s+)?clinical rules?\s+are\s+triggered\.?", " ", note, flags=re.I)
    note = re.sub(r"\bthis rule is triggered\.?", " ", note, flags=re.I)
    note = re.sub(r"\bAuto-learned rule from auditor conclusion and extraction context\.?", " ", note, flags=re.I)
    note = re.sub(r"\bSuper-admin approval required\.?", " ", note, flags=re.I)
    note = re.sub(r"\bSuggested outcome\s*:\s*[A-Z_]+\.?", " ", note, flags=re.I)
    if re.search(r"\bChecklist condition matched and evidence pattern triggered\b", note, flags=re.I):
        return ""
    if re.search(r"\bGST guardrail triggered\b", note, flags=re.I):
        return ""
    if re.search(r"\b(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion)\b", note, flags=re.I):
        return ""
    machine_term = r"(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion|complaints)"
    if re.fullmatch(rf"{machine_term}(?:\s*,\s*{machine_term})*", note.strip(), flags=re.I):
        return ""
    note = re.sub(r"\s+", " ", note).strip(" .;:-")
    return note
def _build_rulewise_conclusion(entries: list[ChecklistEntry], recommendation: str, openai_rationale: str = "") -> str:
    triggered = [
        e
        for e in entries
        if e.triggered and e.source in {"openai_claim_rules", "openai_diagnosis_criteria"}
    ]
    points: list[str] = []
    missing_items: list[str] = []
    seen_points: set[str] = set()
    seen_missing: set[str] = set()

    for entry in triggered[:10]:
        note = _clean_reporting_note(str(entry.note or "").strip())
        if note.lower() in {"checklist condition matched and evidence pattern triggered.", "triggered condition identified."}:
            note = ""
        if "openai_merged_review" in note.lower() or "merged document ai medical audit" in note.lower():
            note = ""
        if not note and str(entry.code or "").strip().upper() == "R018":
            note = "Pharmacy GST validation failed against verified GST registration details for the submitted vendor context"

        if note:
            key = re.sub(r"\s+", " ", note).strip().lower()
            if key and key not in seen_points:
                seen_points.add(key)
                points.append(note)

        missing = entry.missing_evidence or []
        for raw in missing:
            cleaned = _clean_reporting_note(str(raw or ""))
            if not cleaned:
                continue
            key = cleaned.lower()
            if key not in seen_missing:
                seen_missing.add(key)
                missing_items.append(cleaned)

    rec = str(recommendation or "").strip().lower()
    if rec == "approve":
        prefix = "Medical review supports admissibility on available records."
    else:
        prefix = "The case requires additional clinical correlation before final admissibility decision."

    parts: list[str] = [prefix]
    if points:
        parts.append("Key clinical observations: " + "; ".join(points[:3]) + ".")
    ai_reason = _clean_reporting_note(str(openai_rationale or "").strip())
    if ai_reason and rec == "approve" and not points:
        parts.append("Clinical summary: " + ai_reason)

    return " ".join([p for p in parts if p]).strip()



def _emit_workflow_event(
    db: Session,
    claim_id: UUID,
    event_type: str,
    actor_id: str | None,
    payload: dict[str, Any],
) -> None:
    db.execute(
        text(
            """
            INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
            VALUES (:claim_id, 'user', :actor_id, :event_type, CAST(:event_payload AS jsonb))
            """
        ),
        {
            "claim_id": str(claim_id),
            "actor_id": actor_id,
            "event_type": event_type,
            "event_payload": json.dumps(payload),
        },
    )


def run_claim_checklist_pipeline(
    db: Session,
    claim_id: UUID,
    actor_id: str | None,
    force_source_refresh: bool = False,
) -> ChecklistRunResponse:
    context = _collect_claim_context(db, claim_id)

    rules, criteria, source_summary = get_checklist_catalog(force_refresh=force_source_refresh)
    entries = _evaluate_checklist(
        context["text_norm"],
        rules,
        criteria,
        expired_medicine_hits=context.get("expired_medicine_hits") or [],
        expiry_check_date=context.get("expiry_check_date"),
    )
    gst_guardrail = _assess_gst_guardrail(context)
    entries.append(_build_gst_guardrail_entry(gst_guardrail))
    recommendation, route_target, manual_review_required, review_priority, summary_text = _derive_recommendation(entries)
    rule_locked_by_trigger = any(
        e.triggered and e.source in {"openai_claim_rules", "openai_diagnosis_criteria"}
        and e.decision in {ChecklistDecision.reject, ChecklistDecision.query}
        for e in entries
    )

    ml_prediction = {
        "available": False,
        "label": None,
        "confidence": 0.0,
        "probabilities": {},
        "top_signals": [],
        "model_version": "strict_rule_mode",
        "training_examples": 0,
        "reason": "strict_rule_based_mode_enabled",
    }
    if not STRICT_RULE_BASED_MODE:
        ml_prediction_obj = predict_claim_recommendation(
            db=db,
            claim_text=context["text"],
            # Learn on every claim-process run so the model stays continuously refreshed.
            force_retrain=True,
        )
        ml_prediction = {
            "available": bool(ml_prediction_obj.available),
            "label": ml_prediction_obj.label,
            "confidence": float(ml_prediction_obj.confidence or 0.0),
            "probabilities": ml_prediction_obj.probabilities or {},
            "top_signals": ml_prediction_obj.top_signals or [],
            "model_version": ml_prediction_obj.model_version,
            "training_examples": int(ml_prediction_obj.training_examples or 0),
            "reason": ml_prediction_obj.reason,
        }

        recommendation, route_target, manual_review_required, review_priority, summary_text = _combine_rule_and_ml(
            recommendation=recommendation,
            route_target=route_target,
            manual_review_required=manual_review_required,
            review_priority=review_priority,
            summary_text=summary_text,
            ml_pred=ml_prediction,
        )

    openai_merged_review: dict[str, Any] | None = None
    openai_merged_review_error: str | None = "disabled_in_strict_rule_mode" if STRICT_RULE_BASED_MODE else None
    if (not STRICT_RULE_BASED_MODE) and context.get("extraction_count", 0):
        try:
            openai_merged_review = _run_openai_merged_medical_audit(context["text"])
            (
                openai_recommendation,
                openai_route_target,
                openai_manual_review_required,
                openai_review_priority,
                openai_decision,
            ) = _map_admission_required_to_pipeline(openai_merged_review.get("admission_required"))

            rationale = str(openai_merged_review.get("rationale") or "").strip()
            evidence = openai_merged_review.get("evidence") if isinstance(openai_merged_review.get("evidence"), list) else []
            missing = (
                openai_merged_review.get("missing_information")
                if isinstance(openai_merged_review.get("missing_information"), list)
                else []
            )

            # Rule-first mode: keep rule-based recommendation authoritative.
            # OpenAI merged audit is appended as advisory evidence only.

            note_parts: list[str] = []
            if rationale:
                note_parts.append("Clinical summary: " + rationale)
            if missing:
                note_parts.append("Missing information: " + "; ".join(str(x) for x in missing[:12] if str(x).strip()))
            note = "; ".join([p for p in note_parts if p])
            if not note:
                note = f"OpenAI merged medical audit used ({openai_merged_review.get('confidence', 0):.1f}% confidence)."

            entries.append(
                ChecklistEntry(
                    code="OPENAI_MERGED_REVIEW",
                    name="Merged Document AI Medical Audit",
                    decision=openai_decision,
                    severity="SOFT_QUERY",
                    source="openai_merged_review",
                    matched_scope=True,
                    triggered=True,
                    status=openai_decision.value,
                    missing_evidence=[str(x) for x in missing[:20]],
                    note=note,
                )
            )
        except Exception as exc:
            err_text = str(exc or "")
            if err_text.startswith(_OPENAI_MERGED_RATE_LIMIT_MARKER):
                openai_merged_review_error = "OpenAI rate limit active; merged AI medical-audit skipped temporarily."
            else:
                openai_merged_review_error = err_text

    probs = ml_prediction.get("probabilities") if isinstance(ml_prediction.get("probabilities"), dict) else {}
    fraud_risk_score = float(probs.get("reject") or 0.0) if ml_prediction.get("available") else None
    qc_risk_score = (
        float(max(float(probs.get("need_more_evidence") or 0.0), float(probs.get("manual_review") or 0.0)))
        if ml_prediction.get("available")
        else None
    )

    db.execute(
        text(
            """
            UPDATE decision_results
            SET is_active = FALSE
            WHERE claim_id = :claim_id AND is_active = TRUE AND generated_by = 'checklist_pipeline'
            """
        ),
        {"claim_id": str(claim_id)},
    )

    source_summary = dict(source_summary or {})
    source_summary["strict_rule_based_mode"] = bool(STRICT_RULE_BASED_MODE)
    source_summary["ml_model"] = {
        "available": ml_prediction["available"],
        "model_version": ml_prediction.get("model_version"),
        "training_examples": ml_prediction.get("training_examples"),
    }
    source_summary["openai_merged_review"] = {
        "used": bool(openai_merged_review),
        "admission_required": (openai_merged_review or {}).get("admission_required"),
        "confidence": (openai_merged_review or {}).get("confidence"),
        "model": (openai_merged_review or {}).get("used_model"),
        "error": openai_merged_review_error,
    }
    source_summary["expired_medicine_check"] = {
        "as_of_date": context.get("expiry_check_date"),
        "expired_entries_count": len(context.get("expired_medicine_hits") or []),
        "sample": (context.get("expired_medicine_hits") or [])[:3],
    }
    source_summary["gst_guardrail"] = {
        "triggered": bool(gst_guardrail.get("triggered")),
        "trigger_type": _text(gst_guardrail.get("trigger_type")),
        "reason": _text(gst_guardrail.get("reason")),
        "vendor_name": _text(gst_guardrail.get("vendor_name")),
        "gstins_checked": (gst_guardrail.get("gstins_checked") or [])[:5],
        "invalid_format_values": (gst_guardrail.get("invalid_format_values") or [])[:3],
        "service_error": bool(gst_guardrail.get("service_error")),
        "profiles_sample": (gst_guardrail.get("profiles") or [])[:3],
    }

    rulewise_conclusion = _build_rulewise_conclusion(
        entries=entries,
        recommendation=recommendation,
        openai_rationale=str((openai_merged_review or {}).get("rationale") or ""),
    )
    recommendation_text = _recommendation_sentence(recommendation)
    ml_label = str(ml_prediction.get("label") or "").strip().lower() if ml_prediction.get("available") else ""
    ml_conf = float(ml_prediction.get("confidence") or 0.0) if ml_prediction.get("available") else 0.0
    learning_note = f"Learning signal: {ml_label} ({ml_conf * 100.0:.1f}% confidence)." if ml_label else ""

    if rulewise_conclusion:
        summary_text = (rulewise_conclusion + (" " + learning_note if learning_note else ""))[:4000]
    elif learning_note:
        summary_text = ((summary_text.rstrip(" .") + ". ") if summary_text else "") + learning_note

    source_summary["reporting"] = {
        "conclusion": rulewise_conclusion,
        "recommendation_text": recommendation_text,
        "rule_locked_by_trigger": bool(rule_locked_by_trigger),
    }

    payload = {
        "checklist": [entry.model_dump() for entry in entries],
        "source_summary": source_summary,
        "claim_text_excerpt": context["text"][:4000],
        "ml_prediction": ml_prediction,
        "openai_merged_review": openai_merged_review or {},
        "openai_merged_review_error": openai_merged_review_error,
        "conclusion": rulewise_conclusion,
        "recommendation_text": recommendation_text,
    }
    triggered_rule_hits = [entry.model_dump() for entry in entries if entry.triggered]
    consistency_checks = [entry.model_dump() for entry in entries if entry.source == "openai_diagnosis_criteria"]

    row = db.execute(
        text(
            """
            INSERT INTO decision_results (
                claim_id,
                extraction_id,
                rule_version,
                model_version,
                fraud_risk_score,
                qc_risk_score,
                consistency_checks,
                rule_hits,
                explanation_summary,
                recommendation,
                route_target,
                manual_review_required,
                review_priority,
                decision_payload,
                generated_by,
                is_active
            )
            VALUES (
                :claim_id,
                :extraction_id,
                :rule_version,
                :model_version,
                :fraud_risk_score,
                :qc_risk_score,
                CAST(:consistency_checks AS jsonb),
                CAST(:rule_hits AS jsonb),
                :explanation_summary,
                :recommendation,
                :route_target,
                :manual_review_required,
                :review_priority,
                CAST(:decision_payload AS jsonb),
                'checklist_pipeline',
                TRUE
            )
            RETURNING id, generated_at
            """
        ),
        {
            "claim_id": str(claim_id),
            "extraction_id": str(context["extraction_id"]) if context["extraction_id"] else None,
            "rule_version": "legacy-qc-checklist-v1",
            "model_version": ml_prediction.get("model_version") or source_summary.get("catalog_source"),
            "fraud_risk_score": fraud_risk_score,
            "qc_risk_score": qc_risk_score,
            "consistency_checks": json.dumps(consistency_checks),
            "rule_hits": json.dumps(triggered_rule_hits),
            "explanation_summary": summary_text,
            "recommendation": recommendation,
            "route_target": route_target,
            "manual_review_required": manual_review_required,
            "review_priority": review_priority,
            "decision_payload": json.dumps(payload),
        },
    ).mappings().one()

    hybrid_feedback_label = recommendation_to_feedback_label(recommendation)
    hybrid_feedback_captured = False
    if hybrid_feedback_label:
        try:
            trigger_codes = [
                str(item.get("code") or item.get("rule_id") or "").strip()
                for item in triggered_rule_hits
                if isinstance(item, dict)
            ]
            trigger_codes = [code for code in trigger_codes if code]
            hybrid_notes = {
                "triggered_count": len(triggered_rule_hits),
                "trigger_codes": trigger_codes[:50],
                "rule_locked_by_trigger": bool(rule_locked_by_trigger),
                "route_target": route_target,
                "manual_review_required": bool(manual_review_required),
                "review_priority": int(review_priority),
                "openai_merged_used": bool(openai_merged_review),
            }
            hybrid_feedback_captured = upsert_feedback_label(
                db=db,
                claim_id=str(claim_id),
                decision_id=str(row["id"]),
                label_type=HYBRID_LABEL_TYPE,
                label_value=hybrid_feedback_label,
                override_reason="auto_hybrid_pipeline_learning",
                notes=json.dumps(hybrid_notes, ensure_ascii=False),
                created_by=(actor_id or "system:checklist_pipeline"),
            )
        except Exception:
            hybrid_feedback_captured = False
    _emit_workflow_event(
        db=db,
        claim_id=claim_id,
        event_type="claim_checklist_evaluated",
        actor_id=actor_id,
        payload={
            "decision_result_id": str(row["id"]),
            "recommendation": recommendation,
            "route_target": route_target,
            "catalog_source": source_summary.get("catalog_source"),
            "triggered_count": len(triggered_rule_hits),
            "ml_available": ml_prediction.get("available"),
            "ml_label": ml_prediction.get("label"),
            "ml_confidence": ml_prediction.get("confidence"),
            "hybrid_feedback_label": hybrid_feedback_label,
            "hybrid_feedback_captured": hybrid_feedback_captured,
        },
    )

    db.commit()

    return ChecklistRunResponse(
        claim_id=claim_id,
        decision_result_id=row["id"],
        recommendation=recommendation,
        route_target=route_target,
        manual_review_required=manual_review_required,
        review_priority=review_priority,
        generated_at=row["generated_at"],
        checklist=entries,
        source_summary=source_summary,
    )

def get_latest_claim_checklist(db: Session, claim_id: UUID) -> ChecklistLatestResponse:
    context = _collect_claim_context(db, claim_id)

    row = db.execute(
        text(
            """
            SELECT id, recommendation, route_target, manual_review_required, review_priority, generated_at, decision_payload
            FROM decision_results
            WHERE claim_id = :claim_id AND generated_by = 'checklist_pipeline'
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()

    if row is None:
        return ChecklistLatestResponse(found=False, claim_id=claim_id)

    payload = row.get("decision_payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}

    raw_entries = payload.get("checklist") if isinstance(payload.get("checklist"), list) else []
    checklist: list[ChecklistEntry] = []
    for item in raw_entries:
        if isinstance(item, dict):
            try:
                checklist.append(ChecklistEntry.model_validate(item))
            except Exception:
                continue

    source_summary = payload.get("source_summary") if isinstance(payload.get("source_summary"), dict) else {}

    return ChecklistLatestResponse(
        found=True,
        claim_id=claim_id,
        decision_result_id=row["id"],
        recommendation=row.get("recommendation"),
        route_target=row.get("route_target"),
        manual_review_required=bool(row.get("manual_review_required")),
        review_priority=int(row.get("review_priority") or 0),
        generated_at=row.get("generated_at"),
        checklist=checklist,
        source_summary=source_summary,
    )



































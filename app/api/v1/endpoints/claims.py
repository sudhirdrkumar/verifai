import json
import re
from uuid import UUID
from html import unescape

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps.auth import require_roles
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.schemas.auth import UserRole
from app.schemas.claim import (
    ClaimAssignmentRequest,
    ClaimListResponse,
    ClaimReportGrammarCheckRequest,
    ClaimReportGrammarCheckResponse,
    ClaimReportSaveRequest,
    ClaimReportSaveResponse,
    ClaimConclusionGenerateRequest,
    ClaimConclusionGenerateResponse,
    ClaimResponse,
    ClaimStatus,
    ClaimStatusUpdateRequest,
    ClaimStructuredDataRequest,
    ClaimStructuredDataResponse,
    CreateClaimRequest,
)
from app.services.access_control import doctor_matches_assignment
from app.services.auth_service import AuthenticatedUser
from app.services.claims_service import (
    ClaimNotFoundError,
    DuplicateClaimIdError,
    assign_claim,
    create_claim,
    get_claim,
    list_claims,
    update_claim_status,
)
from app.services.claim_structuring_service import (
    ClaimStructuredDataNotFoundError,
    ClaimStructuringError,
    generate_claim_structured_data,
    get_claim_structured_data,
)
from app.services.grammar_service import GrammarCheckError, grammar_check_report_html
from app.services.checklist_pipeline import (
    ClaimNotFoundError as ChecklistClaimNotFoundError,
    get_latest_claim_checklist,
    run_claim_checklist_pipeline,
)

router = APIRouter(prefix="/claims", tags=["claims"])
_ENABLE_AUDITOR_AUTO_RULE_SUGGESTION = False
_STANDARD_REPORT_FONT_FAMILY = "Arial, Helvetica, sans-serif"
_STANDARD_REPORT_FONT_SIZE_PX = 14
_STANDARD_REPORT_LINE_HEIGHT = 1.45


def _normalize_single_doctor_id(raw: str) -> str:
    doctor_id = (raw or "").strip()
    if not doctor_id:
        raise HTTPException(status_code=400, detail="assigned_doctor_id is required")
    if "," in doctor_id:
        raise HTTPException(status_code=400, detail="A case can be assigned to only one doctor")
    return doctor_id


def _normalize_report_html_to_standard_font(report_html: str) -> str:
    raw = str(report_html or "").strip()
    if not raw:
        return ""

    # Avoid nesting repeated wrappers when report is saved multiple times.
    wrapped = re.match(
        r'(?is)^\s*<div\b[^>]*\bdata-report-standardized\s*=\s*["\']1["\'][^>]*>(.*)</div>\s*$',
        raw,
    )
    if wrapped:
        raw = str(wrapped.group(1) or "").strip()

    # Remove prior standardized style block so repeated save keeps exactly one canonical block.
    raw = re.sub(
        r'(?is)<style\b[^>]*\bdata-report-standardized-style\s*=\s*["\']1["\'][^>]*>.*?</style>',
        "",
        raw,
    ).strip()

    # Remove legacy <font> tags and face/size attributes.
    raw = re.sub(r"(?is)</?font\b[^>]*>", "", raw)
    raw = re.sub(r"(?is)\s(?:face|size)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", "", raw)

    def _clean_style_block(style_value: str) -> str:
        pieces: list[str] = []
        for chunk in str(style_value or "").split(";"):
            if ":" not in chunk:
                continue
            prop, val = chunk.split(":", 1)
            prop_clean = prop.strip().lower()
            if prop_clean in {"font-family", "font-size", "font"}:
                continue
            val_clean = val.strip()
            if not prop_clean or not val_clean:
                continue
            pieces.append(f"{prop.strip()}: {val_clean}")
        return "; ".join(pieces)

    def _replace_style_attr(match: re.Match[str]) -> str:
        quote = match.group(1)
        style_value = match.group(2) or ""
        cleaned = _clean_style_block(style_value)
        if not cleaned:
            return ""
        return f' style={quote}{cleaned}{quote}'

    raw = re.sub(r'(?is)\sstyle\s*=\s*(["\'])(.*?)\1', _replace_style_attr, raw)

    standardized_style = (
        f"font-family: {_STANDARD_REPORT_FONT_FAMILY}; "
        f"font-size: {_STANDARD_REPORT_FONT_SIZE_PX}px; "
        f"line-height: {_STANDARD_REPORT_LINE_HEIGHT};"
    )
    standardized_style_block = (
        "<style data-report-standardized-style=\"1\">"
        "[data-report-standardized=\"1\"],"
        "[data-report-standardized=\"1\"] *{"
        f"font-family:{_STANDARD_REPORT_FONT_FAMILY} !important;"
        f"font-size:{_STANDARD_REPORT_FONT_SIZE_PX}px !important;"
        f"line-height:{_STANDARD_REPORT_LINE_HEIGHT} !important;"
        "}"
        "</style>"
    )
    return f'<div data-report-standardized="1" style="{standardized_style}">{standardized_style_block}{raw}</div>'


def _strip_html_to_text(html: str) -> str:
    raw = str(html or "")
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = unescape(raw)
    return re.sub(r"\s+", " ", raw).strip().lower()


def _extract_feedback_label_from_report_html(report_html: str) -> str | None:
    text_value = _strip_html_to_text(report_html)
    if not text_value:
        return None

    if "final recommendation" in text_value:
        if re.search(r"\b(inadmissible|reject(?:ion|ed)?|not justified)\b", text_value):
            return "reject"
        if re.search(r"\b(admissible|approve(?:d)?|payable|justified)\b", text_value):
            return "approve"
        if re.search(r"\b(query|need more evidence|manual review|uncertain)\b", text_value):
            return "need_more_evidence"

    if re.search(r"\bclaim is recommended for rejection\b", text_value):
        return "reject"
    if re.search(r"\bclaim is payable\b", text_value):
        return "approve"
    if re.search(r"\bclaim is kept in query\b", text_value):
        return "need_more_evidence"

    return None


def _feedback_label_from_decision_recommendation(raw: str | None) -> str | None:
    recommendation = str(raw or "").strip().lower()
    if recommendation in {"approve", "approved", "admissible", "payable"}:
        return "approve"
    if recommendation in {"reject", "rejected", "inadmissible"}:
        return "reject"
    if recommendation in {"need_more_evidence", "query", "manual_review"}:
        return "need_more_evidence"
    return None

def _strip_html_to_readable_text(html: str) -> str:
    raw = str(html or "")
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?i)</(p|div|tr|li|section|article|h[1-6])>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = unescape(raw)
    lines = [re.sub(r"\s+", " ", line).strip() for line in str(raw).splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _extract_auditor_learning_from_report_html(report_html: str) -> str | None:
    raw = str(report_html or "")
    if not raw.strip():
        return None

    # Prefer structured table row: <th>Conclusion</th><td>...</td>
    match = re.search(r"(?is)<th[^>]*>\s*Conclusion\s*</th>\s*<td[^>]*>(.*?)</td>", raw)
    candidate = _strip_html_to_readable_text(match.group(1) if match else "")

    if not candidate:
        plain = _strip_html_to_readable_text(raw)
        fallback = re.search(r"(?is)\bConclusion\b\s*[:\-]?\s*(.{30,1400}?)(?:\bRecommendation\b|$)", plain)
        candidate = str(fallback.group(1) if fallback else "").strip()

    candidate = re.sub(r"\bR\d{3}\b\s*[-:]?\s*", "", str(candidate or ""), flags=re.IGNORECASE)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    if len(candidate) < 20:
        return None
    if len(candidate) > 3800:
        candidate = candidate[:3800].rstrip()
    return candidate or None


def _clip_text(value: str, limit: int = 400) -> str:
    text_value = _compact_text(value)
    if not text_value:
        return ""
    if limit <= 0 or len(text_value) <= limit:
        return text_value
    return text_value[: max(20, limit - 3)].rstrip(" ,;:-.") + "..."


def _safe_json_dict(value: object) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _value_to_text(value: object, limit: int = 260) -> str:
    if value is None:
        return ""
    text_value = ""
    if isinstance(value, list):
        parts = [_compact_text(str(item)) for item in value if _compact_text(str(item))]
        text_value = ", ".join(parts[:8])
    elif isinstance(value, dict):
        parts: list[str] = []
        for key, item in list(value.items())[:8]:
            clean_item = _compact_text(str(item))
            if clean_item:
                parts.append(f"{_compact_text(str(key))}: {clean_item}")
        text_value = "; ".join(parts) if parts else _compact_text(json.dumps(value, ensure_ascii=False))
    else:
        text_value = _compact_text(str(value))

    if text_value.lower() in {"-", "na", "n/a", "none", "nil", "not available"}:
        return ""
    return _clip_text(text_value, limit)


def _first_present_text(extracted: dict, structured: dict, extraction_keys: tuple[str, ...], structured_keys: tuple[str, ...], limit: int = 260) -> str:
    for key in extraction_keys:
        if key in extracted:
            out = _value_to_text(extracted.get(key), limit)
            if out:
                return out
    for key in structured_keys:
        if key in structured:
            out = _value_to_text(structured.get(key), limit)
            if out:
                return out
    return ""


def _suggested_rule_decision(learning_label: str | None, recommendation: str | None) -> str:
    label = str(learning_label or "").strip().lower()
    if label == "reject":
        return "REJECT"
    if label == "approve":
        return "APPROVE"
    rec = str(recommendation or "").strip().lower()
    if rec in {"reject", "rejected", "inadmissible"}:
        return "REJECT"
    if rec in {"approve", "approved", "admissible", "payable"}:
        return "APPROVE"
    return "QUERY"


def _create_pending_rule_suggestion_from_learning(
    db: Session,
    claim_id: UUID,
    decision_row: dict | None,
    auditor_learning: str,
    learning_label: str | None,
    actor_id: str,
) -> int | None:
    learning_text = _clip_text(auditor_learning, 2000)
    if not learning_text:
        return None

    suggestions_table_exists = db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'openai_claim_rule_suggestions'
            LIMIT 1
            """
        )
    ).first()
    if not suggestions_table_exists:
        return None

    decision_data = decision_row if isinstance(decision_row, dict) else {}
    recommendation = str(decision_data.get("recommendation") or "").strip()
    suggested_decision = _suggested_rule_decision(learning_label, recommendation)

    source_analysis_id = None
    legacy_analysis_id = decision_data.get("legacy_analysis_id")
    if legacy_analysis_id is not None:
        try:
            parsed_id = int(legacy_analysis_id)
            source_analysis_id = parsed_id if parsed_id > 0 else None
        except (TypeError, ValueError):
            source_analysis_id = None

    decision_payload = _safe_json_dict(decision_data.get("decision_payload"))
    engine_conclusion = _value_to_text(
        decision_payload.get("conclusion") or decision_data.get("explanation_summary"),
        500,
    )

    extraction_row = None
    extraction_id = decision_data.get("extraction_id")
    if extraction_id:
        extraction_row = db.execute(
            text(
                """
                SELECT extracted_entities
                FROM document_extractions
                WHERE id = :extraction_id
                LIMIT 1
                """
            ),
            {"extraction_id": str(extraction_id)},
        ).mappings().first()
    if extraction_row is None:
        extraction_row = db.execute(
            text(
                """
                SELECT extracted_entities
                FROM document_extractions
                WHERE claim_id = :claim_id
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"claim_id": str(claim_id)},
        ).mappings().first()

    extracted_entities = _safe_json_dict(extraction_row.get("extracted_entities") if extraction_row else {})
    structured_table_exists = db.execute(
        text(
            """
            SELECT 1
            FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'claim_structured_data'
            LIMIT 1
            """
        )
    ).first()
    structured_row = None
    if structured_table_exists:
        structured_row = db.execute(
            text(
                """
                SELECT diagnosis, complaints, findings, investigation_finding_in_details,
                       medicine_used, deranged_investigation, conclusion
                FROM claim_structured_data
                WHERE claim_id = :claim_id
                LIMIT 1
                """
            ),
            {"claim_id": str(claim_id)},
        ).mappings().first()
    structured = dict(structured_row) if structured_row else {}

    diagnosis = _first_present_text(
        extracted_entities,
        structured,
        ("diagnosis", "final_diagnosis", "provisional_diagnosis"),
        ("diagnosis",),
        220,
    )
    complaints = _first_present_text(
        extracted_entities,
        structured,
        ("complaints", "chief_complaints", "presenting_complaints"),
        ("complaints",),
        220,
    )
    medicine_used = _first_present_text(
        extracted_entities,
        structured,
        ("medicine_used", "treatment", "medications", "high_end_antibiotic_for_rejection"),
        ("medicine_used",),
        220,
    )
    investigation = _first_present_text(
        extracted_entities,
        structured,
        ("investigation_finding_in_details", "deranged_investigation", "findings", "lab_findings"),
        ("investigation_finding_in_details", "deranged_investigation", "findings"),
        240,
    )
    extraction_conclusion = _first_present_text(
        extracted_entities,
        structured,
        ("detailed_conclusion", "conclusion", "recommendation", "rationale"),
        ("conclusion",),
        350,
    )

    rule_name_seed = learning_text.split(".")[0]
    suggested_name = _clip_text(f"Learning from conclusion and extraction: {rule_name_seed}", 255)

    conditions_bits: list[str] = []
    if diagnosis:
        conditions_bits.append(f"Diagnosis signal: {diagnosis}")
    if complaints:
        conditions_bits.append(f"Complaint signal: {complaints}")
    if medicine_used:
        conditions_bits.append(f"Medicine/treatment signal: {medicine_used}")
    if investigation:
        conditions_bits.append(f"Investigation signal: {investigation}")
    if extraction_conclusion:
        conditions_bits.append(f"Extraction conclusion signal: {extraction_conclusion}")
    conditions_bits.append(f"Auditor conclusion signal: {learning_text}")
    if engine_conclusion:
        conditions_bits.append(f"Engine conclusion context: {engine_conclusion}")
    conditions_bits.append(f"Suggested decision: {suggested_decision}")
    suggested_conditions = _clip_text("; ".join(conditions_bits), 3600)
    if not suggested_conditions:
        suggested_conditions = _clip_text(
            f"Auditor conclusion signal: {learning_text}; Suggested decision: {suggested_decision}",
            3600,
        )

    required_evidence: list[str] = []
    if diagnosis:
        required_evidence.append("diagnosis")
    if complaints:
        required_evidence.append("complaints")
    if medicine_used:
        required_evidence.append("medicine_used")
    if investigation:
        required_evidence.append("investigation_finding_in_details")
    required_evidence.append("auditor_conclusion")
    if engine_conclusion:
        required_evidence.append("decision_conclusion")

    source_context_parts: list[str] = [f"Claim ID: {claim_id}", f"Auditor learning: {learning_text}"]
    if engine_conclusion:
        source_context_parts.append(f"Engine conclusion: {engine_conclusion}")
    if extraction_conclusion:
        source_context_parts.append(f"Extraction conclusion: {extraction_conclusion}")
    if diagnosis:
        source_context_parts.append(f"Diagnosis: {diagnosis}")
    if complaints:
        source_context_parts.append(f"Complaints: {complaints}")
    if medicine_used:
        source_context_parts.append(f"Medicine: {medicine_used}")
    if investigation:
        source_context_parts.append(f"Investigations: {investigation}")
    source_context_text = _clip_text(" | ".join(source_context_parts), 8000)

    confidence = 58
    if diagnosis:
        confidence += 8
    if medicine_used:
        confidence += 8
    if investigation:
        confidence += 8
    if extraction_conclusion:
        confidence += 6
    if engine_conclusion:
        confidence += 6
    if len(learning_text) >= 120:
        confidence += 6
    confidence = max(0, min(95, confidence))

    generator_reasoning = (
        "Auto-created from auditor learning save using latest extraction signals and conclusion context. "
        "This is pending and requires super-admin approval before becoming an active claim rule."
    )
    generator_response_json = {
        "source": "auditor_report_learning",
        "claim_id": str(claim_id),
        "decision_id": str(decision_data.get("id") or ""),
        "extraction_id": str(extraction_id or ""),
        "learning_label": str(learning_label or ""),
        "recommended_decision": suggested_decision,
    }

    if source_analysis_id is not None:
        existing = db.execute(
            text(
                """
                SELECT id
                FROM openai_claim_rule_suggestions
                WHERE source_analysis_id = :source_analysis_id
                LIMIT 1
                """
            ),
            {"source_analysis_id": source_analysis_id},
        ).first()
        if existing:
            return None
    else:
        existing = db.execute(
            text(
                """
                SELECT id
                FROM openai_claim_rule_suggestions
                WHERE claim_id = :claim_id
                  AND status = 'pending'
                  AND suggestion_type = 'new_rule'
                  AND suggested_conditions = :suggested_conditions
                LIMIT 1
                """
            ),
            {"claim_id": str(claim_id), "suggested_conditions": suggested_conditions},
        ).first()
        if existing:
            return None

    inserted = db.execute(
        text(
            """
            INSERT INTO openai_claim_rule_suggestions (
                source_analysis_id,
                claim_id,
                suggestion_type,
                target_rule_id,
                proposed_rule_id,
                suggested_name,
                suggested_decision,
                suggested_conditions,
                suggested_remark_template,
                suggested_required_evidence_json,
                source_context_text,
                generator_confidence,
                generator_reasoning,
                generator_response_json,
                status
            )
            VALUES (
                :source_analysis_id,
                :claim_id,
                'new_rule',
                NULL,
                NULL,
                :suggested_name,
                :suggested_decision,
                :suggested_conditions,
                :suggested_remark_template,
                CAST(:required_evidence_json AS jsonb),
                :source_context_text,
                :generator_confidence,
                :generator_reasoning,
                CAST(:generator_response_json AS jsonb),
                'pending'
            )
            RETURNING id
            """
        ),
        {
            "source_analysis_id": source_analysis_id,
            "claim_id": str(claim_id),
            "suggested_name": suggested_name or "Learning from conclusion and extraction",
            "suggested_decision": suggested_decision,
            "suggested_conditions": suggested_conditions,
            "suggested_remark_template": (
                f"Rule suggestion generated from auditor conclusion context. "
                f"Super-admin approval required. Suggested outcome: {suggested_decision}."
            ),
            "required_evidence_json": json.dumps(required_evidence),
            "source_context_text": source_context_text,
            "generator_confidence": confidence,
            "generator_reasoning": generator_reasoning,
            "generator_response_json": json.dumps(generator_response_json),
        },
    ).mappings().first()

    if inserted:
        db.execute(
            text(
                """
                INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
                VALUES (:claim_id, 'user', :actor_id, 'rule_suggestion_created_from_learning', CAST(:event_payload AS jsonb))
                """
            ),
            {
                "claim_id": str(claim_id),
                "actor_id": actor_id,
                "event_payload": json.dumps(
                    {
                        "suggestion_id": int(inserted["id"]),
                        "source": "auditor_report_learning",
                        "decision": suggested_decision,
                    }
                ),
            },
        )
        return int(inserted["id"])
    return None


def _normalize_label_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _compact_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _trim_for_conclusion(value: str, limit: int, default: str = "") -> str:
    text_value = _compact_text(value)
    if not text_value:
        return default
    lim = int(limit or 0)
    if lim <= 0 or len(text_value) <= lim:
        return text_value
    clipped = text_value[: max(20, lim)]
    return clipped.rstrip(" ,;:-.")


def _extract_report_table_rows(report_html: str) -> dict[str, str]:
    rows: dict[str, str] = {}
    raw = str(report_html or "")
    if not raw.strip():
        return rows

    for tr_match in re.finditer(r"(?is)<tr[^>]*>(.*?)</tr>", raw):
        segment = str(tr_match.group(1) or "")
        th_match = re.search(r"(?is)<th[^>]*>(.*?)</th>", segment)
        td_match = re.search(r"(?is)<td[^>]*>(.*?)</td>", segment)
        if not th_match or not td_match:
            continue
        key = _normalize_label_key(_strip_html_to_readable_text(th_match.group(1)))
        value = _compact_text(_strip_html_to_readable_text(td_match.group(1)))
        if not key or not value or key in rows:
            continue
        rows[key] = value
    return rows


def _pick_report_row_value(rows: dict[str, str], aliases: list[str]) -> str:
    for alias in aliases:
        key = _normalize_label_key(alias)
        if key and rows.get(key):
            return str(rows.get(key) or "")
    return ""


def _parse_age_years(value: str) -> str:
    text_value = str(value or "")
    match = re.search(r"\b(\d{1,3})\s*(?:years?|yrs?|yr|y)\b", text_value, flags=re.I)
    if not match:
        match = re.search(r"\b(\d{1,3})\b", text_value)
    if not match:
        return ""
    years = int(match.group(1))
    if years <= 0 or years > 120:
        return ""
    return str(years)


def _parse_gender_word(value: str) -> str:
    text_value = str(value or "").lower()
    if not text_value:
        return ""
    if re.search(r"\b(?:male|man|boy|m)\b", text_value, flags=re.I):
        return "man"
    if re.search(r"\b(?:female|woman|girl|f)\b", text_value, flags=re.I):
        return "woman"
    return ""


def _build_patient_phrase(insured_text: str) -> str:
    age = _parse_age_years(insured_text)
    if age:
        return f"{age}yr old patient"
    gender = _parse_gender_word(insured_text)
    if gender == "man":
        return "Male patient"
    if gender == "woman":
        return "Female patient"
    return "Patient"


def _is_checklist_rule_source(source: str) -> bool:
    src = str(source or "").strip().lower()
    return src.startswith("openai_claim_rules") or src.startswith("openai_diagnosis_criteria")


def _extract_rule_code(value: str) -> str:
    match = re.search(r"\bR\d{3}\b", str(value or "").upper())
    return str(match.group(0) if match else "")


def _extract_rule_code_from_entry(entry: dict) -> str:
    if not isinstance(entry, dict):
        return ""
    for key in ("code", "rule_id", "name", "title", "note", "summary", "reason"):
        code = _extract_rule_code(str(entry.get(key) or ""))
        if code:
            return code
    return ""


def _strip_rule_tokens(value: str) -> str:
    txt = _compact_text(value)
    txt = re.sub(r"\bOPENAI_MERGED_REVIEW\b", "", txt, flags=re.I)
    txt = re.sub(r"\b[Rr]\d{3}\b\s*[-:]\s*", "", txt)
    txt = re.sub(r"\bDX\d{3}\b\s*[-:]\s*", "", txt, flags=re.I)
    txt = re.sub(r"\bMissing evidence\s*:[^.;\n]*(?:[.;]|$)", " ", txt, flags=re.I)
    txt = re.sub(r"\bLearning signal\s*:[^.]*\.?", "", txt, flags=re.I)
    txt = re.sub(r"\bGST guardrail triggered\s*:[^.;\n]*(?:[.;]|$)", " ", txt, flags=re.I)
    txt = re.sub(r"\bAuto-learned rule from auditor conclusion and extraction context\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bSuper-admin approval required\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bSuggested outcome\s*:\s*[A-Z_]+\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\bTriggered condition identified\.?", " ", txt, flags=re.I)
    txt = re.sub(r"\btherefore,\s*one or more clinical rules are triggered\.?", "", txt, flags=re.I)
    txt = re.sub(r"\btherefore,\s*this rule is triggered\.?", "", txt, flags=re.I)
    txt = re.sub(r"\b(?:one or more\s+)?clinical rules?\s+are\s+triggered\.?", "", txt, flags=re.I)
    txt = re.sub(r"\bthis rule is triggered\.?", "", txt, flags=re.I)
    txt = re.sub(r"\btriggers this exclusion/review rule\.?", "materially impacts claim assessment.", txt, flags=re.I)
    txt = re.sub(r"\bChecklist condition matched and evidence pattern triggered\b[^.;\n]*(?:[.;]|$)", " ", txt, flags=re.I)
    machine_term = r"(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion|complaints)"
    if re.search(r"\bChecklist condition matched and evidence pattern triggered\b", txt, flags=re.I):
        return ""
    if re.search(r"\bGST guardrail triggered\b", txt, flags=re.I):
        return ""
    if re.search(r"\b(?:investigation_finding_in_details|auditor_conclusion|decision_conclusion)\b", txt, flags=re.I):
        return ""
    if re.fullmatch(rf"{machine_term}(?:\s*,\s*{machine_term})*", txt.strip(), flags=re.I):
        return ""
    txt = re.sub(r"\s+", " ", txt).strip(" .;:-")
    return txt


def _extract_antibiotic_names_for_conclusion(medicine_text: str, high_end_signal: str, max_items: int = 3) -> str:
    combined = "\n".join([_compact_text(medicine_text), _compact_text(high_end_signal)]).strip()
    if not combined:
        return ""

    pattern = re.compile(
        r"(meropenem|imipenem|ertapenem|doripenem|piperacillin|tazobactam|pip\s*-?\s*taz|ceftriaxone|cefotaxime|cefoperazone|sulbactam|cefepime|ceftazidime|amikacin|linezolid|colistin|teicoplanin|vancomycin|tigecycline|azithromycin|levofloxacin|ofloxacin|ciprofloxacin|amox(?:i|y)clav|amoxycillin|monocef)",
        flags=re.I,
    )

    found: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\r\n;,]+", combined):
        cleaned = _compact_text(chunk)
        cleaned = re.sub(r"^[-\d.()\s]+", "", cleaned)
        cleaned = re.sub(r"\b(?:inj|injection|tab|tablet|cap|capsule|syp|syrup|iv|im|po|od|bd|tid|qid|hs|stat)\b\.?", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"\b\d+(?:\.\d+)?\s*(?:mg|gm|g|ml|mcg|iu|units?)\b", " ", cleaned, flags=re.I)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,;:-")
        if not cleaned:
            continue
        if not pattern.search(cleaned):
            continue
        key = re.sub(r"[^a-z0-9]+", "", cleaned.lower())
        if not key or key in seen:
            continue
        seen.add(key)
        found.append(cleaned)

    if not found and re.search(r"high\s*-?end\s*antibiotic|meropenem", combined, flags=re.I):
        return "high-end antibiotic therapy"

    return ", ".join(found[: max(1, int(max_items or 3))])


def _rule_line_by_code(code: str, abx_label: str) -> str:
    mapping = {
        "R001": f"Use of {abx_label} is not supported by sepsis markers or objective evidence of severe infection.",
        "R005": "The diagnosis of sepsis is not substantiated as relevant sepsis markers, culture reports, and infection work-up are not adequately documented.",
        "R003": f"Pneumonia is not adequately established in view of negative/non-supportive imaging, absence of culture evidence, and unjustified use of {abx_label}.",
        "R004": "UTI management is not sufficiently supported because urine culture/sensitivity correlation is absent or not aligned with the prescribed treatment.",
        "R002": "The indication for ORIF is not justified as records do not demonstrate displaced, unstable, or otherwise surgically indicated fracture morphology.",
        "R006": f"{abx_label} administration is not supported by abnormal vitals, laboratory markers, or other objective evidence of serious infection.",
        "R009": "The fracture described appears hairline/minimally severe in nature, and the necessity for surgical fixation is not established from available records.",
        "R010": "Available imaging/clinical records suggest stable or undisplaced fracture, for which ORIF/K-wire fixation lacks adequate justification.",
        "R011": "Fracture diagnosis and related billing are inadequately supported due to absence of relevant X-ray evidence and/or corresponding bill support.",
        "R007": "The claim is not admissible as the treating Ayurvedic facility's accreditation/registration documents are not available for verification.",
        "R008": "History suggestive of alcoholism in the context of chronic liver disease materially impacts claim assessment.",
        "R013": f"UTI treatment with {abx_label} is supported by culture and sensitivity evidence; therefore, the treatment is considered justified under this rule.",
        "R014": "Though the bill amount is below the usual threshold, the treatment flow is not consistent with simple OPD management, and the override is applicable.",
        "R015": "The claim falls under high-bill maternity/LSCS/neonatal jaundice override criteria and is therefore considered under the applicable exception pathway.",
        "R016": "Sepsis justification requires a combination of abnormal vitals, inflammatory/infective markers, and culture support; absence of these elements weakens the diagnosis.",
        "R018": "Pharmacy GST validation failed against verified GST registration details for the submitted vendor context.",
    }
    return str(mapping.get(str(code or "").upper()) or "")


def _reason_label_from_recommendation(recommendation: str) -> str:
    rec = str(recommendation or "").strip().lower()
    if rec in {"approve", "approved", "admissible", "payable"}:
        return "approval"
    if rec in {"query", "need_more_evidence", "manual_review", "pending"}:
        return "query"
    return "rejection"


def _build_rule_based_conclusion_from_report(report_html: str, checklist_payload: dict) -> tuple[str, int]:
    rows = _extract_report_table_rows(report_html)
    insured_text = _pick_report_row_value(rows, ["INSURED", "PATIENT", "PATIENT DETAILS"])
    diagnosis = _trim_for_conclusion(_pick_report_row_value(rows, ["DIAGNOSIS"]), 180, "unspecified diagnosis")
    complaints = _trim_for_conclusion(
        _pick_report_row_value(rows, ["CHIEF COMPLAINTS AT ADMISSION", "CHIEF COMPLAINTS", "CHIEF COMPLAINT"]),
        240,
        "unspecified complaints",
    )
    treatments = _trim_for_conclusion(
        _pick_report_row_value(rows, ["MEDICINE EVIDENCE USED", "MEDICINES USED", "TREATMENT"]),
        240,
        "supportive treatment",
    )
    deranged = _trim_for_conclusion(
        _pick_report_row_value(rows, ["DERANGED INVESTIGATION REPORTS", "DERANGED INVESTIGATION"]),
        220,
        "no significant deranged values documented",
    )

    plain_report_text = _strip_html_to_readable_text(report_html)
    high_end_signal = _pick_report_row_value(
        rows,
        [
            "HIGH-END ANTIBIOTIC CHECK",
            "HIGH END ANTIBIOTIC CHECK",
            "HIGH END ANTIBIOTIC FOR REJECTION",
            "HIGH_END_ANTIBIOTIC_FOR_REJECTION",
        ],
    )
    if not high_end_signal:
        match = re.search(r"(?is)high\s*-?end\s*antibiotic[^:\n]*[:\-]?\s*([^\n]+)", plain_report_text)
        high_end_signal = _compact_text(match.group(1) if match else "")

    abx_names = _extract_antibiotic_names_for_conclusion(treatments, high_end_signal, 3)
    abx_label = abx_names or "Meropenem/high-end antibiotic"

    checklist_rows = checklist_payload.get("checklist") if isinstance(checklist_payload.get("checklist"), list) else []
    triggered_count = 0
    seen_codes: set[str] = set()
    seen_lines: set[str] = set()
    rule_lines: list[str] = []

    for entry in checklist_rows:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("triggered")):
            continue
        if not _is_checklist_rule_source(str(entry.get("source") or "")):
            continue
        if re.search(r"\blearning from conclusion and extraction\b", str(entry.get("name") or entry.get("title") or ""), flags=re.I):
            continue
        triggered_count += 1

        code = _extract_rule_code_from_entry(entry)
        if not re.fullmatch(r"R\d{3}", code):
            continue
        mapped_line = ""
        if code and code not in seen_codes:
            seen_codes.add(code)
            mapped_line = _strip_rule_tokens(_rule_line_by_code(code, abx_label))
        if mapped_line:
            key = mapped_line.lower()
            if key not in seen_lines:
                seen_lines.add(key)
                rule_lines.append(mapped_line)
            continue

        fallback = _strip_rule_tokens(
            str(entry.get("note") or entry.get("why_triggered") or entry.get("summary") or entry.get("reason") or "")
        )
        if fallback:
            key = fallback.lower()
            if key not in seen_lines:
                seen_lines.add(key)
                rule_lines.append(fallback)

    reporting = checklist_payload.get("source_summary") if isinstance(checklist_payload.get("source_summary"), dict) else {}
    reporting_obj = reporting.get("reporting") if isinstance(reporting.get("reporting"), dict) else {}
    reporting_conclusion = _strip_rule_tokens(str(reporting_obj.get("conclusion") or ""))
    if not rule_lines and reporting_conclusion:
        rule_lines.append(reporting_conclusion)

    reason_text = "; ".join(rule_lines[:3]) if rule_lines else "clinical evidence is incomplete for final admissibility decision"
    recommendation = str(checklist_payload.get("recommendation") or _pick_report_row_value(rows, ["FINAL RECOMMENDATION", "RECOMMENDATION"]))
    reason_label = _reason_label_from_recommendation(recommendation)

    conclusion = (
        f"{_build_patient_phrase(insured_text)} with chief complaint of {complaints}, diagnosis of {diagnosis}, "
        f"treated with following {treatments}, and deranged investigation report of {deranged}. "
        f"Reason for {reason_label}: {reason_text}."
    )
    conclusion = re.sub(r"\s+", " ", str(conclusion or "")).strip()
    conclusion = _strip_rule_tokens(conclusion)
    if not conclusion:
        conclusion = "Patient with available clinical complaints and diagnosis was reviewed. Reason for query: clinical evidence is incomplete for final admissibility decision."
    return conclusion, triggered_count
_ALLOWED_CONCLUSION_ENDINGS = {
    "Therefore, the claim is admissible.",
    "Therefore, the claim is recommended for rejection.",
    "Therefore, the claim is kept under query.",
}


def _extract_openai_response_text_for_claims(body: dict) -> str:
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


def _dedupe_model_candidates(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        model = str(value or "").strip()
        if not model:
            continue
        key = model.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(model)
    return out


def _resolve_conclusion_llm_targets() -> list[dict[str, str | list[str]]]:
    targets: list[dict[str, str | list[str]]] = []

    openai_key = str(settings.openai_api_key or "").strip()
    if openai_key:
        openai_base = str(settings.openai_base_url or "").strip().rstrip("/") or "https://api.openai.com/v1"
        openai_models = _dedupe_model_candidates([settings.openai_rag_model, settings.openai_model, "gpt-4.1-mini", "gpt-4o-mini"])
        if openai_models:
            targets.append(
                {
                    "provider": "openai",
                    "base_url": openai_base,
                    "api_key": openai_key,
                    "models": openai_models,
                }
            )

    deepseek_key = str(settings.deepseek_api_key or "").strip()
    if deepseek_key:
        deepseek_base = str(settings.deepseek_base_url or "").strip().rstrip("/") or "https://api.deepseek.com/v1"
        deepseek_models = _dedupe_model_candidates([settings.deepseek_model, "deepseek-v3.2", "deepseek-chat"])
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


def _build_latest_extraction_context_for_conclusion(db: Session, claim_id: UUID) -> str:
    row = db.execute(
        text(
            """
            SELECT extracted_entities
            FROM document_extractions
            WHERE claim_id = :claim_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if not row:
        return ""

    extracted = _safe_json_dict(row.get("extracted_entities"))
    if not extracted:
        return ""

    fields_map: list[tuple[str, tuple[str, ...]]] = [
        ("Diagnosis", ("diagnosis",)),
        ("Chief Complaints", ("chief_complaints_at_admission", "chief_complaints", "complaints")),
        ("Clinical Findings", ("major_diagnostic_finding", "clinical_findings", "findings")),
        ("Investigations", ("deranged_investigation", "all_investigation_reports_with_values", "investigation_finding_in_details")),
        ("Medicine Evidence", ("medicine_used", "medicines_used", "treatment_medicines")),
        ("Final Recommendation", ("final_recommendation", "recommendation")),
        ("Extraction Conclusion", ("detailed_conclusion", "conclusion")),
    ]

    lines: list[str] = []
    for label, aliases in fields_map:
        for key in aliases:
            if key not in extracted:
                continue
            value = _value_to_text(extracted.get(key), 360)
            if value:
                lines.append(f"{label}: {value}")
                break

    if not lines:
        for key, value in list(extracted.items())[:8]:
            txt = _value_to_text(value, 220)
            if txt:
                lines.append(f"{_compact_text(str(key))}: {txt}")

    return "\n".join(lines[:12]).strip()


def _verdict_sentence_from_recommendation(recommendation: str) -> str:
    rec = str(recommendation or "").strip().lower()
    if rec in {"approve", "approved", "admissible", "payable"}:
        return "Therefore, the claim is admissible."
    if rec in {"reject", "rejected", "inadmissible"}:
        return "Therefore, the claim is recommended for rejection."
    return "Therefore, the claim is kept under query."


def _normalize_ai_conclusion_paragraph(value: str, recommendation: str) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    text_value = re.sub(r"^```(?:text|markdown)?\s*", "", text_value, flags=re.I)
    text_value = re.sub(r"\s*```$", "", text_value).strip()
    text_value = re.sub(r"<[^>]+>", " ", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    text_value = text_value.strip('"\'` ')

    for ending in _ALLOWED_CONCLUSION_ENDINGS:
        if text_value.endswith(ending):
            return text_value

    verdict = _verdict_sentence_from_recommendation(recommendation)
    text_value = text_value.rstrip(" .") + ". " + verdict
    return re.sub(r"\s+", " ", text_value).strip()


def _generate_ai_medico_legal_conclusion(
    report_html: str,
    checklist_payload: dict,
    recommendation: str,
    extraction_context: str = "",
) -> str:
    targets = _resolve_conclusion_llm_targets()
    if not targets:
        raise RuntimeError("Neither OPENAI_API_KEY nor DEEPSEEK_API_KEY is configured")

    report_text = _strip_html_to_readable_text(report_html)
    if not report_text:
        raise RuntimeError("report text is empty")
    if len(report_text) > 50000:
        report_text = report_text[:50000]
    extraction_context = str(extraction_context or "").strip()
    if len(extraction_context) > 12000:
        extraction_context = extraction_context[:12000]

    checklist_rows = checklist_payload.get("checklist") if isinstance(checklist_payload.get("checklist"), list) else []
    triggered_codes: list[str] = []
    seen_codes: set[str] = set()
    for entry in checklist_rows:
        if not isinstance(entry, dict):
            continue
        if not bool(entry.get("triggered")):
            continue
        if not _is_checklist_rule_source(str(entry.get("source") or "")):
            continue
        if re.search(r"\blearning from conclusion and extraction\b", str(entry.get("name") or entry.get("title") or ""), flags=re.I):
            continue
        code = _extract_rule_code_from_entry(entry)
        if not code or not re.fullmatch(r"R\d{3}", code) or code in seen_codes:
            continue
        seen_codes.add(code)
        triggered_codes.append(code)

    trigger_hint = ", ".join(triggered_codes) if triggered_codes else "No explicit triggered rule code in latest checklist payload"

    rules_text = (
        "R001 - Meropenem/high-end antibiotic without sepsis markers\n"
        "R002 - ORIF billed without displaced/unstable fracture indication\n"
        "R003 - Pneumonia imaging negative + no culture + high-end antibiotic\n"
        "R004 - UTI without urine culture/sensitivity correlation\n"
        "R005 - Sepsis diagnosis must have markers and culture/work-up\n"
        "R006 - High-end antibiotic not supported by vitals/objective evidence\n"
        "R007 - Ayurvedic hospital accreditation/registration missing\n"
        "R008 - Alcoholism history with CLD context\n"
        "R009 - Hairline fracture in surgical fixation claim\n"
        "R010 - Stable/undisplaced fracture without ORIF/K-wire indication\n"
        "R011 - Fracture case missing X-ray evidence and billing support\n"
        "R013 - UTI + Meropenem supported by culture sensitivity evidence\n"
        "R014 - Low bill but admission justified override\n"
        "R015 - Maternity/LSCS/neonatal override\n"
        "R016 - Sepsis requires combined evidence of vitals, markers, and culture"
    )

    user_prompt = (
        "You are a senior medical claim investigator and audit specialist with expertise in insurance claim adjudication, clinical documentation review, medical necessity assessment, and medico-legal audit writing.\n\n"
        "Your task is to review the Health Claim Assessment Sheet provided below and generate a single professional conclusion paragraph suitable for TPA/insurance audit use.\n\n"
        "REVIEW OBJECTIVES:\n"
        "1. Examine the full report clinically, logically, and documentarily.\n"
        "2. Apply all relevant rules from R001 to R016.\n"
        "3. Determine whether diagnosis, investigations, treatment, admission, and billing are mutually consistent.\n"
        "4. Identify contradictions, unsupported treatment, missing evidence, weak justification, or incorrect reasoning.\n"
        "5. Assess whether the existing report conclusion is supported by available records.\n"
        "6. Produce a final medico-legal conclusion in one paragraph only.\n"
        "7. For rejection/query outcomes, clearly include the culprit medicine(s) and investigation basis for rejection.\n\n"
        "RULES TO BE APPLIED:\n"
        + rules_text
        + "\n\nSTRICT INSTRUCTIONS:\n"
        "1. Apply every relevant rule and synthesize findings into clear clinical reasoning.\n"
        "2. Do not mention internal category labels.\n"
        "3. Cross-check diagnosis vs findings/investigations, treatment vs severity, antibiotic/procedure support, LOS necessity, and billing intensity.\n"
        "4. If report conclusion is unsupported, explicitly state it is not consistent with available records.\n"
        "5. Maintain formal, objective, concise medico-legal language.\n"
        "6. No bullets, headings, or labels in output.\n"
        "7. Output must be exactly one paragraph.\n"
        "8. Last sentence must be exactly one of: Therefore, the claim is admissible. OR Therefore, the claim is recommended for rejection. OR Therefore, the claim is kept under query.\n"
        "9. If recommendation is rejection or query, explicitly name the culprit medicine(s) (for example Meropenem/high-end antibiotic when applicable) and state the investigation basis (missing/contradictory labs, cultures, imaging, vitals, or other objective findings) in the same paragraph.\n"
        "10. Keep the conclusion detailed but still a single paragraph with no extra sections.\n\n"
        "ENGINE CLINICAL HINTS: "
        + trigger_hint
        + "\n\nLATEST EXTRACTION SNAPSHOT (from document extraction):\n"
        + (extraction_context or "not available")
        + "\n\nHEALTH CLAIM ASSESSMENT SHEET:\n"
        + report_text
    )

    def _generate_from_targets(
        candidate_targets: list[dict[str, str | list[str]]],
        prompt_text: str,
        errors_bucket: list[str],
    ) -> str:
        for target in candidate_targets:
            provider = str(target.get("provider") or "llm")
            base_url = str(target.get("base_url") or "").strip().rstrip("/")
            api_key = str(target.get("api_key") or "").strip()
            models = target.get("models") if isinstance(target.get("models"), list) else []
            if not base_url or not api_key or not models:
                continue

            url = f"{base_url}/chat/completions"
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            for model in models:
                model_name = str(model or "").strip()
                if not model_name:
                    continue
                request_payload = {
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": "You are a medico-legal claim audit writer. Return exactly one paragraph only."},
                        {"role": "user", "content": prompt_text},
                    ],
                    "temperature": 0.1,
                }
                try:
                    with httpx.Client(timeout=120.0) as client:
                        response = client.post(url, headers=headers, json=request_payload)
                        response.raise_for_status()
                    raw = _extract_openai_response_text_for_claims(response.json())
                    normalized = _normalize_ai_conclusion_paragraph(raw, recommendation)
                    if normalized:
                        return normalized
                    errors_bucket.append(f"{provider}:{model_name}: empty_output")
                except Exception as exc:
                    errors_bucket.append(f"{provider}:{model_name}: {exc}")
        return ""

    openai_targets = [t for t in targets if str(t.get("provider") or "").strip().lower() == "openai"]
    deepseek_targets = [t for t in targets if str(t.get("provider") or "").strip().lower() == "deepseek"]
    other_targets = [t for t in targets if t not in openai_targets and t not in deepseek_targets]

    errors: list[str] = []
    openai_conclusion = _generate_from_targets(openai_targets, user_prompt, errors)

    # For conclusion flow, always pass OpenAI draft (when available) to DeepSeek v3.2 stage.
    deepseek_prompt = user_prompt
    if openai_conclusion:
        deepseek_prompt = (
            user_prompt
            + "\n\nOPENAI DRAFT CONCLUSION (refine/correct this using full report evidence):\n"
            + openai_conclusion
        )

    deepseek_conclusion = _generate_from_targets(deepseek_targets, deepseek_prompt, errors)
    if deepseek_conclusion:
        return deepseek_conclusion
    if openai_conclusion:
        return openai_conclusion

    fallback_conclusion = _generate_from_targets(other_targets, user_prompt, errors)
    if fallback_conclusion:
        return fallback_conclusion

    raise RuntimeError(f"ai conclusion generation failed: {errors[:3] or ['unknown']}")

@router.post("", response_model=ClaimResponse, status_code=status.HTTP_201_CREATED)
def create_claim_endpoint(
    payload: CreateClaimRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> ClaimResponse:
    try:
        return create_claim(db, payload, actor_id=current_user.username)
    except DuplicateClaimIdError as exc:
        raise HTTPException(status_code=409, detail="external_claim_id already exists") from exc


@router.get("", response_model=ClaimListResponse)
def list_claims_endpoint(
    status_filter: ClaimStatus | None = Query(default=None, alias="status"),
    assigned_doctor_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimListResponse:
    effective_doctor = assigned_doctor_id
    if current_user.role == UserRole.doctor:
        effective_doctor = current_user.username
    return list_claims(db, status_filter, effective_doctor, limit, offset)


@router.get("/{claim_id}", response_model=ClaimResponse)
def get_claim_endpoint(
    claim_id: UUID,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimResponse:
    try:
        claim = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(claim.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can access only assigned claims")

    return claim


@router.patch("/{claim_id}/status", response_model=ClaimResponse)
def update_claim_status_endpoint(
    claim_id: UUID,
    payload: ClaimStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimResponse:
    try:
        existing = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can update only assigned claims")

    if current_user.role == UserRole.auditor:
        if payload.status != ClaimStatus.in_review:
            raise HTTPException(status_code=400, detail="auditor can only send case back to doctor (in_review)")
        auditor_note = str(payload.note or "").strip()
        if not auditor_note:
            raise HTTPException(status_code=400, detail="auditor opinion is required")
        enriched_payload = payload.model_copy(update={"actor_id": payload.actor_id or current_user.username, "note": auditor_note})
    else:
        enriched_payload = payload.model_copy(update={"actor_id": payload.actor_id or current_user.username})

    try:
        return update_claim_status(db, claim_id, enriched_payload)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc


@router.patch("/{claim_id}/assign", response_model=ClaimResponse)
def assign_claim_endpoint(
    claim_id: UUID,
    payload: ClaimAssignmentRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> ClaimResponse:
    assigned_doctor_id = _normalize_single_doctor_id(payload.assigned_doctor_id)
    enriched_payload = payload.model_copy(
        update={
            "assigned_doctor_id": assigned_doctor_id,
            "actor_id": payload.actor_id or current_user.username,
        }
    )
    try:
        return assign_claim(db, claim_id, enriched_payload)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

@router.post("/{claim_id}/reports/html", response_model=ClaimReportSaveResponse, status_code=status.HTTP_201_CREATED)
def save_claim_report_html_endpoint(
    claim_id: UUID,
    payload: ClaimReportSaveRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimReportSaveResponse:
    try:
        existing = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can save report only for assigned claims")

    report_html = (payload.report_html or "").strip()
    report_html = report_html.replace("HEALTH CLAIM INVESTIGATION REPORT", "HEALTH CLAIM ASSESSMENT SHEET")
    if current_user.role in {UserRole.doctor, UserRole.auditor}:
        report_html = _normalize_report_html_to_standard_font(report_html)
    if not report_html:
        raise HTTPException(status_code=400, detail="report_html is required")
    if len(report_html) > 2_000_000:
        raise HTTPException(status_code=413, detail="report_html too large")

    # TODO: Grammar check disabled temporarily for performance. Re-enable with async processing.
    # try:
    #     grammar_result = grammar_check_report_html(report_html)
    #     report_html = grammar_result.get("corrected_html", report_html)
    #     logger.info(f"Grammar check applied: {grammar_result.get('checked_segments', 0)} segments checked, {grammar_result.get('corrected_segments', 0)} corrected")
    # except GrammarCheckError as exc:
    #     logger.warning(f"Grammar check failed, continuing with uncorrected report: {exc}")
    # except Exception as exc:
    #     logger.warning(f"Unexpected error during grammar check: {exc}")

    report_status = (payload.report_status or "draft").strip().lower() or "draft"
    allowed_status = {"draft", "completed", "uploaded", "final"}
    if report_status not in allowed_status:
        raise HTTPException(status_code=400, detail=f"invalid report_status. allowed: {', '.join(sorted(allowed_status))}")

    report_source = (payload.report_source or "doctor").strip().lower() or "doctor"
    if report_source not in {"doctor", "system"}:
        raise HTTPException(status_code=400, detail="invalid report_source. allowed: doctor, system")

    actor_id = (payload.actor_id or current_user.username or "").strip() or current_user.username
    created_by = actor_id
    if report_source == "system":
        created_by = actor_id if actor_id.lower().startswith("system:") else f"system:{actor_id}"

    decision_row = db.execute(
        text(
            """
            SELECT id, recommendation, extraction_id, legacy_analysis_id, explanation_summary, decision_payload
            FROM decision_results
            WHERE claim_id = :claim_id
            ORDER BY generated_at DESC
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    decision_id = decision_row.get("id") if decision_row else None
    decision_recommendation = str(decision_row.get("recommendation") or "") if decision_row else ""

    version_no = int(
        db.execute(
            text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM report_versions WHERE claim_id = :claim_id"),
            {"claim_id": str(claim_id)},
        ).scalar_one()
        or 1
    )

    row = db.execute(
        text(
            """
            INSERT INTO report_versions (
                claim_id,
                decision_id,
                version_no,
                report_status,
                report_markdown,
                export_uri,
                created_by
            )
            VALUES (
                :claim_id,
                :decision_id,
                :version_no,
                :report_status,
                :report_markdown,
                '',
                :created_by
            )
            RETURNING id, claim_id, decision_id, version_no, report_status, created_by, created_at
            """
        ),
        {
            "claim_id": str(claim_id),
            "decision_id": str(decision_id) if decision_id else None,
            "version_no": version_no,
            "report_status": report_status,
            "report_markdown": report_html,
            "created_by": created_by,
        },
    ).mappings().one()

    db.execute(
        text(
            """
            INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
            VALUES (:claim_id, 'user', :actor_id, 'report_saved_html', CAST(:event_payload AS jsonb))
            """
        ),
        {
            "claim_id": str(claim_id),
            "actor_id": actor_id,
            "event_payload": json.dumps({"version_no": version_no, "report_status": report_status, "report_source": report_source}),
        },
    )

    feedback_label_value = _extract_feedback_label_from_report_html(report_html)
    if not feedback_label_value:
        feedback_label_value = _feedback_label_from_decision_recommendation(decision_recommendation)
    if report_source == "doctor" and feedback_label_value:
        db.execute(
            text(
                """
                DELETE FROM feedback_labels
                WHERE claim_id = :claim_id AND label_type = 'doctor_report_outcome'
                """
            ),
            {"claim_id": str(claim_id)},
        )
        db.execute(
            text(
                """
                INSERT INTO feedback_labels (
                    claim_id,
                    decision_id,
                    label_type,
                    label_value,
                    override_reason,
                    notes,
                    created_by
                )
                VALUES (
                    :claim_id,
                    :decision_id,
                    'doctor_report_outcome',
                    :label_value,
                    'doctor_report_saved_html',
                    :notes,
                    :created_by
                )
                """
            ),
            {
                "claim_id": str(claim_id),
                "decision_id": str(row.get("decision_id") or "") or None,
                "label_value": feedback_label_value,
                "notes": f"Auto label from doctor report HTML (version {version_no}, status={report_status}).",
                "created_by": current_user.username,
            },
        )


    if current_user.role in {UserRole.auditor, UserRole.super_admin} and report_source == "doctor":
        auditor_learning = _extract_auditor_learning_from_report_html(report_html)
        db.execute(
            text(
                """
                DELETE FROM feedback_labels
                WHERE claim_id = :claim_id AND label_type = 'auditor_report_learning'
                """
            ),
            {"claim_id": str(claim_id)},
        )
        if auditor_learning:
            auditor_learning_label = feedback_label_value or _feedback_label_from_decision_recommendation(decision_recommendation) or "manual_review"
            db.execute(
                text(
                    """
                    INSERT INTO feedback_labels (
                        claim_id,
                        decision_id,
                        label_type,
                        label_value,
                        override_reason,
                        notes,
                        created_by
                    )
                    VALUES (
                        :claim_id,
                        :decision_id,
                        'auditor_report_learning',
                        :label_value,
                        'auditor_report_saved_html',
                        :notes,
                        :created_by
                    )
                    """
                ),
                {
                    "claim_id": str(claim_id),
                    "decision_id": str(row.get("decision_id") or "") or None,
                    "label_value": auditor_learning_label,
                    "notes": auditor_learning,
                    "created_by": current_user.username,
                },
            )
            if _ENABLE_AUDITOR_AUTO_RULE_SUGGESTION:
                _create_pending_rule_suggestion_from_learning(
                    db=db,
                    claim_id=claim_id,
                    decision_row=dict(decision_row) if decision_row else None,
                    auditor_learning=auditor_learning,
                    learning_label=auditor_learning_label,
                    actor_id=current_user.username,
                )

    db.commit()

    return ClaimReportSaveResponse(
        id=row["id"],
        claim_id=row["claim_id"],
        decision_id=row.get("decision_id"),
        version_no=int(row["version_no"]),
        report_status=str(row["report_status"]),
        report_source=report_source,
        created_by=str(row["created_by"]),
        created_at=row["created_at"],
        html_size=len(report_html),
    )










@router.post("/{claim_id}/reports/grammar-check", response_model=ClaimReportGrammarCheckResponse)
def grammar_check_claim_report_endpoint(
    claim_id: UUID,
    payload: ClaimReportGrammarCheckRequest,
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimReportGrammarCheckResponse:
    with SessionLocal() as db:
        try:
            existing = get_claim(db, claim_id)
        except ClaimNotFoundError as exc:
            raise HTTPException(status_code=404, detail="claim not found") from exc
        if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
            raise HTTPException(status_code=403, detail="doctor can grammar-check only assigned claims")
    # DB connection released — no connection held during the slow grammar check

    report_html = str(payload.report_html or "").strip()
    if not report_html:
        raise HTTPException(status_code=400, detail="report_html is required")
    if len(report_html) > 2_000_000:
        raise HTTPException(status_code=413, detail="report_html too large")

    actor_id = (payload.actor_id or current_user.username or "").strip() or current_user.username

    try:
        result = grammar_check_report_html(report_html)
    except GrammarCheckError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"grammar check failed: {exc}") from exc

    with SessionLocal() as db:
        try:
            db.execute(
                text(
                    """
                    INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
                    VALUES (:claim_id, 'user', :actor_id, 'report_grammar_checked', CAST(:event_payload AS jsonb))
                    """
                ),
                {
                    "claim_id": str(claim_id),
                    "actor_id": actor_id,
                    "event_payload": json.dumps(
                        {
                            "checked_segments": int(result.get("checked_segments") or 0),
                            "corrected_segments": int(result.get("corrected_segments") or 0),
                            "model": str(result.get("model") or ""),
                        }
                    ),
                },
            )
            db.commit()
        except Exception:
            db.rollback()

    return ClaimReportGrammarCheckResponse(
        corrected_html=str(result.get("corrected_html") or report_html),
        changed=bool(result.get("changed")),
        checked_segments=int(result.get("checked_segments") or 0),
        corrected_segments=int(result.get("corrected_segments") or 0),
        model=str(result.get("model") or "") or None,
        notes=str(result.get("notes") or "") or None,
    )

@router.post("/{claim_id}/reports/conclusion-only", response_model=ClaimConclusionGenerateResponse)
def generate_claim_conclusion_only_endpoint(
    claim_id: UUID,
    payload: ClaimConclusionGenerateRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimConclusionGenerateResponse:
    try:
        existing = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can access only assigned claims")

    report_html = str(payload.report_html or "").strip()
    if not report_html:
        raise HTTPException(status_code=400, detail="report_html is required")
    if len(report_html) > 2_000_000:
        raise HTTPException(status_code=413, detail="report_html too large")

    actor_id = (payload.actor_id or current_user.username or "").strip() or current_user.username

    try:
        if bool(payload.rerun_rules):
            run_claim_checklist_pipeline(
                db=db,
                claim_id=claim_id,
                actor_id=actor_id,
                force_source_refresh=bool(payload.force_source_refresh),
            )

        checklist_latest = get_latest_claim_checklist(db=db, claim_id=claim_id)
        if not checklist_latest.found:
            run_claim_checklist_pipeline(
                db=db,
                claim_id=claim_id,
                actor_id=actor_id,
                force_source_refresh=False,
            )
            checklist_latest = get_latest_claim_checklist(db=db, claim_id=claim_id)
    except ChecklistClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"checklist pipeline failed: {exc}") from exc

    checklist_payload = checklist_latest.model_dump() if hasattr(checklist_latest, "model_dump") else {}
    recommendation_raw = str(checklist_payload.get("recommendation") or "").strip()
    recommendation = recommendation_raw.upper() or None
    conclusion_text, triggered_count = _build_rule_based_conclusion_from_report(report_html, checklist_payload)
    extraction_context = ""
    try:
        extraction_context = _build_latest_extraction_context_for_conclusion(db, claim_id)
    except Exception:
        extraction_context = ""
    source_label = "rule_engine"
    if bool(payload.use_ai):
        try:
            ai_conclusion = _generate_ai_medico_legal_conclusion(
                report_html,
                checklist_payload,
                recommendation_raw,
                extraction_context=extraction_context,
            )
            if ai_conclusion:
                conclusion_text = ai_conclusion
                source_label = "ai_medico_legal"
        except Exception:
            source_label = "rule_engine"

    try:
        db.execute(
            text(
                """
                INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload)
                VALUES (:claim_id, 'user', :actor_id, 'report_conclusion_generated', CAST(:event_payload AS jsonb))
                """
            ),
            {
                "claim_id": str(claim_id),
                "actor_id": actor_id,
                "event_payload": json.dumps(
                    {
                        "triggered_rules_count": int(triggered_count),
                        "recommendation": recommendation,
                        "rerun_rules": bool(payload.rerun_rules),
                        "force_source_refresh": bool(payload.force_source_refresh),
                        "use_ai": bool(payload.use_ai),
                        "source": source_label,
                    }
                ),
            },
        )
        db.commit()
    except Exception:
        db.rollback()

    return ClaimConclusionGenerateResponse(
        claim_id=claim_id,
        conclusion=conclusion_text,
        recommendation=recommendation,
        triggered_rules_count=int(triggered_count),
        source=source_label,
    )

@router.post("/{claim_id}/structured-data", response_model=ClaimStructuredDataResponse)
def generate_claim_structured_data_endpoint(
    claim_id: UUID,
    payload: ClaimStructuredDataRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimStructuredDataResponse:
    try:
        existing = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can access only assigned claims")

    actor_id = (payload.actor_id or current_user.username or "").strip() or current_user.username
    try:
        data = generate_claim_structured_data(
            db=db,
            claim_id=claim_id,
            actor_id=actor_id,
            use_llm=bool(payload.use_llm),
            force_refresh=bool(payload.force_refresh),
        )
        return ClaimStructuredDataResponse.model_validate(data)
    except ClaimStructuringError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"structured data generation failed: {exc}") from exc


@router.get("/{claim_id}/structured-data", response_model=ClaimStructuredDataResponse)
def get_claim_structured_data_endpoint(
    claim_id: UUID,
    auto_generate: bool = Query(default=False),
    use_llm: bool = Query(default=True),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> ClaimStructuredDataResponse:
    try:
        existing = get_claim(db, claim_id)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc

    if current_user.role == UserRole.doctor and not doctor_matches_assignment(existing.assigned_doctor_id, current_user.username):
        raise HTTPException(status_code=403, detail="doctor can access only assigned claims")

    try:
        data = get_claim_structured_data(db, claim_id)
        return ClaimStructuredDataResponse.model_validate(data)
    except ClaimStructuredDataNotFoundError:
        if not auto_generate:
            raise HTTPException(status_code=404, detail="structured data not found")
        actor_id = current_user.username
        try:
            data = generate_claim_structured_data(
                db=db,
                claim_id=claim_id,
                actor_id=actor_id,
                use_llm=bool(use_llm),
                force_refresh=True,
            )
            return ClaimStructuredDataResponse.model_validate(data)
        except ClaimStructuringError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"structured data generation failed: {exc}") from exc














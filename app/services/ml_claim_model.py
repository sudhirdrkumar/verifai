from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

MODEL_KEY = "claim_recommendation_nb"
MODEL_VERSION_PREFIX = "nb-v2"
ARTIFACT_DIR = Path("artifacts") / "ml"
MAX_VOCAB = 5000
MIN_TOKEN_LEN = 3
MIN_TRAINING_ROWS = 12
ALLOWED_LABELS = {"approve", "reject", "need_more_evidence", "manual_review"}

ALIGNMENT_LABEL_TYPE = "extraction_html_alignment"
HYBRID_LABEL_TYPE = "hybrid_rule_ml"
AUDITOR_QC_LABEL_TYPE = "auditor_qc_status"
ALIGNMENT_MIN_FIELDS = 2
ALIGNMENT_APPROVE_THRESHOLD = 0.8
ALIGNMENT_NEED_MORE_EVIDENCE_THRESHOLD = 0.35


@dataclass
class MLPrediction:
    available: bool
    label: str | None = None
    confidence: float = 0.0
    probabilities: dict[str, float] | None = None
    top_signals: list[str] | None = None
    model_version: str | None = None
    training_examples: int = 0
    reason: str | None = None


_MODEL_CACHE: dict[str, Any] | None = None


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", str(text or "").lower())).strip()


def _tokenize(text: str) -> list[str]:
    return [tok for tok in _normalize_text(text).split(" ") if len(tok) >= MIN_TOKEN_LEN]


def _flatten_text(value: Any) -> list[str]:
    out: list[str] = []
    if value is None:
        return out
    if isinstance(value, str):
        s = value.strip()
        if s:
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
        for k, v in value.items():
            out.append(str(k))
            out.extend(_flatten_text(v))
        return out
    return out


def _as_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, type(default)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, type(default)) else default
        except json.JSONDecodeError:
            return default
    return default


def _pick_entity_value(entities: dict[str, Any], aliases: list[str]) -> str:
    if not isinstance(entities, dict):
        return ""
    alias_keys = [re.sub(r"[^a-z0-9]+", "", str(a or "").lower()) for a in aliases]
    for key, value in entities.items():
        key_norm = re.sub(r"[^a-z0-9]+", "", str(key or "").lower())
        if not key_norm:
            continue
        for alias in alias_keys:
            if key_norm == alias or alias in key_norm or key_norm in alias:
                out = _normalize_text(_flatten_text(value)[0] if _flatten_text(value) else str(value or ""))
                if out:
                    return out
    return ""


def _extract_ml_focus_features(extracted_entities: dict[str, Any]) -> dict[str, str]:
    entities = extracted_entities if isinstance(extracted_entities, dict) else {}
    diagnosis = _pick_entity_value(entities, ["diagnosis", "final_diagnosis", "provisional_diagnosis"])
    hospital_name = _pick_entity_value(entities, ["hospital_name", "hospital", "treating_hospital"])
    hospital_address = _pick_entity_value(entities, ["hospital_address", "address_of_hospital", "hospital_addr"])
    bill_amount_raw = _pick_entity_value(
        entities,
        ["bill_amount", "claim_amount", "claimed_amount", "amount_claimed", "invoice_amount", "net_payable"],
    )
    bill_amount = ""
    if bill_amount_raw:
        m = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", bill_amount_raw.replace(",", ""))
        if m:
            bill_amount = m.group(1)

    return {
        "diagnosis": diagnosis,
        "hospital_name": hospital_name,
        "hospital_address": hospital_address,
        "bill_amount": bill_amount,
    }


def _extract_rule_learning_lines(row: dict[str, Any], limit: int = 24) -> list[str]:
    lines: list[str] = []

    def _push(value: Any) -> None:
        text_value = _normalize_text(" ".join(_flatten_text(value)))
        if text_value:
            lines.append(text_value[:260])

    decision_recommendation = str(row.get("decision_recommendation") or "").strip().lower()
    decision_route_target = str(row.get("decision_route_target") or "").strip().lower()
    if decision_recommendation:
        _push(f"decision_recommendation {decision_recommendation}")
    if decision_route_target:
        _push(f"decision_route_target {decision_route_target}")

    explanation_summary = str(row.get("explanation_summary") or "").strip()
    if explanation_summary:
        _push("decision_summary " + explanation_summary)

    rule_hits = _as_json(row.get("rule_hits"), [])
    if isinstance(rule_hits, list):
        for item in rule_hits:
            if not isinstance(item, dict):
                continue
            _push(
                "rule_hit "
                + " ".join(
                    [
                        str(item.get("source") or ""),
                        str(item.get("decision") or ""),
                        str(item.get("status") or ""),
                        str(item.get("title") or item.get("rule_id") or ""),
                        str(item.get("summary") or item.get("reason") or ""),
                    ]
                )
            )
            if len(lines) >= limit:
                return lines[:limit]

    decision_payload = _as_json(row.get("decision_payload"), {})
    if isinstance(decision_payload, dict):
        checklist = decision_payload.get("checklist") if isinstance(decision_payload.get("checklist"), list) else []
        for item in checklist:
            if not isinstance(item, dict) or not bool(item.get("triggered")):
                continue
            _push(
                "checklist_trigger "
                + " ".join(
                    [
                        str(item.get("source") or ""),
                        str(item.get("decision") or ""),
                        str(item.get("status") or ""),
                        str(item.get("title") or ""),
                        str(item.get("summary") or ""),
                    ]
                )
            )
            if len(lines) >= limit:
                return lines[:limit]

    return lines[:limit]


def _extract_labeled_value(raw_text: str, aliases: list[str]) -> str:
    text = str(raw_text or "")
    if not text:
        return ""
    for alias in aliases:
        pattern = rf"(?im)(?:^|[\n\r])[ \t>*-]*{re.escape(alias)}\s*[:\-]\s*(.+)$"
        match = re.search(pattern, text)
        if match:
            value = str(match.group(1) or "").strip()
            if value:
                return value
    return ""


def _entities_from_raw_response_json(raw_response_json: Any) -> dict[str, Any]:
    payload: Any = raw_response_json
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return {}
        try:
            payload = json.loads(stripped)
        except Exception:
            payload = stripped

    if isinstance(payload, dict):
        nested_entities = payload.get("extracted_entities")
        if isinstance(nested_entities, dict) and nested_entities:
            return nested_entities

    text_parts: list[str] = []
    if isinstance(payload, dict):
        for key in [
            "batch_summaries",
            "summary",
            "report_summary",
            "diagnosis",
            "hospital_name",
            "patient_name",
            "bill_amount",
            "output_text",
            "content",
            "text",
            "raw_text",
        ]:
            text_parts.extend(_flatten_text(payload.get(key)))
        if not text_parts:
            text_parts.extend(_flatten_text(payload))
    else:
        text_parts.extend(_flatten_text(payload))

    if len(text_parts) > 300:
        text_parts = text_parts[:300]

    raw_text = "\n".join(text_parts)
    if len(raw_text) > 250000:
        raw_text = raw_text[:250000]

    if not raw_text.strip():
        return {}

    entities: dict[str, Any] = {}

    diagnosis = _extract_labeled_value(raw_text, ["Diagnosis", "Final Diagnosis", "Provisional Diagnosis"])
    hospital_name = _extract_labeled_value(raw_text, ["Hospital Name", "Hospital", "Treating Hospital"])
    patient_name = _extract_labeled_value(raw_text, ["Patient Name", "Insured", "Benef Name", "Beneficiary"])
    bill_amount = _extract_labeled_value(raw_text, ["Bill Amount", "Claim Amount", "Claimed Amount", "Amount Claimed"])
    clinical_findings = _extract_labeled_value(raw_text, ["Clinical Findings", "Major Diagnostic Finding", "Findings"])
    investigations = _extract_labeled_value(raw_text, ["Investigations", "Investigation Findings", "Lab Values"])

    if diagnosis:
        entities["diagnosis"] = diagnosis
    if hospital_name:
        entities["hospital_name"] = hospital_name
    if patient_name:
        entities["name"] = patient_name
    if bill_amount:
        entities["bill_amount"] = bill_amount
    if clinical_findings:
        entities["clinical_findings"] = clinical_findings
    if investigations:
        entities["all_investigation_reports_with_values"] = investigations

    return entities


def _coerce_alignment_entities(extracted_entities_value: Any, raw_response_json_value: Any) -> dict[str, Any]:
    entities = _as_json(extracted_entities_value, {})
    if isinstance(entities, dict) and entities:
        return entities
    fallback = _entities_from_raw_response_json(raw_response_json_value)
    return fallback if isinstance(fallback, dict) else {}

def _pick_entity_values(entities: dict[str, Any], aliases: list[str], limit: int = 8) -> list[str]:
    if not isinstance(entities, dict):
        return []

    alias_keys = [re.sub(r"[^a-z0-9]+", "", str(a or "").lower()) for a in aliases]
    values: list[str] = []

    for key, raw_value in entities.items():
        key_norm = re.sub(r"[^a-z0-9]+", "", str(key or "").lower())
        if not key_norm:
            continue
        if not any(key_norm == alias or alias in key_norm or key_norm in alias for alias in alias_keys):
            continue

        for candidate in _flatten_text(raw_value):
            normalized = _normalize_text(candidate)
            if normalized:
                values.append(normalized)

    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
        if len(deduped) >= max(1, limit):
            break
    return deduped


def _strip_html_to_text(report_html: str) -> tuple[str, str]:
    raw = str(report_html or "")
    raw = re.sub(r"(?is)<(script|style).*?>.*?</\\1>", " ", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = unescape(raw)
    raw = re.sub(r"\s+", " ", raw).strip()
    return raw, _normalize_text(raw)


def _text_value_matches_report(value: str, report_text: str, report_tokens: set[str]) -> bool:
    normalized_value = _normalize_text(value)
    if not normalized_value:
        return False
    if normalized_value in report_text:
        return True

    tokens = [tok for tok in normalized_value.split(" ") if len(tok) >= 4]
    if len(tokens) < 2:
        return False

    hit_count = sum(1 for tok in tokens if tok in report_tokens)
    return (hit_count / max(len(tokens), 1)) >= 0.6


def _amount_matches_report(value: str, report_text_raw: str) -> bool:
    normalized_value = str(value or "").replace(",", "")
    match = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", normalized_value)
    if not match:
        return False

    number = match.group(1)
    report_no_commas = str(report_text_raw or "").replace(",", "")
    return bool(re.search(rf"(?<![0-9]){re.escape(number)}(?![0-9])", report_no_commas))


def _evaluate_extraction_report_alignment(extracted_entities: dict[str, Any], report_html: str) -> dict[str, Any]:
    entities = extracted_entities if isinstance(extracted_entities, dict) else {}
    report_raw, report_text = _strip_html_to_text(report_html)
    report_tokens = set(tok for tok in report_text.split(" ") if tok)

    core_fields: list[tuple[str, str, str]] = [
        ("name", _pick_entity_value(entities, ["name", "patient_name", "benef_name", "beneficiary", "insured"]), "text"),
        ("diagnosis", _pick_entity_value(entities, ["diagnosis", "final_diagnosis", "provisional_diagnosis"]), "text"),
        ("clinical_findings", _pick_entity_value(entities, ["clinical_findings", "major_diagnostic_finding", "findings"]), "text"),
        ("hospital_name", _pick_entity_value(entities, ["hospital_name", "hospital", "treating_hospital"]), "text"),
        ("hospital_address", _pick_entity_value(entities, ["hospital_address", "address_of_hospital", "hospital_addr"]), "text"),
        (
            "bill_amount",
            _pick_entity_value(entities, ["bill_amount", "claim_amount", "claimed_amount", "amount_claimed", "invoice_amount", "net_payable"]),
            "amount",
        ),
    ]
    investigation_values = _pick_entity_values(
        entities,
        ["all_investigation_reports_with_values", "investigation_reports", "lab_values", "investigations"],
        limit=12,
    )

    compared_fields: list[str] = []
    matched_fields: list[str] = []

    for field_name, field_value, field_kind in core_fields:
        if not str(field_value or "").strip():
            continue
        compared_fields.append(field_name)
        if field_kind == "amount":
            if _amount_matches_report(str(field_value), report_raw):
                matched_fields.append(field_name)
        elif _text_value_matches_report(str(field_value), report_text, report_tokens):
            matched_fields.append(field_name)

    if investigation_values:
        compared_fields.append("all_investigation_reports_with_values")
        matched_count = 0
        for value in investigation_values:
            if _text_value_matches_report(value, report_text, report_tokens):
                matched_count += 1
        if matched_count > 0 and (matched_count / max(len(investigation_values), 1)) >= 0.4:
            matched_fields.append("all_investigation_reports_with_values")

    compared = len(compared_fields)
    matched = len(matched_fields)
    score = float(matched / compared) if compared else 0.0

    label: str | None = None
    if compared >= ALIGNMENT_MIN_FIELDS:
        if score >= ALIGNMENT_APPROVE_THRESHOLD:
            label = "approve"
        elif score <= ALIGNMENT_NEED_MORE_EVIDENCE_THRESHOLD:
            label = "need_more_evidence"
        else:
            label = "manual_review"

    return {
        "label": label,
        "score": score,
        "compared": compared,
        "matched": matched,
        "compared_fields": compared_fields,
        "matched_fields": matched_fields,
    }


def generate_alignment_feedback_labels(
    db: Session,
    *,
    created_by: str = "system:ml_alignment",
    overwrite: bool = False,
) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            WITH latest_extraction AS (
                SELECT DISTINCT ON (claim_id)
                    claim_id,
                    extracted_entities,
                    created_at
                FROM document_extractions
                ORDER BY claim_id, created_at DESC
            ),
            latest_report_version AS (
                SELECT DISTINCT ON (claim_id)
                    claim_id,
                    report_markdown AS report_html,
                    created_at
                FROM report_versions
                WHERE NULLIF(TRIM(COALESCE(report_markdown, '')), '') IS NOT NULL
                ORDER BY claim_id, version_no DESC, created_at DESC
            ),
            latest_decision_report AS (
                SELECT DISTINCT ON (claim_id)
                    claim_id,
                    NULLIF(TRIM(COALESCE(decision_payload ->> 'report_html', '')), '') AS report_html,
                    NULLIF(TRIM(COALESCE(decision_payload ->> 'raw_response_json', '')), '') AS raw_response_json,
                    generated_at
                FROM decision_results
                WHERE NULLIF(TRIM(COALESCE(decision_payload ->> 'report_html', '')), '') IS NOT NULL
                ORDER BY claim_id, generated_at DESC
            )
            SELECT
                c.id,
                c.external_claim_id,
                le.extracted_entities,
                COALESCE(lrv.report_html, ldr.report_html) AS report_html,
                ldr.raw_response_json
            FROM claims c
            LEFT JOIN latest_extraction le ON le.claim_id = c.id
            LEFT JOIN latest_report_version lrv ON lrv.claim_id = c.id
            LEFT JOIN latest_decision_report ldr ON ldr.claim_id = c.id
            WHERE NULLIF(TRIM(COALESCE(lrv.report_html, ldr.report_html, '')), '') IS NOT NULL
            """
        )
    ).mappings().all()

    existing_rows = db.execute(
        text(
            """
            SELECT
                claim_id::text AS claim_id,
                SUM(CASE WHEN LOWER(TRIM(label_type)) = :alignment_label THEN 1 ELSE 0 END) AS alignment_count,
                SUM(CASE WHEN LOWER(TRIM(label_type)) <> :alignment_label THEN 1 ELSE 0 END) AS non_alignment_count
            FROM feedback_labels
            GROUP BY claim_id
            """
        ),
        {"alignment_label": ALIGNMENT_LABEL_TYPE},
    ).mappings().all()

    existing_map: dict[str, dict[str, int]] = {}
    for row in existing_rows:
        claim_id = str(row.get("claim_id") or "")
        if not claim_id:
            continue
        existing_map[claim_id] = {
            "alignment": int(row.get("alignment_count") or 0),
            "non_alignment": int(row.get("non_alignment_count") or 0),
        }

    inserted = 0
    skipped_existing = 0
    skipped_insufficient = 0

    for row in rows:
        claim_id = str(row.get("id") or "").strip()
        if not claim_id:
            continue

        existing = existing_map.get(claim_id, {"alignment": 0, "non_alignment": 0})
        if int(existing.get("non_alignment") or 0) > 0:
            skipped_existing += 1
            continue

        if int(existing.get("alignment") or 0) > 0:
            if overwrite:
                db.execute(
                    text(
                        """
                        DELETE FROM feedback_labels
                        WHERE claim_id = :claim_id AND LOWER(TRIM(label_type)) = :alignment_label
                        """
                    ),
                    {"claim_id": claim_id, "alignment_label": ALIGNMENT_LABEL_TYPE},
                )
            else:
                skipped_existing += 1
                continue

        extracted_entities = _coerce_alignment_entities(row.get("extracted_entities"), row.get("raw_response_json"))
        report_html = str(row.get("report_html") or "")
        alignment = _evaluate_extraction_report_alignment(extracted_entities, report_html)

        label = str(alignment.get("label") or "").strip().lower() or None
        if label not in ALLOWED_LABELS:
            skipped_insufficient += 1
            continue

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
                    NULL,
                    :label_type,
                    :label_value,
                    :override_reason,
                    :notes,
                    :created_by
                )
                """
            ),
            {
                "claim_id": claim_id,
                "label_type": ALIGNMENT_LABEL_TYPE,
                "label_value": label,
                "override_reason": "auto_label_from_extraction_vs_report_html",
                "notes": json.dumps(
                    {
                        "score": round(float(alignment.get("score") or 0.0), 4),
                        "compared": int(alignment.get("compared") or 0),
                        "matched": int(alignment.get("matched") or 0),
                        "compared_fields": alignment.get("compared_fields") or [],
                        "matched_fields": alignment.get("matched_fields") or [],
                        "external_claim_id": str(row.get("external_claim_id") or ""),
                    },
                    ensure_ascii=False,
                ),
                "created_by": created_by,
            },
        )
        inserted += 1

    return {
        "processed": len(rows),
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_insufficient": skipped_insufficient,
    }

def _build_claim_text(row: dict[str, Any]) -> str:
    extracted_entities = _as_json(row.get("extracted_entities"), {})
    ml_focus = _extract_ml_focus_features(extracted_entities)
    rule_learning_lines = _extract_rule_learning_lines(row)
    payload = {
        "external_claim_id": row.get("external_claim_id"),
        "patient_name": row.get("patient_name"),
        "patient_identifier": row.get("patient_identifier"),
        "status": row.get("status"),
        "priority": row.get("priority"),
        "source_channel": row.get("source_channel"),
        "supervision_source": row.get("supervised_label_type"),
        "tags": _as_json(row.get("tags"), []),
        "extracted_entities": extracted_entities,
        "evidence_refs": _as_json(row.get("evidence_refs"), []),
        "ml_focus_features": ml_focus,
        "rule_learning_features": rule_learning_lines,
    }
    lines = _flatten_text(payload)
    if ml_focus.get("diagnosis"):
        lines.append("focus diagnosis " + ml_focus["diagnosis"])
    if ml_focus.get("hospital_name"):
        lines.append("focus hospital " + ml_focus["hospital_name"])
    if ml_focus.get("hospital_address"):
        lines.append("focus hospital address " + ml_focus["hospital_address"])
    if ml_focus.get("bill_amount"):
        lines.append("focus bill amount " + ml_focus["bill_amount"])

    return "\n".join(lines)


def _extract_label(row: dict[str, Any]) -> str | None:
    raw_label = str(row.get("supervised_label") or "").strip().lower()
    if raw_label in ALLOWED_LABELS:
        return raw_label
    return None



def recommendation_to_feedback_label(raw: str | None) -> str | None:
    recommendation = str(raw or "").strip().lower()
    if recommendation in {"approve", "approved", "admissible", "payable"}:
        return "approve"
    if recommendation in {"reject", "rejected", "inadmissible"}:
        return "reject"
    if recommendation in {"need_more_evidence", "query"}:
        return "need_more_evidence"
    if recommendation in {"manual_review", "in_review"}:
        return "manual_review"
    return None


def upsert_feedback_label(
    db: Session,
    *,
    claim_id: str,
    label_type: str,
    label_value: str,
    created_by: str,
    override_reason: str | None = None,
    notes: str | None = None,
    decision_id: str | None = None,
) -> bool:
    claim_key = str(claim_id or "").strip()
    label_type_key = str(label_type or "").strip().lower()
    label_value_key = str(label_value or "").strip().lower()
    if not claim_key or not label_type_key or label_value_key not in ALLOWED_LABELS:
        return False

    db.execute(
        text(
            """
            DELETE FROM feedback_labels
            WHERE claim_id = :claim_id
              AND LOWER(TRIM(label_type)) = :label_type
            """
        ),
        {"claim_id": claim_key, "label_type": label_type_key},
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
                :label_type,
                :label_value,
                :override_reason,
                :notes,
                :created_by
            )
            """
        ),
        {
            "claim_id": claim_key,
            "decision_id": str(decision_id or "").strip() or None,
            "label_type": label_type_key,
            "label_value": label_value_key,
            "override_reason": str(override_reason or "").strip() or None,
            "notes": str(notes or "").strip() or None,
            "created_by": str(created_by or "").strip() or "system",
        },
    )
    return True

def _collect_training_rows(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text(
            """
            WITH latest_extraction AS (
                SELECT DISTINCT ON (claim_id)
                    claim_id,
                    extracted_entities,
                    evidence_refs,
                    created_at
                FROM document_extractions
                ORDER BY claim_id, created_at DESC
            ),
            latest_feedback AS (
                SELECT
                    x.claim_id,
                    x.label_value,
                    x.label_type,
                    x.created_at
                FROM (
                    SELECT
                        claim_id,
                        LOWER(TRIM(label_value)) AS label_value,
                        LOWER(TRIM(label_type)) AS label_type,
                        created_at,
                        ROW_NUMBER() OVER (
                            PARTITION BY claim_id
                            ORDER BY
                                CASE
                                    WHEN LOWER(TRIM(label_type)) = 'auditor_qc_status' THEN 0
                                    WHEN LOWER(TRIM(label_type)) = 'hybrid_rule_ml' THEN 1
                                    WHEN LOWER(TRIM(label_type)) = 'extraction_html_alignment' THEN 2
                                    ELSE 3
                                END,
                                created_at DESC
                        ) AS rn
                    FROM feedback_labels
                ) x
                WHERE x.rn = 1
            ),
            latest_decision AS (
                SELECT DISTINCT ON (claim_id)
                    claim_id,
                    rule_hits,
                    explanation_summary,
                    recommendation AS decision_recommendation,
                    route_target AS decision_route_target,
                    decision_payload,
                    generated_at
                FROM decision_results
                ORDER BY claim_id, generated_at DESC
            )
            SELECT
                c.id,
                c.external_claim_id,
                c.patient_name,
                c.patient_identifier,
                c.status,
                c.priority,
                c.source_channel,
                c.tags,
                le.extracted_entities,
                le.evidence_refs,
                ld.rule_hits,
                ld.explanation_summary,
                ld.decision_recommendation,
                ld.decision_route_target,
                ld.decision_payload,
                lf.label_type AS supervised_label_type,
                CASE
                    WHEN lf.label_value IN ('approve','approved') THEN 'approve'
                    WHEN lf.label_value IN ('reject','rejected') THEN 'reject'
                    WHEN lf.label_value IN ('need_more_evidence','query') THEN 'need_more_evidence'
                    WHEN lf.label_value IN ('manual_review','review') THEN 'manual_review'
                    WHEN c.status = 'completed' THEN 'approve'
                    WHEN c.status = 'withdrawn' THEN 'reject'
                    ELSE NULL
                END AS supervised_label
            FROM claims c
            LEFT JOIN latest_extraction le ON le.claim_id = c.id
            LEFT JOIN latest_feedback lf ON lf.claim_id = c.id
            LEFT JOIN latest_decision ld ON ld.claim_id = c.id
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def _train_naive_bayes(examples: list[tuple[str, str]]) -> dict[str, Any] | None:
    if len(examples) < MIN_TRAINING_ROWS:
        return None

    class_doc_counts: Counter[str] = Counter()
    token_counts_by_class: dict[str, Counter[str]] = {}
    total_token_counts: Counter[str] = Counter()

    for text_value, label in examples:
        class_doc_counts[label] += 1
        toks = _tokenize(text_value)
        if label not in token_counts_by_class:
            token_counts_by_class[label] = Counter()
        token_counts_by_class[label].update(toks)
        total_token_counts.update(toks)

    if len(class_doc_counts) < 2:
        return None

    vocab = [tok for tok, _ in total_token_counts.most_common(MAX_VOCAB)]
    vocab_set = set(vocab)

    compact_counts: dict[str, dict[str, int]] = {}
    total_tokens_by_class: dict[str, int] = {}
    for label, counter in token_counts_by_class.items():
        filtered = {tok: int(cnt) for tok, cnt in counter.items() if tok in vocab_set}
        compact_counts[label] = filtered
        total_tokens_by_class[label] = int(sum(filtered.values()))

    model = {
        "model_key": MODEL_KEY,
        "algorithm": "multinomial_naive_bayes",
        "trained_at": datetime.now(timezone.utc).isoformat(),
        "class_doc_counts": dict(class_doc_counts),
        "token_counts_by_class": compact_counts,
        "total_tokens_by_class": total_tokens_by_class,
        "vocab": vocab,
        "num_examples": len(examples),
        "label_counts": dict(class_doc_counts),
    }
    return model


def _predict(model: dict[str, Any], text_value: str) -> MLPrediction:
    vocab = model.get("vocab") or []
    if not vocab:
        return MLPrediction(available=False, reason="empty vocabulary")
    vocab_set = set(vocab)

    class_doc_counts: dict[str, int] = {k: int(v) for k, v in (model.get("class_doc_counts") or {}).items()}
    token_counts_by_class: dict[str, dict[str, int]] = {
        k: {tk: int(tv) for tk, tv in (v or {}).items()}
        for k, v in (model.get("token_counts_by_class") or {}).items()
    }
    total_tokens_by_class: dict[str, int] = {
        k: int(v) for k, v in (model.get("total_tokens_by_class") or {}).items()
    }

    if len(class_doc_counts) < 2:
        return MLPrediction(available=False, reason="not enough classes")

    token_freq = Counter([tok for tok in _tokenize(text_value) if tok in vocab_set])
    classes = list(class_doc_counts.keys())
    doc_total = sum(class_doc_counts.values())
    class_count = len(classes)
    vocab_size = len(vocab)

    log_scores: dict[str, float] = {}
    for label in classes:
        prior = (class_doc_counts[label] + 1.0) / (doc_total + class_count)
        score = math.log(prior)
        class_counts = token_counts_by_class.get(label, {})
        denom = float(total_tokens_by_class.get(label, 0) + vocab_size)
        for tok, cnt in token_freq.items():
            tok_count = float(class_counts.get(tok, 0) + 1)
            score += float(cnt) * math.log(tok_count / denom)
        log_scores[label] = score

    max_log = max(log_scores.values())
    exp_scores = {k: math.exp(v - max_log) for k, v in log_scores.items()}
    prob_sum = sum(exp_scores.values()) or 1.0
    probs = {k: (v / prob_sum) for k, v in exp_scores.items()}

    sorted_probs = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    best_label, best_conf = sorted_probs[0]
    second_label = sorted_probs[1][0] if len(sorted_probs) > 1 else best_label

    second_counts = token_counts_by_class.get(second_label, {})
    best_counts = token_counts_by_class.get(best_label, {})
    best_denom = float(total_tokens_by_class.get(best_label, 0) + vocab_size)
    second_denom = float(total_tokens_by_class.get(second_label, 0) + vocab_size)
    token_signals: list[tuple[float, str]] = []
    for tok, cnt in token_freq.items():
        p_best = (best_counts.get(tok, 0) + 1.0) / best_denom
        p_second = (second_counts.get(tok, 0) + 1.0) / second_denom
        delta = float(cnt) * (math.log(p_best) - math.log(p_second))
        token_signals.append((delta, tok))
    token_signals.sort(reverse=True)
    top_signals = [tok for delta, tok in token_signals[:8] if delta > 0]

    return MLPrediction(
        available=True,
        label=best_label,
        confidence=float(best_conf),
        probabilities=probs,
        top_signals=top_signals,
        model_version=str(model.get("version") or ""),
        training_examples=int(model.get("num_examples") or 0),
    )


def _write_artifact(model: dict[str, Any], version: str) -> str:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    artifact_path = ARTIFACT_DIR / f"{MODEL_KEY}_{version}.json"
    artifact_path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8")
    return str(artifact_path)


def _read_artifact(path_value: str) -> dict[str, Any] | None:
    try:
        path = Path(path_value)
        if not path.exists() or not path.is_file():
            return None
        body = json.loads(path.read_text(encoding="utf-8"))
        return body if isinstance(body, dict) else None
    except Exception:
        return None


def _persist_registry(db: Session, version: str, artifact_uri: str, model: dict[str, Any]) -> None:
    metrics = {
        "algorithm": model.get("algorithm"),
        "num_examples": model.get("num_examples"),
        "label_counts": model.get("label_counts"),
        "vocab_size": len(model.get("vocab") or []),
        "trained_at": model.get("trained_at"),
    }

    db.execute(
        text(
            """
            UPDATE model_registry
            SET status = 'archived', effective_to = NOW()
            WHERE model_key = :model_key AND status = 'active'
            """
        ),
        {"model_key": MODEL_KEY},
    )

    db.execute(
        text(
            """
            INSERT INTO model_registry (
                model_key,
                version,
                status,
                metrics,
                artifact_uri,
                effective_from
            )
            VALUES (
                :model_key,
                :version,
                'active',
                CAST(:metrics AS jsonb),
                :artifact_uri,
                NOW()
            )
            ON CONFLICT (model_key, version)
            DO UPDATE SET
                status = EXCLUDED.status,
                metrics = EXCLUDED.metrics,
                artifact_uri = EXCLUDED.artifact_uri,
                effective_from = EXCLUDED.effective_from,
                effective_to = NULL
            """
        ),
        {
            "model_key": MODEL_KEY,
            "version": version,
            "metrics": json.dumps(metrics),
            "artifact_uri": artifact_uri,
        },
    )


def _load_latest_from_registry(db: Session) -> dict[str, Any] | None:
    row = db.execute(
        text(
            """
            SELECT version, artifact_uri
            FROM model_registry
            WHERE model_key = :model_key
            ORDER BY
                CASE WHEN status = 'active' THEN 0 ELSE 1 END,
                COALESCE(effective_from, created_at) DESC,
                created_at DESC
            LIMIT 1
            """
        ),
        {"model_key": MODEL_KEY},
    ).mappings().first()

    if row is None:
        return None

    artifact_uri = str(row.get("artifact_uri") or "").strip()
    model = _read_artifact(artifact_uri) if artifact_uri else None
    if not isinstance(model, dict):
        return None

    model["version"] = str(row.get("version") or model.get("version") or "")
    return model


def _train_and_persist(db: Session) -> dict[str, Any] | None:
    try:
        generate_alignment_feedback_labels(db=db, created_by="system:ml_alignment", overwrite=False)
    except Exception:
        # Alignment-label generation should not block model training.
        pass

    rows = _collect_training_rows(db)
    examples: list[tuple[str, str]] = []
    for row in rows:
        label = _extract_label(row)
        if label is None:
            continue
        text_value = _build_claim_text(row)
        if not text_value.strip():
            continue
        examples.append((text_value, label))

    model = _train_naive_bayes(examples)
    if not isinstance(model, dict):
        return None

    version = f"{MODEL_VERSION_PREFIX}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    model["version"] = version
    artifact_uri = _write_artifact(model, version)
    _persist_registry(db, version, artifact_uri, model)
    db.commit()
    return model


def ensure_model(db: Session, force_retrain: bool = False) -> dict[str, Any] | None:
    global _MODEL_CACHE

    if force_retrain:
        model = _train_and_persist(db)
        _MODEL_CACHE = model
        return model

    if isinstance(_MODEL_CACHE, dict):
        return _MODEL_CACHE

    model = _load_latest_from_registry(db)
    if not isinstance(model, dict):
        model = _train_and_persist(db)
    else:
        version = str(model.get("version") or "")
        if not version.startswith(MODEL_VERSION_PREFIX):
            retrained = _train_and_persist(db)
            if isinstance(retrained, dict):
                model = retrained

    _MODEL_CACHE = model
    return model


def predict_claim_recommendation(
    db: Session,
    claim_text: str,
    force_retrain: bool = False,
) -> MLPrediction:
    model = ensure_model(db, force_retrain=force_retrain)
    if not isinstance(model, dict):
        return MLPrediction(available=False, reason="model unavailable")

    pred = _predict(model, claim_text)
    if pred.available:
        pred.model_version = str(model.get("version") or pred.model_version or "")
        pred.training_examples = int(model.get("num_examples") or pred.training_examples or 0)
    return pred












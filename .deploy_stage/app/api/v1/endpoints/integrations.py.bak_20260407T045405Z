import hmac
import json
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.session import get_db
from app.schemas.integration import TeamRightWorksCaseIntakeRequest, TeamRightWorksCaseIntakeResponse

router = APIRouter(prefix="/integrations", tags=["integrations"])

_ALLOWED_CLAIM_STATUS = {
    "ready_for_assignment",
    "waiting_for_documents",
    "in_review",
    "needs_qc",
    "completed",
    "withdrawn",
}
_ALLOWED_REPORT_STATUS = {"draft", "completed", "uploaded", "final"}
_ALLOWED_LABELS = {"approve", "reject", "need_more_evidence", "manual_review"}


def _ensure_claim_legacy_data_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS claim_legacy_data (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                legacy_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_claim_legacy_data_claim_id ON claim_legacy_data(claim_id)"))


def _ensure_claim_report_uploads_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS claim_report_uploads (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                report_export_status VARCHAR(30) NOT NULL DEFAULT 'pending',
                tagging VARCHAR(120),
                subtagging VARCHAR(120),
                opinion TEXT,
                qc_status VARCHAR(10) NOT NULL DEFAULT 'no',
                updated_by VARCHAR(100),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_claim_report_uploads_claim_id ON claim_report_uploads(claim_id)"))



def _ensure_claim_completed_at_column(db: Session) -> None:
    db.execute(text("ALTER TABLE claims ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_claims_completed_at ON claims(completed_at)"))
def _extract_auth_token(authorization: str | None, x_integration_token: str | None) -> str:
    header_token = (x_integration_token or "").strip()
    if header_token:
        return header_token

    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return auth


def _normalize_claim_status(raw: str | None) -> str:
    val = str(raw or "").strip().lower()
    if val in _ALLOWED_CLAIM_STATUS:
        return val
    return "completed"


def _normalize_report_status(raw: str | None) -> str:
    val = str(raw or "").strip().lower()
    if val in _ALLOWED_REPORT_STATUS:
        return val
    return "completed"


def _normalize_recommendation(raw: str | None) -> str | None:
    val = str(raw or "").strip().lower()
    if not val:
        return None

    if val in {"approve", "approved", "admissible", "payable", "justified"}:
        return "approve"
    if val in {"reject", "rejected", "inadmissible", "not justified", "inadmissable", "inadmissible"}:
        return "reject"
    if val in {"query", "need_more_evidence", "need more evidence", "need-more-evidence"}:
        return "need_more_evidence"
    if val in {"manual_review", "manual review", "review"}:
        return "manual_review"

    if any(token in val for token in ["inadmiss", "reject", "rejection", "not justified"]):
        return "reject"
    if any(token in val for token in ["admiss", "approve", "payable", "justified"]):
        return "approve"
    if "query" in val or "need more" in val:
        return "need_more_evidence"
    if "manual" in val:
        return "manual_review"
    return None


def _route_target_for_recommendation(recommendation: str) -> tuple[str, bool, int]:
    if recommendation == "approve":
        return "auto_approve_queue", False, 4
    if recommendation == "reject":
        return "reject_queue", True, 1
    if recommendation == "need_more_evidence":
        return "query_queue", True, 2
    return "manual_review_queue", True, 3


def _normalize_feedback_label(raw: str | None) -> str | None:
    val = str(raw or "").strip().lower()
    if not val:
        return None
    if val in _ALLOWED_LABELS:
        return val
    if val in {"approved", "admissible", "payable", "justified"}:
        return "approve"
    if val in {"rejected", "inadmissible", "not justified"}:
        return "reject"
    if val in {"query", "need more evidence", "need-more-evidence"}:
        return "need_more_evidence"
    if val in {"manual review", "review"}:
        return "manual_review"
    return None


def _legacy_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        text_value = str(value).strip()
        if text_value:
            return text_value
    return ""


_EMPTY_LIKE_TEXT_VALUES = {
    "",
    "-",
    ".",
    "na",
    "n/a",
    "none",
    "nil",
    "null",
    "not available",
    "0",
}


def _clean_text(value: Any) -> str:
    text_value = str(value or "").strip()
    if not text_value:
        return ""
    if text_value.lower() in _EMPTY_LIKE_TEXT_VALUES:
        return ""
    return text_value


def _normalize_tagging_value(value: Any) -> str:
    raw = _clean_text(value).lower()
    if raw == "genuine":
        return "Genuine"
    if raw in {"fraudulent", "fraudlent", "fraud"}:
        return "Fraudulent"
    return ""


def _normalize_export_status_value(value: Any) -> str:
    raw = _clean_text(value).lower()
    return raw if raw in {"uploaded", "pending"} else ""


def _normalize_qc_status_value(value: Any) -> str:
    raw = _clean_text(value).lower()
    return raw if raw in {"yes", "no"} else ""


def _default_subtagging_for_tagging(tagging: str) -> str:
    if tagging == "Genuine":
        return "Hospitalization verified and found to be genuine"
    if tagging == "Fraudulent":
        return "Circumstantial evidence suggesting of possible fraud"
    return ""


def _strip_html_to_text(value: Any) -> str:
    raw = str(value or "")
    if not raw:
        return ""
    text_value = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text_value = re.sub(r"<[^>]+>", " ", text_value)
    text_value = re.sub(r"\s+", " ", text_value).strip()
    return _clean_text(text_value)



def _clear_claim_generated_data(db: Session, claim_id: str) -> dict[str, int]:
    report_versions_deleted = int(
        db.execute(text("DELETE FROM report_versions WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
    )
    claim_report_uploads_deleted = int(
        db.execute(text("DELETE FROM claim_report_uploads WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
    )
    feedback_labels_deleted = int(
        db.execute(text("DELETE FROM feedback_labels WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
    )
    decision_results_deleted = int(
        db.execute(text("DELETE FROM decision_results WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
    )
    document_extractions_deleted = int(
        db.execute(text("DELETE FROM document_extractions WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
    )
    documents_reset = int(
        db.execute(
            text(
                """
                UPDATE claim_documents
                SET parse_status = 'pending',
                    parsed_at = NULL
                WHERE claim_id = :claim_id
                """
            ),
            {"claim_id": claim_id},
        ).rowcount
        or 0
    )
    return {
        "report_versions_deleted": report_versions_deleted,
        "claim_report_uploads_deleted": claim_report_uploads_deleted,
        "feedback_labels_deleted": feedback_labels_deleted,
        "decision_results_deleted": decision_results_deleted,
        "document_extractions_deleted": document_extractions_deleted,
        "documents_reset": documents_reset,
    }

@router.post("/teamrightworks/case-intake", response_model=TeamRightWorksCaseIntakeResponse)
def teamrightworks_case_intake(
    payload: TeamRightWorksCaseIntakeRequest,
    db: Session = Depends(get_db),
    authorization: str | None = Header(default=None),
    x_integration_token: str | None = Header(default=None, alias="X-Integration-Token"),
) -> TeamRightWorksCaseIntakeResponse:
    expected_token = str(settings.teamrightworks_integration_token or "").strip()
    if not expected_token:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="integration token not configured")

    provided_token = _extract_auth_token(authorization, x_integration_token)
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid integration token")

    _ensure_claim_legacy_data_table(db)
    _ensure_claim_report_uploads_table(db)
    _ensure_claim_completed_at_column(db)

    actor_id = str(settings.teamrightworks_integration_actor or "integration:teamrightworks").strip() or "integration:teamrightworks"

    created_claim = False
    raw_cleanup_summary: dict[str, int] | None = None
    report_version_no: int | None = None
    decision_id: str | None = None
    feedback_label_saved = False

    external_claim_id = payload.external_claim_id.strip()
    claim_status = _normalize_claim_status(payload.status)
    tags = [str(tag).strip() for tag in (payload.tags or []) if str(tag).strip()]
    source_channel = str(payload.source_channel or "teamrightworks.in").strip() or "teamrightworks.in"
    raw_files_only = bool(payload.raw_files_only)

    try:
        claim = db.execute(
            text(
                """
                SELECT id, external_claim_id
                FROM claims
                WHERE external_claim_id = :external_claim_id
                LIMIT 1
                """
            ),
            {"external_claim_id": external_claim_id},
        ).mappings().first()

        if claim is None:
            claim = db.execute(
                text(
                    """
                    INSERT INTO claims (
                        external_claim_id,
                        patient_name,
                        patient_identifier,
                        status,
                        assigned_doctor_id,
                        priority,
                        source_channel,
                        tags,
                        completed_at
                    )
                    VALUES (
                        :external_claim_id,
                        :patient_name,
                        :patient_identifier,
                        CAST(:status AS claim_status),
                        :assigned_doctor_id,
                        :priority,
                        :source_channel,
                        CAST(:tags AS jsonb),
                        CASE WHEN CAST(:status AS claim_status) = 'completed'::claim_status THEN NOW() ELSE NULL END
                    )
                    RETURNING id, external_claim_id
                    """
                ),
                {
                    "external_claim_id": external_claim_id,
                    "patient_name": (payload.patient_name or "").strip() or None,
                    "patient_identifier": (payload.patient_identifier or "").strip() or None,
                    "status": claim_status,
                    "assigned_doctor_id": (payload.assigned_doctor_id or "").strip() or None,
                    "priority": int(payload.priority),
                    "source_channel": source_channel,
                    "tags": json.dumps(tags),
                },
            ).mappings().one()
            created_claim = True
        else:
            db.execute(
                text(
                    """
                    UPDATE claims
                    SET
                        patient_name = COALESCE(NULLIF(:patient_name, ''), patient_name),
                        patient_identifier = COALESCE(NULLIF(:patient_identifier, ''), patient_identifier),
                        assigned_doctor_id = COALESCE(NULLIF(:assigned_doctor_id, ''), assigned_doctor_id),
                        status = COALESCE(CAST(:status AS claim_status), status),
                        completed_at = CASE WHEN CAST(:status AS claim_status) = 'completed'::claim_status THEN COALESCE(completed_at, NOW()) ELSE completed_at END,
                        priority = COALESCE(:priority, priority),
                        source_channel = COALESCE(NULLIF(:source_channel, ''), source_channel),
                        tags = COALESCE(CAST(:tags_json AS jsonb), tags),
                        updated_at = NOW()
                    WHERE id = :claim_id
                    """
                ),
                {
                    "claim_id": str(claim["id"]),
                    "patient_name": (payload.patient_name or "").strip(),
                    "patient_identifier": (payload.patient_identifier or "").strip(),
                    "assigned_doctor_id": (payload.assigned_doctor_id or "").strip(),
                    "status": claim_status,
                    "priority": int(payload.priority),
                    "source_channel": source_channel,
                    "tags_json": json.dumps(tags) if payload.tags is not None else None,
                },
            )

        claim_id = str(claim["id"])

        legacy_payload = payload.legacy_payload if isinstance(payload.legacy_payload, dict) else {}
        if legacy_payload:
            db.execute(
                text(
                    """
                    INSERT INTO claim_legacy_data (claim_id, legacy_payload, updated_at)
                    VALUES (:claim_id, CAST(:legacy_payload AS jsonb), NOW())
                    ON CONFLICT (claim_id)
                    DO UPDATE SET
                        legacy_payload = EXCLUDED.legacy_payload,
                        updated_at = NOW()
                    """
                ),
                {
                    "claim_id": claim_id,
                    "legacy_payload": json.dumps(legacy_payload),
                },
            )

        if raw_files_only:
            raw_cleanup_summary = _clear_claim_generated_data(db, claim_id)

        normalized_recommendation = _normalize_recommendation(payload.recommendation)
        if normalized_recommendation and not raw_files_only:
            route_target, manual_review_required, review_priority = _route_target_for_recommendation(normalized_recommendation)

            db.execute(
                text(
                    """
                    UPDATE decision_results
                    SET is_active = FALSE
                    WHERE claim_id = :claim_id AND is_active = TRUE
                    """
                ),
                {"claim_id": claim_id},
            )

            payload_obj: dict[str, Any] = dict(payload.decision_payload or {})
            payload_obj.setdefault("source", "teamrightworks_integration")
            payload_obj.setdefault("external_claim_id", external_claim_id)
            if payload.sync_ref:
                payload_obj["sync_ref"] = payload.sync_ref
            if payload.report_html:
                payload_obj["report_html"] = payload.report_html

            inserted_decision = db.execute(
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
                        generated_at,
                        is_active
                    )
                    VALUES (
                        :claim_id,
                        NULL,
                        :rule_version,
                        :model_version,
                        NULL,
                        NULL,
                        CAST(:consistency_checks AS jsonb),
                        CAST(:rule_hits AS jsonb),
                        :explanation_summary,
                        CAST(:recommendation AS decision_recommendation),
                        :route_target,
                        :manual_review_required,
                        :review_priority,
                        CAST(:decision_payload AS jsonb),
                        :generated_by,
                        COALESCE(:generated_at, NOW()),
                        TRUE
                    )
                    RETURNING id
                    """
                ),
                {
                    "claim_id": claim_id,
                    "rule_version": "integration_teamrightworks_v1",
                    "model_version": "integration_external",
                    "consistency_checks": "[]",
                    "rule_hits": "[]",
                    "explanation_summary": (payload.explanation_summary or "").strip() or None,
                    "recommendation": normalized_recommendation,
                    "route_target": route_target,
                    "manual_review_required": bool(manual_review_required),
                    "review_priority": int(review_priority),
                    "decision_payload": json.dumps(payload_obj),
                    "generated_by": actor_id,
                    "generated_at": payload.event_occurred_at,
                },
            ).mappings().one()
            decision_id = str(inserted_decision["id"])

        report_html = (payload.report_html or "").strip()
        if report_html and not raw_files_only:
            selected_decision_id = decision_id
            if not selected_decision_id:
                latest_decision = db.execute(
                    text(
                        """
                        SELECT id
                        FROM decision_results
                        WHERE claim_id = :claim_id
                        ORDER BY generated_at DESC
                        LIMIT 1
                        """
                    ),
                    {"claim_id": claim_id},
                ).mappings().first()
                if latest_decision is not None:
                    selected_decision_id = str(latest_decision["id"])

            report_version_no = int(
                db.execute(
                    text("SELECT COALESCE(MAX(version_no), 0) + 1 FROM report_versions WHERE claim_id = :claim_id"),
                    {"claim_id": claim_id},
                ).scalar_one()
                or 1
            )

            db.execute(
                text(
                    """
                    INSERT INTO report_versions (
                        claim_id,
                        decision_id,
                        version_no,
                        report_status,
                        report_markdown,
                        export_uri,
                        created_by,
                        created_at
                    )
                    VALUES (
                        :claim_id,
                        :decision_id,
                        :version_no,
                        :report_status,
                        :report_markdown,
                        '',
                        :created_by,
                        COALESCE(:created_at, NOW())
                    )
                    """
                ),
                {
                    "claim_id": claim_id,
                    "decision_id": selected_decision_id,
                    "version_no": report_version_no,
                    "report_status": _normalize_report_status(payload.report_status),
                    "report_markdown": report_html,
                    "created_by": (payload.doctor_username or actor_id).strip() or actor_id,
                    "created_at": payload.event_occurred_at,
                },
            )

        intake_tagging = _normalize_tagging_value(getattr(payload, "tagging", None))
        intake_subtagging = _clean_text(getattr(payload, "subtagging", None))
        intake_opinion = _clean_text(getattr(payload, "opinion", None))
        intake_report_export_status = _normalize_export_status_value(getattr(payload, "report_export_status", None))
        intake_qc_status = _normalize_qc_status_value(getattr(payload, "qc_status", None))

        legacy_tagging = _normalize_tagging_value(
            _legacy_text(
                legacy_payload,
                "tagging",
                "tagging_status",
                "tag",
                "qc_tagging",
                "audit_tagging",
                "final_tagging",
            )
        )
        legacy_subtagging = _clean_text(
            _legacy_text(
                legacy_payload,
                "subtagging",
                "sub_tagging",
                "subtag",
                "qc_subtagging",
                "audit_subtagging",
                "final_subtagging",
            )
        )
        legacy_opinion = _clean_text(
            _legacy_text(
                legacy_payload,
                "opinion",
                "doctor_opinion",
                "auditor_opinion",
                "remarks",
            )
        )

        legacy_report_export_status = _normalize_export_status_value(
            _legacy_text(legacy_payload, "report_export_status", "document_status", "upload_status")
        )
        legacy_qc_status = _normalize_qc_status_value(_legacy_text(legacy_payload, "qc_status"))

        resolved_tagging = intake_tagging or legacy_tagging
        resolved_subtagging = intake_subtagging or legacy_subtagging
        resolved_opinion = intake_opinion or legacy_opinion

        resolved_report_export_status = intake_report_export_status or legacy_report_export_status
        if (
            not resolved_report_export_status
            and resolved_tagging
            and resolved_subtagging
            and resolved_opinion
        ):
            resolved_report_export_status = "uploaded"

        resolved_qc_status = intake_qc_status or legacy_qc_status

        resolved_updated_by = (
            (payload.doctor_username or "").strip()
            or _legacy_text(legacy_payload, "uploaded_by_username")
            or actor_id
        )

        if (
            not raw_files_only
            and (
                intake_tagging
                or intake_subtagging
                or intake_opinion
                or legacy_tagging
                or legacy_subtagging
                or legacy_opinion
                or resolved_report_export_status
                or resolved_qc_status
            )
        ):
            db.execute(
                text(
                    """
                    INSERT INTO claim_report_uploads (
                        claim_id,
                        report_export_status,
                        tagging,
                        subtagging,
                        opinion,
                        qc_status,
                        updated_by,
                        updated_at
                    )
                    VALUES (
                        :claim_id,
                        COALESCE(NULLIF(:report_export_status, ''), 'pending'),
                        NULLIF(:tagging, ''),
                        NULLIF(:subtagging, ''),
                        NULLIF(:opinion, ''),
                        COALESCE(NULLIF(:qc_status, ''), 'no'),
                        :updated_by,
                        NOW()
                    )
                    ON CONFLICT (claim_id)
                    DO UPDATE SET
                        report_export_status = COALESCE(NULLIF(:report_export_status, ''), claim_report_uploads.report_export_status),
                        tagging = COALESCE(NULLIF(:tagging, ''), claim_report_uploads.tagging),
                        subtagging = COALESCE(NULLIF(:subtagging, ''), claim_report_uploads.subtagging),
                        opinion = COALESCE(NULLIF(:opinion, ''), claim_report_uploads.opinion),
                        qc_status = COALESCE(NULLIF(:qc_status, ''), claim_report_uploads.qc_status),
                        updated_by = COALESCE(NULLIF(:updated_by, ''), claim_report_uploads.updated_by),
                        updated_at = NOW()
                    """
                ),
                {
                    "claim_id": claim_id,
                    "report_export_status": resolved_report_export_status,
                    "tagging": resolved_tagging,
                    "subtagging": resolved_subtagging,
                    "opinion": resolved_opinion,
                    "qc_status": resolved_qc_status,
                    "updated_by": resolved_updated_by,
                },
            )

        normalized_label = _normalize_feedback_label(payload.auditor_label)
        if normalized_label and not raw_files_only:
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
                    "claim_id": claim_id,
                    "decision_id": decision_id,
                    "label_type": "teamrightworks_auditor",
                    "label_value": normalized_label,
                    "override_reason": "integration_intake",
                    "notes": (payload.auditor_notes or "").strip() or None,
                    "created_by": actor_id,
                },
            )
            feedback_label_saved = True

        db.execute(
            text(
                """
                INSERT INTO workflow_events (claim_id, actor_type, actor_id, event_type, event_payload, occurred_at)
                VALUES (
                    :claim_id,
                    'system',
                    :actor_id,
                    'teamrightworks_case_intake',
                    CAST(:event_payload AS jsonb),
                    COALESCE(:occurred_at, NOW())
                )
                """
            ),
            {
                "claim_id": claim_id,
                "actor_id": actor_id,
                "event_payload": json.dumps(
                    {
                        "sync_ref": payload.sync_ref,
                        "created_claim": created_claim,
                        "report_version_no": report_version_no,
                        "recommendation": normalized_recommendation,
                        "feedback_label_saved": feedback_label_saved,
                        "raw_files_only": raw_files_only,
                        "raw_cleanup_summary": raw_cleanup_summary or {},
                    }
                ),
                "occurred_at": payload.event_occurred_at,
            },
        )

        db.commit()

        return TeamRightWorksCaseIntakeResponse(
            ok=True,
            claim_id=claim_id,
            external_claim_id=external_claim_id,
            created_claim=created_claim,
            report_version_no=report_version_no,
            decision_id=decision_id,
            feedback_label_saved=feedback_label_saved,
            message="TeamRightWorks case synced successfully.",
        )
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"teamrightworks intake failed: {exc}") from exc


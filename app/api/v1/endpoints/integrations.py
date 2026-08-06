import hmac
import json
import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps.auth import require_roles
from app.core.config import settings
from app.db.session import get_db
from app.schemas.auth import UserRole
from app.schemas.integration import (
    TeamRightWorksCaseIntakeRequest,
    TeamRightWorksCaseIntakeResponse,
    VerifAiClaimQueueResponse,
    VerifAiClaimReportResponse,
)
from app.services.auth_service import AuthenticatedUser
from app.services.documents_service import ensure_legacy_documents_materialized
from app.services.storage_service import StorageOperationError, generate_download_url

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
    # DDL is handled by startup migrations (app/db/migrations.py).
    # Keep as no-op for backward compatibility with existing call sites.
    return None


def _ensure_claim_report_uploads_table(db: Session) -> None:
    # DDL is handled by startup migrations (app/db/migrations.py).
    # Keep as no-op for backward compatibility with existing call sites.
    return None



def _ensure_claim_completed_at_column(db: Session) -> None:
    # DDL is handled by startup migrations (app/db/migrations.py).
    # Keep as no-op for backward compatibility with existing call sites.
    return None
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


def _normalize_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _legacy_first_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        cleaned = _clean_text(value)
        if cleaned:
            return cleaned
    return ""


def _parse_float_value(value: Any) -> float | None:
    raw = _clean_text(value)
    if not raw:
        return None
    normalized = re.sub(r"[^0-9.\-]", "", raw.replace(",", ""))
    if not normalized:
        return None
    try:
        return float(normalized)
    except ValueError:
        return None


def _parse_date_value(value: Any) -> str | None:
    from datetime import datetime

    raw = _clean_text(value)
    if not raw:
        return None
    candidate = raw.split("T", 1)[0].split(" ", 1)[0].strip()
    for pattern in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y"):
        try:
            return datetime.strptime(candidate, pattern).date().isoformat()
        except ValueError:
            continue
    return candidate if re.match(r"^\d{4}-\d{2}-\d{2}$", candidate) else None


def _normalize_claim_type_value(value: Any) -> str | None:
    raw = _clean_text(value).lower()
    if not raw:
        return None
    if "cashless" in raw:
        return "cashless"
    if "reimburse" in raw:
        return "reimbursement"
    if "pre" in raw and "auth" in raw:
        return "pre_authorization"
    if "post" in raw and "hosp" in raw:
        return "post_hospitalization"
    if "invest" in raw:
        return "investigation"
    if "fraud" in raw:
        return "fraud_detection"
    return None


def _normalize_company_code(value: Any) -> str:
    fallback = _clean_text(settings.verifai_insurance_company_code) or "QC_BKP"
    raw = _clean_text(value)
    if not raw:
        return fallback[:32]
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", raw.upper()).strip("_")
    return (normalized or fallback)[:32]


def _normalize_document_type(file_name: str, metadata: dict[str, Any]) -> str:
    candidates = [
        metadata.get("declared_type"),
        metadata.get("document_type"),
        metadata.get("doc_type"),
        file_name,
    ]
    combined = " ".join(_clean_text(item).lower() for item in candidates if _clean_text(item))
    if not combined:
        return "unknown"
    mapping = (
        ("discharge", "discharge_summary"),
        ("final bill", "final_bill"),
        ("bill", "final_bill"),
        ("pharmacy", "pharmacy_bill"),
        ("medicine", "pharmacy_bill"),
        ("lab", "lab_report"),
        ("pathology", "lab_report"),
        ("radiology", "radiology_report"),
        ("xray", "radiology_report"),
        ("mri", "radiology_report"),
        ("ct", "radiology_report"),
        ("ultrasound", "radiology_report"),
        ("admission", "admission_form"),
        ("surgical", "surgical_note"),
        ("operative", "surgical_note"),
        ("ot", "ot_note"),
        ("indoor", "indoor_case_paper"),
        ("doctor", "doctor_certificate"),
        ("consult", "doctor_certificate"),
        ("registration", "hospital_registration"),
        ("investigation", "field_investigation"),
        ("field", "field_investigation"),
        ("gps", "gps_record"),
        ("aadhaar", "kyc_document"),
        ("pan", "kyc_document"),
        ("kyc", "kyc_document"),
        ("icu", "icu_note"),
        ("histo", "histopathology_report"),
    )
    for token, document_type in mapping:
        if token in combined:
            return document_type
    return "unknown"


def _stringify_metadata_values(values: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in values.items():
        cleaned_key = _clean_text(key)
        cleaned_value = _clean_text(value)
        if cleaned_key and cleaned_value:
            out[cleaned_key] = cleaned_value
    return out


def _build_verifai_document_reference(row: dict[str, Any]) -> dict[str, Any]:
    metadata = _normalize_json_object(row.get("metadata"))
    file_name = _clean_text(row.get("file_name")) or "document"
    storage_key = _clean_text(row.get("storage_key"))
    mime_type = _clean_text(row.get("mime_type")) or "application/octet-stream"
    provider = _clean_text(metadata.get("storage_provider")).lower()
    bucket = _clean_text(metadata.get("bucket"))
    region = _clean_text(metadata.get("region"))
    direct_url = _clean_text(
        metadata.get("external_document_url")
        or metadata.get("external_url")
        or metadata.get("legacy_s3_url")
        or metadata.get("s3_url")
    )

    access_method = "presigned_url"
    reference = direct_url
    reference_hint = "direct_url"

    if provider == "s3" and storage_key:
        try:
            reference = generate_download_url(
                storage_key,
                expires_in=max(900, int(settings.verifai_presigned_url_expires_in)),
            )
            reference_hint = "presigned_s3_url"
        except (StorageOperationError, ValueError, TypeError):
            if bucket:
                access_method = "cross_account_iam"
                reference = f"s3://{bucket}/{storage_key.lstrip('/')}"
                reference_hint = "s3_uri_fallback"

    if not reference and bucket and storage_key:
        access_method = "cross_account_iam"
        reference = f"s3://{bucket}/{storage_key.lstrip('/')}"
        reference_hint = "s3_uri"

    if not reference:
        reference = storage_key

    payload_metadata = _stringify_metadata_values(
        {
            "source_system": "qc-python",
            "storage_provider": provider or "unknown",
            "bucket": bucket,
            "region": region,
            "storage_key": storage_key,
            "reference_hint": reference_hint,
            "direct_url": direct_url,
        }
    )

    return {
        "document_id": str(row.get("id") or ""),
        "file_name": file_name,
        "access_method": access_method,
        "reference": reference,
        "content_type": mime_type,
        "declared_type": _normalize_document_type(file_name, metadata),
        "metadata": payload_metadata,
    }


def _build_verifai_claim_details(claim_row: dict[str, Any], legacy_payload: dict[str, Any]) -> dict[str, Any]:
    patient_name = (
        _clean_text(claim_row.get("patient_name"))
        or _legacy_first_value(legacy_payload, "patient_name", "benefname", "pri_benef_name")
    )
    insurer_name = _legacy_first_value(legacy_payload, "vendor_name", "vendor name", "insurance_company")
    address_parts = [
        _legacy_first_value(legacy_payload, "hospital_city"),
        _legacy_first_value(legacy_payload, "hospital_state"),
        _legacy_first_value(legacy_payload, "hospital_pincode"),
    ]
    return {
        "policy_number": _legacy_first_value(legacy_payload, "policy_number"),
        "member_id": _clean_text(claim_row.get("patient_identifier")) or _legacy_first_value(legacy_payload, "member_id"),
        "insured_name": _legacy_first_value(legacy_payload, "insured_name", "benefname", "pri_benef_name") or patient_name,
        "patient_name": patient_name,
        "hospital_name": _legacy_first_value(legacy_payload, "hospital_name"),
        "hospital_city": _legacy_first_value(legacy_payload, "hospital_city"),
        "admission_date": _parse_date_value(legacy_payload.get("doa_date")),
        "discharge_date": _parse_date_value(legacy_payload.get("dod_date")),
        "claimed_amount": _parse_float_value(legacy_payload.get("claim_amount")),
        "diagnosis": _legacy_first_value(
            legacy_payload,
            "diagnosis",
            "primary_ailment_code",
            "primary_icd_group",
            "bill_deduction_reason",
        ),
        "claim_type": _normalize_claim_type_value(legacy_payload.get("claim_type")),
        "tpa_name": _legacy_first_value(legacy_payload, "tpa_name"),
        "insurance_company": insurer_name,
        "hospital_network_status": _legacy_first_value(legacy_payload, "hospital_is_network"),
        "address": ", ".join([part for part in address_parts if part]),
    }


def _emit_integration_workflow_event(
    db: Session,
    claim_id: UUID,
    actor_id: str,
    event_type: str,
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


def _build_verifai_report_json_url(intake_url: str, verifai_case_id: str) -> str:
    cleaned_intake_url = _clean_text(intake_url)
    cleaned_case_id = _clean_text(verifai_case_id)
    if not cleaned_intake_url or not cleaned_case_id:
        return ""

    parsed = urlsplit(cleaned_intake_url)
    path = (parsed.path or "").rstrip("/")
    report_path = re.sub(
        r"/claims/intake$",
        f"/claims/{cleaned_case_id}/report-json",
        path,
        flags=re.IGNORECASE,
    )
    if report_path == path:
        report_path = f"/api/v1/claims/{cleaned_case_id}/report-json"
    return urlunsplit((parsed.scheme, parsed.netloc, report_path, "", ""))


def _build_verifai_claim_detail_url(intake_url: str, verifai_case_id: str) -> str:
    cleaned_intake_url = _clean_text(intake_url)
    cleaned_case_id = _clean_text(verifai_case_id)
    if not cleaned_intake_url or not cleaned_case_id:
        return ""

    parsed = urlsplit(cleaned_intake_url)
    path = (parsed.path or "").rstrip("/")
    detail_path = re.sub(
        r"/claims/intake$",
        f"/claims/{cleaned_case_id}",
        path,
        flags=re.IGNORECASE,
    )
    if detail_path == path:
        detail_path = f"/api/v1/claims/{cleaned_case_id}"
    return urlunsplit((parsed.scheme, parsed.netloc, detail_path, "", ""))


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
                        -- Manual-only policy: integration intake must not reassign doctors
                        -- or change claim status/completion for existing claims.
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
                        "auto_status_assignment_blocked_for_existing_claim": True,
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


@router.post(
    "/verifai/claims/{claim_id}/queue",
    response_model=VerifAiClaimQueueResponse,
)
def queue_claim_to_verifai(
    claim_id: UUID,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> VerifAiClaimQueueResponse:
    intake_url = _clean_text(settings.verifai_intake_url)
    if not intake_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VerifAI intake URL is not configured",
        )

    claim_row = db.execute(
        text(
            """
            SELECT
                c.id::text AS id,
                c.external_claim_id,
                c.patient_name,
                c.patient_identifier,
                c.source_channel,
                l.legacy_payload
            FROM claims c
            LEFT JOIN claim_legacy_data l ON l.claim_id = c.id
            WHERE c.id = :claim_id
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if claim_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="claim not found")

    legacy_payload = _normalize_json_object(claim_row.get("legacy_payload"))
    ensure_legacy_documents_materialized(db, claim_id)
    document_rows = db.execute(
        text(
            """
            SELECT id::text AS id, file_name, mime_type, storage_key, metadata
            FROM claim_documents
            WHERE claim_id = :claim_id
            ORDER BY uploaded_at ASC, file_name ASC
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().all()
    if not document_rows:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No documents found for this claim. Upload or merge documents first.",
        )

    insurer_name = _legacy_first_value(legacy_payload, "vendor_name", "vendor name", "insurance_company")
    intake_payload: dict[str, Any] = {
        "claim_number": _clean_text(claim_row.get("external_claim_id")) or str(claim_id),
        "insurance_company_code": _normalize_company_code(insurer_name),
        "source_application": _clean_text(settings.verifai_source_application) or "qc-python",
        "claim_details": _build_verifai_claim_details(dict(claim_row), legacy_payload),
        "documents": [_build_verifai_document_reference(dict(row)) for row in document_rows],
        "auto_process": True,
    }

    callback_url = _clean_text(settings.verifai_callback_url)
    callback_secret = _clean_text(settings.verifai_callback_secret)
    if callback_url:
        intake_payload["callback_url"] = callback_url
    if callback_secret:
        intake_payload["callback_secret"] = callback_secret

    try:
        with httpx.Client(timeout=max(5.0, float(settings.verifai_timeout_seconds))) as client:
            response = client.post(intake_url, json=intake_payload)
            response.raise_for_status()
            response_payload = response.json()
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Timed out while sending claim to VerifAI",
        ) from exc
    except httpx.HTTPStatusError as exc:
        remote_detail = exc.response.text.strip()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"VerifAI intake rejected the claim: {remote_detail or exc.response.reason_phrase}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach VerifAI intake: {exc}",
        ) from exc

    _emit_integration_workflow_event(
        db=db,
        claim_id=claim_id,
        actor_id=current_user.username,
        event_type="verifai_claim_queued",
        payload={
            "verifai_case_id": str(response_payload.get("case_id") or ""),
            "verifai_status": str(response_payload.get("status") or ""),
            "verifai_stage": str(response_payload.get("stage") or ""),
            "documents_sent": len(intake_payload["documents"]),
            "endpoint": intake_url,
        },
    )
    db.commit()

    return VerifAiClaimQueueResponse(
        ok=True,
        claim_id=str(claim_id),
        external_claim_id=str(claim_row.get("external_claim_id") or ""),
        documents_sent=len(intake_payload["documents"]),
        verifai_case_id=str(response_payload.get("case_id") or ""),
        verifai_status=str(response_payload.get("status") or ""),
        verifai_stage=str(response_payload.get("stage") or ""),
        verifai_endpoint=intake_url,
        message="Claim queued to VerifAI intake",
    )


@router.get(
    "/verifai/claims/{claim_id}/report-json",
    response_model=VerifAiClaimReportResponse,
)
def get_verifai_claim_report_json(
    claim_id: UUID,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> VerifAiClaimReportResponse:
    del current_user
    intake_url = _clean_text(settings.verifai_intake_url)
    if not intake_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="VerifAI intake URL is not configured",
        )

    claim_row = db.execute(
        text(
            """
            SELECT
                c.id::text AS id,
                c.external_claim_id,
                COALESCE(q.event_payload->>'verifai_case_id', '') AS verifai_case_id,
                COALESCE(q.event_payload->>'verifai_status', '') AS verifai_status,
                COALESCE(q.event_payload->>'verifai_stage', '') AS verifai_stage
            FROM claims c
            LEFT JOIN LATERAL (
                SELECT event_payload
                FROM workflow_events
                WHERE claim_id = c.id
                  AND event_type = 'verifai_claim_queued'
                ORDER BY occurred_at DESC
                LIMIT 1
            ) q ON TRUE
            WHERE c.id = :claim_id
            LIMIT 1
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()
    if claim_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="claim not found")

    verifai_case_id = _clean_text(claim_row.get("verifai_case_id"))
    if not verifai_case_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Claim has not been queued to VerifAI yet",
        )

    report_url = _build_verifai_report_json_url(intake_url, verifai_case_id)
    if not report_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Could not derive VerifAI report endpoint",
        )

    detail_url = _build_verifai_claim_detail_url(intake_url, verifai_case_id)
    live_status = _clean_text(claim_row.get("verifai_status"))
    live_stage = _clean_text(claim_row.get("verifai_stage"))

    try:
        with httpx.Client(timeout=max(5.0, float(settings.verifai_timeout_seconds))) as client:
            response = client.get(report_url)
            response.raise_for_status()
            report_payload = response.json()

            if detail_url:
                try:
                    detail_response = client.get(detail_url)
                    detail_response.raise_for_status()
                    detail_payload = detail_response.json()
                    if isinstance(detail_payload, dict):
                        case_payload = detail_payload.get("case") if isinstance(detail_payload.get("case"), dict) else {}
                        live_status = _clean_text(case_payload.get("status")) or live_status
                        live_stage = _clean_text(case_payload.get("stage")) or live_stage
                except Exception:
                    if isinstance(report_payload, dict) and report_payload:
                        live_status = live_status or "processed"
                        live_stage = live_stage or "report_ready"
    except httpx.TimeoutException as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Timed out while fetching VerifAI report JSON",
        ) from exc
    except httpx.HTTPStatusError as exc:
        remote_detail = exc.response.text.strip()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"VerifAI report JSON request failed: {remote_detail or exc.response.reason_phrase}",
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not reach VerifAI report endpoint: {exc}",
        ) from exc

    if not isinstance(report_payload, dict):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="VerifAI report JSON response was not a JSON object",
        )

    if report_payload:
        live_status = live_status or "processed"
        live_stage = live_stage or "report_ready"

    return VerifAiClaimReportResponse(
        ok=True,
        claim_id=str(claim_id),
        external_claim_id=str(claim_row.get("external_claim_id") or ""),
        verifai_case_id=verifai_case_id,
        verifai_status=live_status,
        verifai_stage=live_stage,
        report_json=report_payload,
        verifai_endpoint=report_url,
        message="VerifAI report JSON fetched successfully",
    )


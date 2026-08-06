import json
import re
from typing import Any
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.schemas.claim import (
    ClaimAssignmentRequest,
    ClaimListResponse,
    ClaimResponse,
    ClaimStatus,
    ClaimStatusUpdateRequest,
    CreateClaimRequest,
)


class DuplicateClaimIdError(Exception):
    pass


class ClaimNotFoundError(Exception):
    pass


def _normalize_tags(raw_tags: Any) -> list[str]:
    if raw_tags is None:
        return []
    if isinstance(raw_tags, list):
        return [str(tag) for tag in raw_tags]
    if isinstance(raw_tags, str):
        try:
            parsed = json.loads(raw_tags)
            if isinstance(parsed, list):
                return [str(tag) for tag in parsed]
        except json.JSONDecodeError:
            return []
    return []


def _normalize_doctor_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def _split_doctor_list(raw: str | None) -> list[str]:
    values: list[str] = []
    for part in str(raw or "").split(","):
        token = _normalize_doctor_token(part)
        if token:
            values.append(token)
    return values

def _build_doctor_membership_clauses(doctor_values: list[str], params: dict[str, Any]) -> list[str]:
    clauses: list[str] = []
    for idx, doctor in enumerate(doctor_values):
        key = f"doctor_{idx}"
        params[key] = _normalize_doctor_token(doctor)
        clauses.append(
            f":{key} = ANY(string_to_array(regexp_replace(lower(COALESCE(assigned_doctor_id, '')), '[^a-z0-9,]+', '', 'g'), ','))"
        )
    return clauses


def _to_claim_response(row: dict[str, Any]) -> ClaimResponse:
    row["tags"] = _normalize_tags(row.get("tags"))
    return ClaimResponse.model_validate(row)


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


def _reset_upload_review_metadata_for_pending(
    db: Session,
    claim_id: UUID,
    actor_id: str | None,
) -> None:
    """
    When a case is moved back to pending, clear completed-upload metadata so
    QC/Tagging/Subtagging/Opinion are re-collected fresh.
    """
    try:
        db.execute(
            text(
                """
                UPDATE claim_report_uploads
                SET
                    report_export_status = 'pending',
                    tagging = NULL,
                    subtagging = NULL,
                    opinion = NULL,
                    qc_status = 'no',
                    updated_by = COALESCE(:actor_id, updated_by),
                    updated_at = NOW()
                WHERE claim_id = :claim_id
                """
            ),
            {"claim_id": str(claim_id), "actor_id": actor_id},
        )
    except Exception as exc:
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None)
        # Ignore missing table on older/partial setups.
        if sqlstate != "42P01":
            raise


def ensure_claim_completed_at_column(db: Session) -> None:
    # DDL is handled by startup migrations (app/db/migrations.py).
    # Keep this function as a no-op so existing call sites remain stable.
    return None
def create_claim(db: Session, payload: CreateClaimRequest, actor_id: str | None = None) -> ClaimResponse:
    ensure_claim_completed_at_column(db)
    try:
        result = db.execute(
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
                    :status,
                    :assigned_doctor_id,
                    :priority,
                    :source_channel,
                    CAST(:tags AS jsonb),
                    CASE WHEN CAST(:status AS claim_status) = 'completed'::claim_status THEN NOW() ELSE NULL END
                )
                RETURNING
                    id,
                    external_claim_id,
                    patient_name,
                    patient_identifier,
                    status,
                    assigned_doctor_id,
                    priority,
                    source_channel,
                    tags,
                    created_at,
                    updated_at
                """
            ),
            {
                "external_claim_id": payload.external_claim_id,
                "patient_name": payload.patient_name,
                "patient_identifier": payload.patient_identifier,
                "status": payload.status.value,
                "assigned_doctor_id": payload.assigned_doctor_id,
                "priority": payload.priority,
                "source_channel": payload.source_channel,
                "tags": json.dumps(payload.tags),
            },
        ).mappings().one()

        claim = _to_claim_response(dict(result))
        _emit_workflow_event(
            db=db,
            claim_id=claim.id,
            event_type="claim_created",
            actor_id=actor_id,
            payload={"status": claim.status.value},
        )
        db.commit()
        return claim
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise DuplicateClaimIdError from exc
        raise


def list_claims(
    db: Session,
    status: ClaimStatus | None,
    assigned_doctor_id: str | None,
    limit: int,
    offset: int,
) -> ClaimListResponse:
    ensure_claim_completed_at_column(db)
    filters: list[str] = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}

    if status is not None:
        filters.append("status = :status")
        params["status"] = status.value

    if assigned_doctor_id:
        doctor_values = _split_doctor_list(assigned_doctor_id)
        if doctor_values:
            doctor_clauses = _build_doctor_membership_clauses(doctor_values, params)
            filters.append("(" + " OR ".join(doctor_clauses) + ")")

    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""

    total = db.execute(
        text(f"SELECT COUNT(*) AS total FROM claims {where_clause}"),
        params,
    ).scalar_one()

    rows = db.execute(
        text(
            f"""
            SELECT
                id,
                external_claim_id,
                patient_name,
                patient_identifier,
                status,
                assigned_doctor_id,
                priority,
                source_channel,
                tags,
                created_at,
                updated_at
            FROM claims
            {where_clause}
            ORDER BY created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = [_to_claim_response(dict(row)) for row in rows]
    return ClaimListResponse(total=total, items=items)


def get_claim(db: Session, claim_id: UUID) -> ClaimResponse:
    ensure_claim_completed_at_column(db)
    row = db.execute(
        text(
            """
            SELECT
                id,
                external_claim_id,
                patient_name,
                patient_identifier,
                status,
                assigned_doctor_id,
                priority,
                source_channel,
                tags,
                created_at,
                updated_at
            FROM claims
            WHERE id = :claim_id
            """
        ),
        {"claim_id": str(claim_id)},
    ).mappings().first()

    if row is None:
        raise ClaimNotFoundError

    return _to_claim_response(dict(row))


def update_claim_status(db: Session, claim_id: UUID, payload: ClaimStatusUpdateRequest) -> ClaimResponse:
    ensure_claim_completed_at_column(db)
    row = db.execute(
        text(
            """
            UPDATE claims
            SET status = CAST(:status AS claim_status),
                completed_at = CASE WHEN CAST(:status AS claim_status) = 'completed'::claim_status THEN COALESCE(completed_at, NOW()) ELSE completed_at END
            WHERE id = :claim_id
            RETURNING
                id,
                external_claim_id,
                patient_name,
                patient_identifier,
                status,
                assigned_doctor_id,
                priority,
                source_channel,
                tags,
                created_at,
                updated_at
            """
        ),
        {"status": payload.status.value, "claim_id": str(claim_id)},
    ).mappings().first()

    if row is None:
        db.rollback()
        raise ClaimNotFoundError

    claim = _to_claim_response(dict(row))
    if claim.status == ClaimStatus.waiting_for_documents:
        _reset_upload_review_metadata_for_pending(
            db=db,
            claim_id=claim.id,
            actor_id=payload.actor_id,
        )
    _emit_workflow_event(
        db=db,
        claim_id=claim.id,
        event_type="claim_status_updated",
        actor_id=payload.actor_id,
        payload={"status": claim.status.value, "note": payload.note},
    )
    db.commit()
    return claim


def assign_claim(db: Session, claim_id: UUID, payload: ClaimAssignmentRequest) -> ClaimResponse:
    ensure_claim_completed_at_column(db)
    if payload.status is None:
        sql_stmt = text(
            """
            UPDATE claims
            SET assigned_doctor_id = :assigned_doctor_id
            WHERE id = :claim_id
            RETURNING
                id,
                external_claim_id,
                patient_name,
                patient_identifier,
                status,
                assigned_doctor_id,
                priority,
                source_channel,
                tags,
                created_at,
                updated_at
            """
        )
        params = {
            "assigned_doctor_id": payload.assigned_doctor_id,
            "claim_id": str(claim_id),
        }
    else:
        sql_stmt = text(
            """
            UPDATE claims
            SET assigned_doctor_id = :assigned_doctor_id,
                status = CAST(:status AS claim_status),
                completed_at = CASE WHEN CAST(:status AS claim_status) = 'completed'::claim_status THEN COALESCE(completed_at, NOW()) ELSE completed_at END
            WHERE id = :claim_id
            RETURNING
                id,
                external_claim_id,
                patient_name,
                patient_identifier,
                status,
                assigned_doctor_id,
                priority,
                source_channel,
                tags,
                created_at,
                updated_at
            """
        )
        params = {
            "assigned_doctor_id": payload.assigned_doctor_id,
            "status": payload.status.value,
            "claim_id": str(claim_id),
        }

    row = db.execute(sql_stmt, params).mappings().first()

    if row is None:
        db.rollback()
        raise ClaimNotFoundError

    claim = _to_claim_response(dict(row))
    _emit_workflow_event(
        db=db,
        claim_id=claim.id,
        event_type="claim_assigned",
        actor_id=payload.actor_id,
        payload={
            "assigned_doctor_id": payload.assigned_doctor_id,
            "status": claim.status.value,
        },
    )
    db.commit()
    return claim

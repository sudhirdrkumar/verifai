from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.deps.auth import require_roles
from app.db.session import SessionLocal, get_db
from app.schemas.auth import UserRole
from app.schemas.extraction import ExtractionListResponse, ExtractionResponse, RunExtractionRequest
from app.services.access_control import doctor_can_access_document
from app.services.auth_service import AuthenticatedUser
from app.services.extraction_providers import ExtractionConfigError, ExtractionProcessingError
from app.services.extractions_service import DocumentNotFoundError, list_document_extractions, run_document_extraction, run_document_extraction_releasing_db
from app.services.storage_service import StorageConfigError, StorageOperationError
from app.utils.db_utils import get_db_context

router = APIRouter(tags=["extractions"])


@router.post("/documents/{document_id}/extract", response_model=ExtractionResponse)
def run_extraction_endpoint(
    document_id: UUID,
    payload: RunExtractionRequest,
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.doctor)),
) -> ExtractionResponse:
    if current_user.role == UserRole.doctor:
        with SessionLocal() as db:
            allowed = doctor_can_access_document(db, document_id, current_user.username)
        if allowed is False:
            raise HTTPException(status_code=403, detail="doctor can extract only assigned claim documents")

    try:
        return run_document_extraction_releasing_db(
            document_id=document_id,
            provider=payload.provider,
            actor_id=payload.actor_id or current_user.username,
            force_refresh=bool(payload.force_refresh),
        )
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except (StorageConfigError, ExtractionConfigError) as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except (StorageOperationError, ExtractionProcessingError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"unexpected extraction endpoint error: {exc}") from exc


@router.get("/documents/{document_id}/extractions", response_model=ExtractionListResponse)
def list_document_extractions_endpoint(
    document_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor)),
) -> ExtractionListResponse:
    with get_db_context() as db:
        if current_user.role == UserRole.doctor:
            allowed = doctor_can_access_document(db, document_id, current_user.username)
            if allowed is False:
                raise HTTPException(status_code=403, detail="doctor can access only assigned claim documents")

        try:
            return list_document_extractions(db, document_id, limit, offset)
        except DocumentNotFoundError as exc:
            raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/extraction-jobs")
def list_extraction_jobs_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    status_filter: str = Query(default="all"),
    current_user: AuthenticatedUser = Depends(
        require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)
    ),
) -> dict:
    del current_user
    normalized_status = str(status_filter or "all").strip().lower()
    if normalized_status not in {"all", "queued", "running", "processing", "succeeded", "failed"}:
        normalized_status = "all"

    where_sql = ""
    params: dict[str, object] = {"limit": int(limit), "offset": int(offset)}
    if normalized_status == "running":
        where_sql = "WHERE ej.status IN ('running', 'processing')"
    elif normalized_status != "all":
        where_sql = "WHERE ej.status = :status_filter"
        params["status_filter"] = normalized_status

    with get_db_context() as db:
        from sqlalchemy import text

        total = db.execute(
            text(f"SELECT COUNT(*) FROM extraction_jobs ej {where_sql}"),
            params,
        ).scalar_one()
        rows = db.execute(
            text(
                f"""
                SELECT
                    ej.id,
                    ej.claim_id,
                    ej.document_id,
                    COALESCE(c.external_claim_id, '') AS external_claim_id,
                    COALESCE(cd.file_name, '') AS file_name,
                    COALESCE(ej.provider, '') AS provider,
                    COALESCE(ej.status, '') AS status,
                    ej.queued_at,
                    ej.started_at,
                    ej.finished_at,
                    COALESCE(ej.error_message, '') AS error_message
                FROM extraction_jobs ej
                LEFT JOIN claims c ON c.id = ej.claim_id
                LEFT JOIN claim_documents cd ON cd.id = ej.document_id
                {where_sql}
                ORDER BY ej.queued_at DESC NULLS LAST, ej.created_at DESC
                LIMIT :limit OFFSET :offset
                """
            ),
            params,
        ).mappings().all()
        count_rows = db.execute(
            text("SELECT COALESCE(status, ''), COUNT(*) FROM extraction_jobs GROUP BY COALESCE(status, '')")
        ).all()

    status_counts = {"queued": 0, "running": 0, "succeeded": 0, "failed": 0}
    for raw_status, raw_count in count_rows:
        key = "running" if str(raw_status or "").lower() in {"running", "processing"} else str(raw_status or "").lower()
        if key in status_counts:
            status_counts[key] += int(raw_count or 0)

    def _iso(value):
        return value.isoformat() if value is not None else None

    return {
        "total": int(total or 0),
        "status_counts": status_counts,
        "items": [
            {
                "job_id": str(row["id"]),
                "claim_id": str(row["claim_id"]),
                "document_id": str(row["document_id"]),
                "external_claim_id": str(row["external_claim_id"] or ""),
                "file_name": str(row["file_name"] or ""),
                "provider": str(row["provider"] or ""),
                "status": str(row["status"] or ""),
                "queued_at": _iso(row["queued_at"]),
                "started_at": _iso(row["started_at"]),
                "finished_at": _iso(row["finished_at"]),
                "error_message": str(row["error_message"] or ""),
            }
            for row in rows
        ],
    }


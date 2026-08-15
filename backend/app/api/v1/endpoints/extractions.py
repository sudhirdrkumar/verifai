from __future__ import annotations
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
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
):
    with get_db_context() as db:
        from sqlalchemy import text
        try:
            query = "SELECT id, claim_id, document_id, status, queued_at, provider FROM extraction_jobs ORDER BY queued_at DESC LIMIT :limit OFFSET :offset"
            result = db.execute(text(query), {"limit": int(limit), "offset": int(offset)}).fetchall()
            jobs = [{"id": str(r[0]), "claim_id": str(r[1]), "document_id": str(r[2]), "status": r[3], "queued_at": r[4].isoformat() if r[4] else None, "provider": r[5]} for r in result]
            return jobs
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))


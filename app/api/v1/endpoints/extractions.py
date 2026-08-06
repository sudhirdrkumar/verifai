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
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor)),
) -> ExtractionListResponse:
    if current_user.role == UserRole.doctor:
        allowed = doctor_can_access_document(db, document_id, current_user.username)
        if allowed is False:
            raise HTTPException(status_code=403, detail="doctor can access only assigned claim documents")

    try:
        return list_document_extractions(db, document_id, limit, offset)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


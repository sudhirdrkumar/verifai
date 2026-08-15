import asyncio
import mimetypes
import os
import shutil
import tempfile
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile, status
from sqlalchemy.orm import Session

from app.api.deps.auth import require_roles
from app.db.session import SessionLocal, get_db
from app.schemas.auth import UserRole
from app.schemas.document import (
    DocumentBulkDeleteRequest,
    DocumentBulkDeleteResponse,
    DocumentDownloadUrlResponse,
    DocumentListResponse,
    DocumentMergeJobResponse,
    DocumentMergeJobStatusResponse,
    DocumentMergeUploadResponse,
    DocumentParseStatusUpdateRequest,
    DocumentResponse,
    DocumentUploadPresignedUrlRequest,
    DocumentUploadPresignedUrlResponse,
    DocumentUploadCompleteRequest,
)
from app.services.access_control import doctor_can_access_claim, doctor_can_access_document
from app.services.auth_service import AuthenticatedUser
from app.services.documents_service import (
    ClaimNotFoundError,
    DocumentMergeError,
    DocumentNotFoundError,
    create_document,
    create_merged_document,
    delete_documents,
    get_document_download_url,
    list_documents,
    update_document_parse_status,
)
from app.services.merge_jobs_service import (
    create_merge_job,
    get_merge_job_status,
    MergeJobNotFoundError,
)
from app.services.storage_service import (
    StorageConfigError,
    StorageOperationError,
    generate_presigned_upload_url,
)

router = APIRouter(tags=["documents"])


def _create_document_in_thread(
    claim_id: UUID,
    file_name: str,
    mime_type: str,
    file_bytes: bytes,
    uploaded_by: str | None,
    retention_class: str,
) -> DocumentResponse:
    with SessionLocal() as db:
        return create_document(
            db=db,
            claim_id=claim_id,
            file_name=file_name,
            mime_type=mime_type,
            file_bytes=file_bytes,
            uploaded_by=uploaded_by,
            retention_class=retention_class,
        )


def _create_merged_document_in_thread(
    claim_id: UUID,
    file_items: list[dict],
    uploaded_by: str | None,
    retention_class: str,
    compression_mode: str,
) -> tuple:
    with SessionLocal() as db:
        return create_merged_document(
            db=db,
            claim_id=claim_id,
            file_items=file_items,
            uploaded_by=uploaded_by,
            retention_class=retention_class,
            compression_mode=compression_mode,
        )


@router.post(
    "/claims/{claim_id}/documents",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document_endpoint(
    claim_id: UUID,
    file: UploadFile = File(...),
    uploaded_by: str | None = Form(default=None),
    retention_class: str = Form(default="standard"),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> DocumentResponse:
    content = await file.read()
    if not content:
        raise HTTPException(status_code=400, detail="empty file upload")

    guessed_mime_type, _ = mimetypes.guess_type(file.filename or "")
    mime_type = file.content_type or guessed_mime_type or "application/octet-stream"

    try:
        # Run S3 upload + DB insert in a thread so the event loop stays free
        return await asyncio.to_thread(
            _create_document_in_thread,
            claim_id,
            file.filename or "document",
            mime_type,
            content,
            uploaded_by or current_user.username,
            retention_class,
        )
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc
    except StorageConfigError as exc:
        raise HTTPException(status_code=500, detail=f"storage config error: {exc}") from exc
    except StorageOperationError as exc:
        raise HTTPException(status_code=502, detail=f"storage operation error: {exc}") from exc


@router.post(
    "/claims/{claim_id}/documents/presigned-url",
    response_model=DocumentUploadPresignedUrlResponse,
    status_code=status.HTTP_200_OK,
)
async def get_document_upload_presigned_url_endpoint(
    claim_id: UUID,
    payload: DocumentUploadPresignedUrlRequest,
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> DocumentUploadPresignedUrlResponse:
    """Generate presigned URL for direct S3 upload (browser → S3, no EC2 bottleneck)"""
    from uuid import uuid4

    storage_key = f"claims/{claim_id}/documents/{uuid4().hex}_{payload.file_name}"

    try:
        upload_url = generate_presigned_upload_url(
            object_key=storage_key,
            content_type=payload.mime_type,
            expires_in=3600,
        )
        return DocumentUploadPresignedUrlResponse(
            upload_url=upload_url,
            storage_key=storage_key,
            expires_in=3600,
            method="PUT",
        )
    except StorageConfigError as exc:
        raise HTTPException(status_code=500, detail=f"storage config error: {exc}") from exc
    except StorageOperationError as exc:
        raise HTTPException(status_code=502, detail=f"storage operation error: {exc}") from exc


@router.post(
    "/claims/{claim_id}/documents/upload-complete",
    response_model=DocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def confirm_document_upload_endpoint(
    claim_id: UUID,
    payload: DocumentUploadCompleteRequest,
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
    db: Session = Depends(get_db),
) -> DocumentResponse:
    """Confirm S3 upload complete and create database record"""
    from app.services.documents_service import create_document_from_s3

    try:
        return create_document_from_s3(
            db=db,
            claim_id=claim_id,
            storage_key=payload.storage_key,
            file_name=payload.file_name,
            mime_type=payload.mime_type,
            file_size_bytes=payload.file_size_bytes,
            checksum_sha256=payload.checksum_sha256,
            uploaded_by=payload.uploaded_by or current_user.username,
            retention_class=payload.retention_class,
        )
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc
    except StorageConfigError as exc:
        raise HTTPException(status_code=500, detail=f"storage config error: {exc}") from exc
    except StorageOperationError as exc:
        raise HTTPException(status_code=502, detail=f"storage operation error: {exc}") from exc


@router.post(
    "/claims/{claim_id}/documents/merge-async",
    response_model=DocumentMergeJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def queue_document_merge_endpoint(
    claim_id: UUID,
    request: Request,
    files: list[UploadFile] | None = File(default=None),
    uploaded_by: str | None = Form(default=None),
    retention_class: str = Form(default="standard"),
    compression_mode: str = Form(default="standard"),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
    db: Session = Depends(get_db),
) -> DocumentMergeJobResponse:
    """Queue PDF merge job - returns immediately with job ID"""
    upload_files = list(files or [])
    if not upload_files:
        form = await request.form()
        for field_name in ("files", "files[]", "file", "documents", "documents[]"):
            for value in form.getlist(field_name):
                if hasattr(value, "filename") and hasattr(value, "read"):
                    upload_files.append(value)

    if not upload_files:
        raise HTTPException(status_code=400, detail="no files received")

    file_items: list[dict] = []
    total_size = 0
    for file in upload_files:
        content = await file.read()
        if not content:
            continue
        guessed_mime_type, _ = mimetypes.guess_type(file.filename or "")
        mime_type = file.content_type or guessed_mime_type or "application/octet-stream"
        file_items.append(
            {
                "file_name": file.filename or "document",
                "mime_type": mime_type,
                "file_bytes": content,
            }
        )
        total_size += len(content)

    if not file_items:
        raise HTTPException(status_code=400, detail="all files are empty")

    temp_dir = tempfile.mkdtemp(prefix=f"qc_merge_{claim_id}_")
    queued_items: list[dict] = []
    try:
        for idx, item in enumerate(file_items, start=1):
            raw_bytes = item.get("file_bytes") or b""
            if not isinstance(raw_bytes, (bytes, bytearray)) or not raw_bytes:
                continue
            safe_name = os.path.basename(str(item.get("file_name") or f"document_{idx}"))
            temp_path = os.path.join(temp_dir, f"{idx:03d}_{safe_name}")
            with open(temp_path, "wb") as handle:
                handle.write(bytes(raw_bytes))
            queued_items.append(
                {
                    "file_name": item.get("file_name") or safe_name,
                    "mime_type": item.get("mime_type") or "application/octet-stream",
                    "temp_file_path": temp_path,
                    "file_size_bytes": len(raw_bytes),
                }
            )

        if not queued_items:
            raise HTTPException(status_code=400, detail="No valid files to merge")

        job_id = create_merge_job(
            db=db,
            claim_id=claim_id,
            file_count=len(queued_items),
            source_size_bytes=total_size,
            job_payload={
                "file_count": len(queued_items),
                "uploaded_by": uploaded_by or current_user.username,
                "retention_class": retention_class,
                "compression_mode": compression_mode,
                "temp_dir": temp_dir,
                "file_items": queued_items,
            },
        )
        return DocumentMergeJobResponse(
            job_id=job_id,
            claim_id=str(claim_id),
            status="pending",
            file_count=len(queued_items),
            message=f"Merge queued for {len(queued_items)} file(s). Check status with job_id.",
        )
    except ClaimNotFoundError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise HTTPException(status_code=404, detail="claim not found") from exc
    except HTTPException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


@router.get(
    "/documents/merge-jobs/{job_id}",
    response_model=DocumentMergeJobStatusResponse,
    status_code=status.HTTP_200_OK,
)
def get_merge_job_status_endpoint(
    job_id: str,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> DocumentMergeJobStatusResponse:
    """Get status of a merge job"""
    try:
        return get_merge_job_status(db, job_id)
    except MergeJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="merge job not found") from exc


@router.post(
    "/claims/{claim_id}/documents/merged",
    response_model=DocumentMergeUploadResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_merged_document_endpoint(
    claim_id: UUID,
    request: Request,
    files: list[UploadFile] | None = File(default=None),
    uploaded_by: str | None = Form(default=None),
    retention_class: str = Form(default="standard"),
    compression_mode: str = Form(default="standard"),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> DocumentMergeUploadResponse:
    upload_files = list(files or [])
    if not upload_files:
        form = await request.form()
        for field_name in ("files", "files[]", "file", "documents", "documents[]"):
            for value in form.getlist(field_name):
                if hasattr(value, "filename") and hasattr(value, "read"):
                    upload_files.append(value)
        if not upload_files:
            for value in form.values():
                if hasattr(value, "filename") and hasattr(value, "read"):
                    upload_files.append(value)

    if not upload_files:
        raise HTTPException(status_code=400, detail="no files received")

    file_items: list[dict] = []
    for file in upload_files:
        content = await file.read()
        if not content:
            continue
        guessed_mime_type, _ = mimetypes.guess_type(file.filename or "")
        mime_type = file.content_type or guessed_mime_type or "application/octet-stream"
        file_items.append(
            {
                "file_name": file.filename or "document",
                "mime_type": mime_type,
                "file_bytes": content,
            }
        )

    if not file_items:
        raise HTTPException(status_code=400, detail="all files are empty")

    try:
        # PDF merge + S3 upload in a thread — event loop stays free for other requests
        (
            document,
            accepted_files,
            skipped_files,
            source_total_size_bytes,
            output_size_bytes,
            saved_size_bytes,
            compression_ratio,
        ) = await asyncio.to_thread(
            _create_merged_document_in_thread,
            claim_id,
            file_items,
            uploaded_by or current_user.username,
            retention_class,
            compression_mode,
        )
        return DocumentMergeUploadResponse(
            document=document,
            source_file_count=len(file_items),
            accepted_file_count=len(accepted_files),
            skipped_file_count=len(skipped_files),
            accepted_files=accepted_files,
            skipped_files=skipped_files,
            merged_source_total_size_bytes=source_total_size_bytes,
            merged_output_size_bytes=output_size_bytes,
            merged_saved_size_bytes=saved_size_bytes,
            merge_compression_ratio=compression_ratio,
        )
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc
    except DocumentMergeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StorageConfigError as exc:
        raise HTTPException(status_code=500, detail=f"storage config error: {exc}") from exc
    except StorageOperationError as exc:
        raise HTTPException(status_code=502, detail=f"storage operation error: {exc}") from exc


@router.get("/claims/{claim_id}/documents", response_model=DocumentListResponse)
def list_documents_endpoint(
    claim_id: UUID,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> DocumentListResponse:
    if current_user.role == UserRole.doctor:
        allowed = doctor_can_access_claim(db, claim_id, current_user.username)
        if allowed is False:
            raise HTTPException(status_code=403, detail="doctor can access only assigned claims")

    try:
        return list_documents(db, claim_id, limit, offset)
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc


@router.delete("/claims/{claim_id}/documents", response_model=DocumentBulkDeleteResponse)
def delete_documents_endpoint(
    claim_id: UUID,
    payload: DocumentBulkDeleteRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user)),
) -> DocumentBulkDeleteResponse:
    try:
        return delete_documents(
            db=db,
            claim_id=claim_id,
            document_ids=payload.document_ids,
            actor_id=payload.actor_id or current_user.username,
        )
    except ClaimNotFoundError as exc:
        raise HTTPException(status_code=404, detail="claim not found") from exc


@router.patch("/documents/{document_id}/parse-status", response_model=DocumentResponse)
def update_document_parse_status_endpoint(
    document_id: UUID,
    payload: DocumentParseStatusUpdateRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.doctor)),
) -> DocumentResponse:
    if current_user.role == UserRole.doctor:
        allowed = doctor_can_access_document(db, document_id, current_user.username)
        if allowed is False:
            raise HTTPException(status_code=403, detail="doctor can access only assigned claim documents")

    enriched_payload = payload.model_copy(update={"actor_id": payload.actor_id or current_user.username})

    try:
        return update_document_parse_status(db, document_id, enriched_payload)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc


@router.get("/documents/{document_id}/download-url", response_model=DocumentDownloadUrlResponse)
def get_document_download_url_endpoint(
    document_id: UUID,
    expires_in: int = Query(default=900, ge=60, le=86400),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)),
) -> DocumentDownloadUrlResponse:
    if current_user.role == UserRole.doctor:
        allowed = doctor_can_access_document(db, document_id, current_user.username)
        if allowed is False:
            raise HTTPException(status_code=403, detail="doctor can access only assigned claim documents")

    try:
        return get_document_download_url(db, document_id, expires_in)
    except DocumentNotFoundError as exc:
        raise HTTPException(status_code=404, detail="document not found") from exc
    except StorageConfigError as exc:
        raise HTTPException(status_code=500, detail=f"storage config error: {exc}") from exc
    except StorageOperationError as exc:
        raise HTTPException(status_code=502, detail=f"storage operation error: {exc}") from exc


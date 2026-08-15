import logging
import os
import shutil
from uuid import UUID

from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.services.documents_service import create_merged_document
from app.services.merge_jobs_service import (
    get_pending_merge_jobs,
    update_merge_job_processing,
    update_merge_job_success,
    update_merge_job_failed,
)

logger = logging.getLogger(__name__)


def process_merge_jobs() -> None:
    """Process pending merge jobs. Call this periodically from a background task."""
    db = SessionLocal()
    try:
        pending_jobs = get_pending_merge_jobs(db, limit=5)
        if not pending_jobs:
            return

        logger.info(f"Found {len(pending_jobs)} pending merge jobs")

        for job_id, claim_id, payload in pending_jobs:
            try:
                process_single_merge_job(db, job_id, claim_id, payload)
            except Exception as e:
                logger.error(f"Error processing job {job_id}: {str(e)}")
                try:
                    update_merge_job_failed(db, job_id, str(e)[:500])
                except Exception as update_error:
                    logger.error(f"Failed to update job {job_id} status: {update_error}")
    finally:
        db.close()


def process_single_merge_job(db: Session, job_id: str, claim_id: UUID, payload: dict) -> None:
    """Process a single merge job"""
    logger.info(f"Processing merge job {job_id} for claim {claim_id}")

    # Mark as processing
    update_merge_job_processing(db, job_id)

    # Extract job payload
    file_items = payload.get("file_items", [])
    uploaded_by = payload.get("uploaded_by", "system")
    retention_class = payload.get("retention_class", "standard")
    compression_mode = payload.get("compression_mode", "standard")
    temp_dir = str(payload.get("temp_dir") or "").strip()

    if not file_items:
        raise ValueError("No files in merge job payload")

    # Process merge
    try:
        normalized_items: list[dict] = []
        for item in file_items:
            temp_file_path = str(item.get("temp_file_path") or "").strip()
            file_bytes = b""
            if temp_file_path and os.path.exists(temp_file_path):
                with open(temp_file_path, "rb") as handle:
                    file_bytes = handle.read()
            elif isinstance(item.get("file_bytes"), (bytes, bytearray)):
                file_bytes = bytes(item["file_bytes"])

            if not file_bytes:
                continue

            normalized_items.append(
                {
                    "file_name": str(item.get("file_name") or "document"),
                    "mime_type": str(item.get("mime_type") or "application/octet-stream"),
                    "file_bytes": file_bytes,
                }
            )

        if not normalized_items:
            raise ValueError("No readable files in merge job payload")

        document, accepted_files, skipped_files, _, output_size, _, _ = create_merged_document(
            db=db,
            claim_id=claim_id,
            file_items=normalized_items,
            uploaded_by=uploaded_by,
            retention_class=retention_class,
            compression_mode=compression_mode,
        )

        # Mark as successful
        update_merge_job_success(db, job_id, document.id)
        logger.info(
            f"Merge job {job_id} completed: {len(accepted_files)} files, "
            f"output size {output_size} bytes"
        )
    except Exception as e:
        logger.error(f"Merge failed for job {job_id}: {str(e)}")
        update_merge_job_failed(db, job_id, str(e)[:500])
        raise
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)

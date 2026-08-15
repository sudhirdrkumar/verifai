import json
from datetime import datetime, timezone
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.schemas.document import DocumentMergeJobStatusResponse


class MergeJobNotFoundError(Exception):
    pass


def create_merge_job(
    db: Session,
    claim_id: UUID,
    file_count: int,
    source_size_bytes: int,
    job_payload: dict,
) -> str:
    """Create a merge job and return job ID"""
    job_id = str(uuid4())

    db.execute(
        text("""
            INSERT INTO document_merge_jobs (
                id, claim_id, status, file_count, source_size_bytes, job_payload, created_at, updated_at
            ) VALUES (
                :job_id, :claim_id, 'pending', :file_count, :source_size_bytes, CAST(:job_payload AS jsonb), NOW(), NOW()
            )
        """),
        {
            "job_id": job_id,
            "claim_id": str(claim_id),
            "file_count": file_count,
            "source_size_bytes": source_size_bytes,
            "job_payload": json.dumps(job_payload),
        },
    )
    db.commit()
    return job_id


def get_merge_job_status(db: Session, job_id: str) -> DocumentMergeJobStatusResponse:
    """Get status of a merge job"""
    row = db.execute(
        text("""
            SELECT id, claim_id, status, file_count, result_document_id, error_message, created_at, updated_at, completed_at
            FROM document_merge_jobs
            WHERE id = :job_id
        """),
        {"job_id": job_id},
    ).mappings().first()

    if not row:
        raise MergeJobNotFoundError(f"Job {job_id} not found")

    return DocumentMergeJobStatusResponse(
        job_id=str(row["id"]),
        claim_id=str(row["claim_id"]),
        status=row["status"],
        file_count=row["file_count"],
        result_document_id=str(row["result_document_id"]) if row["result_document_id"] else None,
        error_message=row["error_message"],
        created_at=row["created_at"].isoformat() if row["created_at"] else None,
        updated_at=row["updated_at"].isoformat() if row["updated_at"] else None,
        completed_at=row["completed_at"].isoformat() if row["completed_at"] else None,
    )


def update_merge_job_processing(db: Session, job_id: str) -> None:
    """Mark job as processing"""
    db.execute(
        text("""
            UPDATE document_merge_jobs
            SET status = 'processing', updated_at = NOW()
            WHERE id = :job_id
        """),
        {"job_id": job_id},
    )
    db.commit()


def update_merge_job_success(
    db: Session,
    job_id: str,
    result_document_id: UUID,
) -> None:
    """Mark job as completed successfully"""
    db.execute(
        text("""
            UPDATE document_merge_jobs
            SET status = 'completed', result_document_id = :result_document_id,
                updated_at = NOW(), completed_at = NOW()
            WHERE id = :job_id
        """),
        {
            "job_id": job_id,
            "result_document_id": str(result_document_id),
        },
    )
    db.commit()


def update_merge_job_failed(
    db: Session,
    job_id: str,
    error_message: str,
) -> None:
    """Mark job as failed"""
    db.execute(
        text("""
            UPDATE document_merge_jobs
            SET status = 'failed', error_message = :error_message,
                updated_at = NOW(), completed_at = NOW()
            WHERE id = :job_id
        """),
        {
            "job_id": job_id,
            "error_message": error_message[:500],  # Truncate to 500 chars
        },
    )
    db.commit()


def get_pending_merge_jobs(db: Session, limit: int = 10) -> list[tuple[str, UUID, list]]:
    """Get pending merge jobs to process"""
    rows = db.execute(
        text("""
            SELECT id, claim_id, job_payload
            FROM document_merge_jobs
            WHERE status = 'pending'
            ORDER BY created_at ASC
            LIMIT :limit
        """),
        {"limit": limit},
    ).mappings().all()

    return [
        (row["id"], UUID(row["claim_id"]), json.loads(row["job_payload"]))
        for row in rows
    ]

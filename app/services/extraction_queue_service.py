from __future__ import annotations
from typing import Optional
import logging
import json
import redis
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db.session import SessionLocal
from app.schemas.extraction import ExtractionJobListItem, ExtractionJobListResponse, ExtractionJobResponse, ExtractionJobStatus, ExtractionProvider

logger = logging.getLogger(__name__)

# Redis connection
try:
    _redis = redis.Redis(host='localhost', port=6379, decode_responses=True)
    _redis.ping()
    logger.info("✓ Connected to Redis")
except Exception as e:
    logger.warning(f"Redis connection failed: {e}. Jobs will not be pushed to worker queue.")
    _redis = None


class ExtractionQueueService:
    def __init__(self) -> None:
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        logger.info("Extraction queue service started")

    def stop(self) -> None:
        self._started = False
        logger.info("Extraction queue service stopped")

    def enqueue(
        self,
        document_id: UUID,
        provider: ExtractionProvider,
        actor_id: Optional[str],
        force_refresh: bool,
        priority: int = 100,
    ) -> ExtractionJobResponse:
        job_id = uuid4()
        with SessionLocal() as db:
            doc = db.execute(
                text(
                    """
                    SELECT id, claim_id, parse_status, storage_key
                    FROM claim_documents
                    WHERE id = :document_id
                    LIMIT 1
                    """
                ),
                {"document_id": str(document_id)},
            ).mappings().first()
            if doc is None:
                raise DocumentNotFoundError

            queued_at = datetime.now().astimezone()
            db.execute(
                text(
                    """
                    INSERT INTO extraction_jobs (
                        id, document_id, claim_id, provider, actor_id, force_refresh,
                        status, priority, queued_at, job_payload
                    )
                    VALUES (
                        :id, :document_id, :claim_id, :provider, :actor_id, :force_refresh,
                        'queued', :priority, :queued_at, CAST(:job_payload AS jsonb)
                    )
                    """
                ),
                {
                    "id": str(job_id),
                    "document_id": str(document_id),
                    "claim_id": str(doc["claim_id"]),
                    "provider": provider.value,
                    "actor_id": actor_id,
                    "force_refresh": bool(force_refresh),
                    "priority": int(priority),
                    "queued_at": queued_at,
                    "job_payload": '{"queued_by":"api"}',
                },
            )
            db.execute(
                text("UPDATE claim_documents SET parse_status = 'queued' WHERE id = :document_id"),
                {"document_id": str(document_id)},
            )
            db.commit()

        # Push to Redis queue for stage1-ocr workers
        if _redis:
            try:
                storage_key = doc.get("storage_key", "")
                # Parse storage_key format: "s3://bucket/key" or "bucket/key" or just "key"
                # Default bucket is rightworks-docs for all paths
                if storage_key.startswith("s3://"):
                    parts = storage_key[5:].split("/", 1)
                    s3_bucket = parts[0]
                    s3_key = parts[1] if len(parts) > 1 else ""
                else:
                    # Default to rightworks-docs bucket - storage_key is the full path within bucket
                    s3_bucket = "rightworks-docs"
                    s3_key = storage_key

                task = {
                    "job_id": str(job_id),
                    "document_id": str(document_id),
                    "claim_id": str(doc["claim_id"]),
                    "s3_bucket": s3_bucket,
                    "s3_key": s3_key,
                    "force_refresh": bool(force_refresh),
                }
                _redis.delete(f"queue:stage2_scheduled:{doc['claim_id']}")
                _redis.delete(f"queue:stage3_scheduled:{doc['claim_id']}")
                _redis.lpush("queue:stage1_ocr_extraction", json.dumps(task))
                logger.info(f"Pushed job to Redis: {document_id}")
            except Exception as e:
                logger.error(f"Failed to push job to Redis: {e}")

        return self.get_job(job_id)

    def get_job(self, job_id: UUID) -> ExtractionJobResponse:
        with SessionLocal() as db:
            row = db.execute(
                text(
                    """
                    SELECT
                        id, document_id, claim_id, provider, status, queued_at, started_at,
                        finished_at, error_message, result_extraction_id
                    FROM extraction_jobs
                    WHERE id = :job_id
                    LIMIT 1
                    """
                ),
                {"job_id": str(job_id)},
            ).mappings().first()
            if row is None:
                raise DocumentNotFoundError
            return ExtractionJobResponse.model_validate(
                {
                    "job_id": row["id"],
                    "document_id": row["document_id"],
                    "claim_id": row["claim_id"],
                    "provider": row["provider"],
                    "status": row["status"],
                    "queued_at": row["queued_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "message": None,
                    "error_message": row["error_message"],
                    "result_extraction_id": row["result_extraction_id"],
                }
            )

    def list_jobs(
        self,
        limit: int,
        offset: int,
        status_filter: str = "all",
        search_claim: Optional[str] = None,
    ) -> ExtractionJobListResponse:
        normalized_status = (status_filter or "all").strip().lower()
        params: dict[str, object] = {"limit": int(limit), "offset": int(offset)}
        filters: list[str] = []

        valid_statuses = {"queued", "running", "succeeded", "failed", "all"}
        if normalized_status not in valid_statuses:
            normalized_status = "all"
        if normalized_status != "all":
            filters.append("ej.status = :status_filter")
            params["status_filter"] = normalized_status
        if search_claim and str(search_claim).strip():
            filters.append("LOWER(COALESCE(c.external_claim_id, '')) LIKE :search_claim")
            params["search_claim"] = f"%{str(search_claim).strip().lower()}%"

        where_sql = ("WHERE " + " AND ".join(filters)) if filters else ""
        with SessionLocal() as db:
            total = db.execute(
                text(
                    f"""
                    SELECT COUNT(*)
                    FROM extraction_jobs ej
                    LEFT JOIN claims c ON c.id = ej.claim_id
                    {where_sql}
                    """
                ),
                params,
            ).scalar_one()

            rows = db.execute(
                text(
                    f"""
                    SELECT
                        ej.id,
                        ej.document_id,
                        ej.claim_id,
                        COALESCE(c.external_claim_id, '') AS external_claim_id,
                        COALESCE(cd.file_name, '') AS file_name,
                        ej.provider,
                        ej.status,
                        ej.queued_at,
                        ej.started_at,
                        ej.finished_at,
                        ej.error_message,
                        ej.result_extraction_id
                    FROM extraction_jobs ej
                    LEFT JOIN claims c ON c.id = ej.claim_id
                    LEFT JOIN claim_documents cd ON cd.id = ej.document_id
                    {where_sql}
                    ORDER BY ej.queued_at DESC, ej.created_at DESC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                params,
            ).mappings().all()

        items = [
            ExtractionJobListItem.model_validate(
                {
                    "id": row["id"],
                    "claim_id": row["claim_id"],
                    "document_id": row["document_id"],
                    "status": row["status"],
                    "queued_at": row["queued_at"],
                    "provider": row["provider"],
                }
            )
            for row in rows
        ]
        return ExtractionJobListResponse(total=int(total or 0), items=items)


extraction_queue_service = ExtractionQueueService()


class DocumentNotFoundError(Exception):
    pass

import logging
import queue
import threading
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import text

from app.db.session import SessionLocal
from app.schemas.extraction import ExtractionJobListItem, ExtractionJobListResponse, ExtractionJobResponse, ExtractionJobStatus, ExtractionProvider
from app.services.extractions_service import DocumentNotFoundError, run_document_extraction_releasing_db

logger = logging.getLogger(__name__)


class ExtractionQueueService:
    def __init__(self) -> None:
        self._queue: queue.Queue[str] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._stop_event.clear()
        self._worker = threading.Thread(target=self._worker_loop, name="extraction-queue-worker", daemon=True)
        self._worker.start()
        self._started = True
        logger.info("Extraction queue worker started")

    def stop(self) -> None:
        self._stop_event.set()
        if self._worker and self._worker.is_alive():
            self._queue.put("")
            self._worker.join(timeout=10)
        self._started = False
        logger.info("Extraction queue worker stopped")

    def enqueue(
        self,
        document_id: UUID,
        provider: ExtractionProvider,
        actor_id: str | None,
        force_refresh: bool,
        priority: int = 100,
    ) -> ExtractionJobResponse:
        job_id = uuid4()
        with SessionLocal() as db:
            doc = db.execute(
                text(
                    """
                    SELECT id, claim_id, parse_status
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

        self._queue.put(str(job_id))
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
        search_claim: str | None = None,
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
                    "job_id": row["id"],
                    "document_id": row["document_id"],
                    "claim_id": row["claim_id"],
                    "external_claim_id": row["external_claim_id"],
                    "file_name": row["file_name"],
                    "provider": row["provider"],
                    "status": row["status"],
                    "queued_at": row["queued_at"],
                    "started_at": row["started_at"],
                    "finished_at": row["finished_at"],
                    "error_message": row["error_message"],
                    "result_extraction_id": row["result_extraction_id"],
                }
            )
            for row in rows
        ]
        return ExtractionJobListResponse(total=int(total or 0), items=items)

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                job_id = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if not job_id:
                continue

            try:
                self._process_job(UUID(job_id))
            except Exception:
                logger.exception("Extraction queue job crashed: %s", job_id)

    def _process_job(self, job_id: UUID) -> None:
        with SessionLocal() as db:
            job = db.execute(
                text(
                    """
                    SELECT id, document_id, provider, actor_id, force_refresh
                    FROM extraction_jobs
                    WHERE id = :job_id
                    FOR UPDATE
                    """
                ),
                {"job_id": str(job_id)},
            ).mappings().first()
            if job is None:
                return

            db.execute(
                text(
                    """
                    UPDATE extraction_jobs
                    SET status = 'running', started_at = COALESCE(started_at, NOW()), updated_at = NOW()
                    WHERE id = :job_id
                    """
                ),
                {"job_id": str(job_id)},
            )
            db.commit()

        result_extraction_id: UUID | None = None
        error_message: str | None = None
        status = "succeeded"
        try:
            extraction_result = run_document_extraction_releasing_db(
                document_id=UUID(str(job["document_id"])),
                provider=ExtractionProvider(str(job["provider"])),
                actor_id=str(job.get("actor_id") or "") or None,
                force_refresh=bool(job.get("force_refresh")),
            )
            result_extraction_id = extraction_result.id
        except Exception as exc:
            status = "failed"
            error_message = str(exc)
            logger.exception("Extraction job failed job_id=%s document_id=%s", job_id, job["document_id"])

        with SessionLocal() as db:
            db.execute(
                text(
                    """
                    UPDATE extraction_jobs
                    SET status = :status,
                        finished_at = NOW(),
                        result_extraction_id = :result_extraction_id,
                        error_message = :error_message,
                        updated_at = NOW()
                    WHERE id = :job_id
                    """
                ),
                {
                    "job_id": str(job_id),
                    "status": status,
                    "result_extraction_id": str(result_extraction_id) if result_extraction_id else None,
                    "error_message": error_message,
                },
            )
            db.commit()


extraction_queue_service = ExtractionQueueService()

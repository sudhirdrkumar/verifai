from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
import redis
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.core.config import settings


QUEUE_NAME = "queue:stage1_ocr_extraction"


def _s3_location(storage_key: str, default_bucket: str) -> tuple[str, str]:
    key = str(storage_key or "").strip()
    bucket = str(default_bucket or "rightworks-docs").strip() or "rightworks-docs"
    if key.startswith("s3://"):
        location = key[5:].split("/", 1)
        bucket = location[0]
        key = location[1] if len(location) > 1 else ""
    return bucket, key


def reconcile(*, apply: bool) -> dict[str, object]:
    load_dotenv()
    redis_client = redis.Redis(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
        decode_responses=True,
    )
    default_bucket = os.getenv("S3_BUCKET", "rightworks-docs")

    with psycopg.connect(settings.psycopg_database_uri) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (cd.id)
                    cd.id,
                    cd.claim_id,
                    COALESCE(cd.storage_key, ''),
                    COALESCE(cd.parse_status::text, ''),
                    EXISTS (
                        SELECT 1
                        FROM document_extractions de
                        WHERE de.document_id = cd.id
                        AND COALESCE(de.raw_response, '') <> ''
                    ) AS has_extraction
                FROM claim_documents cd
                JOIN extraction_jobs ej ON ej.document_id = cd.id
                WHERE ej.status IN ('queued', 'processing', 'running')
                ORDER BY cd.id, ej.queued_at DESC NULLS LAST
                """
            )
            rows = cur.fetchall()
            cur.execute(
                """
                SELECT DISTINCT cd.claim_id
                FROM claim_documents cd
                JOIN extraction_jobs ej ON ej.document_id = cd.id
                WHERE ej.status IN ('queued', 'processing', 'running')
                  AND NOT EXISTS (
                      SELECT 1 FROM claim_structured_data csd
                      WHERE csd.claim_id = cd.claim_id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM claim_documents pending_cd
                      WHERE pending_cd.claim_id = cd.claim_id
                        AND NOT EXISTS (
                            SELECT 1 FROM document_extractions de
                            WHERE de.document_id = pending_cd.id
                              AND COALESCE(de.raw_response, '') <> ''
                        )
                  )
                """
            )
            ready_stage2_claims = [str(row[0]) for row in cur.fetchall()]

        completed = []
        pending = []
        missing_storage = []
        for document_id, claim_id, storage_key, parse_status, has_extraction in rows:
            bucket, key = _s3_location(storage_key, default_bucket)
            item = {
                "document_id": str(document_id),
                "claim_id": str(claim_id),
                "s3_bucket": bucket,
                "s3_key": key,
            }
            if has_extraction or str(parse_status).lower() == "succeeded":
                completed.append(item)
            elif key:
                pending.append(item)
            else:
                missing_storage.append(item)

        summary: dict[str, object] = {
            "apply": apply,
            "redis_before": redis_client.llen(QUEUE_NAME),
            "active_documents": len(rows),
            "already_extracted": len(completed),
            "pending_requeued": len(pending),
            "missing_storage": len(missing_storage),
            "ready_stage2_claims": len(ready_stage2_claims),
        }
        if not apply:
            return summary

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_key = f"backup:{QUEUE_NAME}:{timestamp}"
        if redis_client.exists(QUEUE_NAME):
            redis_client.rename(QUEUE_NAME, backup_key)
            redis_client.expire(backup_key, 86400)
            summary["redis_backup_key"] = backup_key

        with conn.cursor() as cur:
            completed_ids = [item["document_id"] for item in completed]
            pending_ids = [item["document_id"] for item in pending]
            missing_ids = [item["document_id"] for item in missing_storage]

            if completed_ids:
                cur.execute(
                    """
                    UPDATE extraction_jobs
                    SET status = 'succeeded', finished_at = COALESCE(finished_at, NOW()),
                        error_message = NULL
                    WHERE document_id = ANY(%s::uuid[])
                      AND status IN ('queued', 'processing', 'running')
                    """,
                    (completed_ids,),
                )
                cur.execute(
                    "UPDATE claim_documents SET parse_status = 'succeeded' WHERE id = ANY(%s::uuid[])",
                    (completed_ids,),
                )

            if pending_ids:
                cur.execute(
                    """
                    UPDATE extraction_jobs
                    SET status = 'queued', started_at = NULL, finished_at = NULL,
                        error_message = NULL, queued_at = COALESCE(queued_at, NOW())
                    WHERE document_id = ANY(%s::uuid[])
                      AND status IN ('queued', 'processing', 'running')
                    """,
                    (pending_ids,),
                )
                cur.execute(
                    "UPDATE claim_documents SET parse_status = 'queued' WHERE id = ANY(%s::uuid[])",
                    (pending_ids,),
                )

            if missing_ids:
                cur.execute(
                    """
                    UPDATE extraction_jobs
                    SET status = 'failed', finished_at = NOW(),
                        error_message = 'Document storage_key is missing'
                    WHERE document_id = ANY(%s::uuid[])
                      AND status IN ('queued', 'processing', 'running')
                    """,
                    (missing_ids,),
                )
                cur.execute(
                    "UPDATE claim_documents SET parse_status = 'failed' WHERE id = ANY(%s::uuid[])",
                    (missing_ids,),
                )

        conn.commit()

        if pending:
            pipeline = redis_client.pipeline(transaction=False)
            for item in reversed(pending):
                pipeline.lpush(QUEUE_NAME, json.dumps(item))
            pipeline.execute()

        stage2_queued = 0
        for claim_id in ready_stage2_claims:
            schedule_key = f"queue:stage2_scheduled:{claim_id}"
            if redis_client.set(schedule_key, "1", nx=True, ex=21600):
                redis_client.lpush("queue:stage2_claim_reduction", json.dumps({"claim_id": claim_id}))
                stage2_queued += 1

        summary["redis_after"] = redis_client.llen(QUEUE_NAME)
        summary["stage2_queued"] = stage2_queued
        return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Reconcile DB extraction jobs with the Redis Stage 1 queue.")
    parser.add_argument("--apply", action="store_true", help="Apply repairs; otherwise perform a dry-run.")
    args = parser.parse_args()
    print(json.dumps(reconcile(apply=args.apply), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

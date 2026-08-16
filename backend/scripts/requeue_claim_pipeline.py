from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from uuid import uuid4

import psycopg


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from stage2_reduction_worker import DB_DSN, r  # noqa: E402


QUEUE_NAMES = (
    'queue:stage1_ocr_extraction',
    'queue:stage2_claim_reduction',
    'queue:stage3_report_generation',
)


def task_belongs_to_claim(raw_task: str, claim_id: str) -> bool:
    try:
        return str(json.loads(raw_task).get('claim_id') or '') == claim_id
    except (TypeError, ValueError, json.JSONDecodeError):
        return False


def purge_claim_tasks(claim_id: str) -> dict[str, int]:
    removed: dict[str, int] = {}
    for queue_name in QUEUE_NAMES:
        tasks = r.lrange(queue_name, 0, -1)
        kept = [task for task in tasks if not task_belongs_to_claim(task, claim_id)]
        removed[queue_name] = len(tasks) - len(kept)
        if removed[queue_name]:
            pipe = r.pipeline(transaction=True)
            pipe.delete(queue_name)
            if kept:
                pipe.rpush(queue_name, *kept)
            pipe.execute()
    return removed


def inspect_claim(external_claim_id: str) -> dict:
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT id FROM claims WHERE external_claim_id = %s ORDER BY created_at DESC LIMIT 1',
                (external_claim_id,),
            )
            row = cur.fetchone()
            if not row:
                raise ValueError(f'claim {external_claim_id} was not found')
            claim_id = str(row[0])
            cur.execute(
                '''
                SELECT
                    (SELECT COUNT(*) FROM claim_documents WHERE claim_id = %s),
                    (SELECT COUNT(*) FROM document_extractions WHERE claim_id = %s),
                    (SELECT COUNT(*) FROM extraction_jobs WHERE claim_id = %s AND status = 'queued'),
                    (SELECT COUNT(*) FROM extraction_jobs WHERE claim_id = %s AND status = 'processing'),
                    (SELECT COUNT(*) FROM extraction_jobs WHERE claim_id = %s AND status = 'succeeded'),
                    (SELECT COUNT(*) FROM extraction_jobs WHERE claim_id = %s AND status = 'failed'),
                    (SELECT COUNT(*) FROM claim_structured_data WHERE claim_id = %s),
                    (SELECT COUNT(*) FROM medical_reports WHERE claim_id = %s),
                    (SELECT COUNT(*) FROM report_versions WHERE claim_id = %s)
                ''',
                (claim_id,) * 9,
            )
            counts = cur.fetchone()

    queue_counts = {}
    for queue_name in QUEUE_NAMES:
        queue_counts[queue_name] = sum(
            1 for task in r.lrange(queue_name, 0, -1)
            if task_belongs_to_claim(task, claim_id)
        )
    return {
        'external_claim_id': external_claim_id,
        'claim_id': claim_id,
        'documents': counts[0],
        'extractions': counts[1],
        'jobs': {
            'queued': counts[2],
            'processing': counts[3],
            'succeeded': counts[4],
            'failed': counts[5],
        },
        'structured_records': counts[6],
        'medical_reports': counts[7],
        'report_versions': counts[8],
        'redis_tasks': queue_counts,
    }


def requeue_claim(external_claim_id: str) -> dict:
    state = inspect_claim(external_claim_id)
    claim_id = state['claim_id']
    removed_tasks = purge_claim_tasks(claim_id)

    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', (claim_id,))
            cur.execute(
                '''
                UPDATE extraction_jobs
                SET status = 'failed', finished_at = NOW(),
                    error_message = 'Stopped for explicit claim requeue'
                WHERE claim_id = %s AND status IN ('queued', 'processing')
                ''',
                (claim_id,),
            )
            stopped_jobs = cur.rowcount
            cur.execute(
                'SELECT id, storage_key FROM claim_documents WHERE claim_id = %s ORDER BY uploaded_at, id',
                (claim_id,),
            )
            documents = cur.fetchall()
            if not documents:
                raise ValueError(f'claim {external_claim_id} has no documents')

            tasks = []
            job_ids = []
            for document_id, storage_key in documents:
                job_id = str(uuid4())
                job_ids.append(job_id)
                cur.execute(
                    '''
                    INSERT INTO extraction_jobs (
                        id, document_id, claim_id, provider, actor_id, force_refresh,
                        status, priority, queued_at, job_payload
                    )
                    VALUES (%s, %s, %s, 'openai', 'pipeline-requeue', TRUE,
                            'queued', 100, NOW(), %s::jsonb)
                    ''',
                    (job_id, document_id, claim_id, json.dumps({'queued_by': 'pipeline-requeue'})),
                )
                cur.execute(
                    "UPDATE claim_documents SET parse_status = 'queued' WHERE id = %s",
                    (document_id,),
                )
                bucket = 'rightworks-docs'
                key = str(storage_key or '')
                if key.startswith('s3://'):
                    location = key[5:].split('/', 1)
                    bucket = location[0]
                    key = location[1] if len(location) > 1 else ''
                tasks.append(json.dumps({
                    'job_id': job_id,
                    'document_id': str(document_id),
                    'claim_id': claim_id,
                    's3_bucket': bucket,
                    's3_key': key,
                    'force_refresh': True,
                }))
        conn.commit()

    r.delete(f'queue:stage2_scheduled:{claim_id}')
    r.delete(f'queue:stage3_scheduled:{claim_id}')
    if tasks:
        r.lpush('queue:stage1_ocr_extraction', *tasks)
    return {
        **state,
        'stopped_jobs': stopped_jobs,
        'removed_tasks': removed_tasks,
        'queued_documents': len(tasks),
        'job_ids': job_ids,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Inspect or safely requeue one claim pipeline.')
    parser.add_argument('external_claim_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    result = requeue_claim(args.external_claim_id) if args.apply else inspect_claim(args.external_claim_id)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()

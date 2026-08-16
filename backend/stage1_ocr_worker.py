import os
import json
import time
import re
import boto3
import redis
import psycopg
import base64
import logging
import mimetypes
import requests
from dotenv import load_dotenv

from ocr_batching import (
    excluded_document_reason,
    group_documents_by_size,
    parse_batched_ocr_response,
)

# Load .env file
load_dotenv()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://postgres:Dhoom*2690@127.0.0.1:5432/qc_bkp_modern')
AWS_REGION = os.getenv('AWS_REGION', 'ap-south-1')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
S3_BUCKET = os.getenv('S3_BUCKET', 'rightworks-docs')
S3_ACCESS_KEY = os.getenv('S3_ACCESS_KEY')
S3_SECRET_KEY = os.getenv('S3_SECRET_KEY')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
OPENAI_MODEL = os.getenv('OPENAI_VISION_MODEL', 'gpt-4o-mini')
OPENAI_MAX_FILE_BYTES = int(os.getenv('OPENAI_MAX_FILE_BYTES', 20 * 1024 * 1024))
OPENAI_MAX_OUTPUT_TOKENS = int(os.getenv('OPENAI_MAX_OUTPUT_TOKENS', 16000))
OPENAI_BATCH_MAX_BYTES = int(os.getenv('OPENAI_BATCH_MAX_BYTES', 7 * 1024 * 1024))
OPENAI_BATCH_MAX_FILES = max(1, int(os.getenv('OPENAI_BATCH_MAX_FILES', 4)))
STAGE1_QUEUE = 'queue:stage1_ocr_extraction'
STAGE1_DELAYED_CLAIMS = 'queue:stage1_ocr_extraction:delayed_claims'
STAGE1_RECOVERY_LOCK = 'lock:stage1_ocr_extraction:recover_stale_jobs'
STAGE1_RECOVERY_INTERVAL_SECONDS = max(
    15,
    int(os.getenv('STAGE1_RECOVERY_INTERVAL_SECONDS', 30)),
)
STAGE1_RECOVERY_STALE_SECONDS = max(
    60,
    int(os.getenv('STAGE1_RECOVERY_STALE_SECONDS', 120)),
)

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)

POP_DUE_CLAIM_TASKS = r.register_script('''
local score = redis.call('ZSCORE', KEYS[1], ARGV[1])
if not score or tonumber(score) > tonumber(ARGV[2]) then
    return {}
end
redis.call('ZREM', KEYS[1], ARGV[1])
local tasks = redis.call('HVALS', KEYS[2])
redis.call('DEL', KEYS[2])
return tasks
''')


def pop_due_claim_tasks():
    for claim_id in r.zrangebyscore(STAGE1_DELAYED_CLAIMS, 0, time.time(), start=0, num=10):
        task_key = f'queue:stage1_ocr_extraction:claim:{claim_id}'
        raw_tasks = POP_DUE_CLAIM_TASKS(
            keys=[STAGE1_DELAYED_CLAIMS, task_key],
            args=[claim_id, time.time()],
        )
        if raw_tasks:
            tasks = [json.loads(raw_task) for raw_task in raw_tasks]
            logger.info('Claim %s debounce complete with %s documents', claim_id, len(tasks))
            return tasks
    return []

# Create S3 client with explicit credentials if available
s3_kwargs = {'region_name': AWS_REGION}
if S3_ACCESS_KEY and S3_SECRET_KEY:
    s3_kwargs['aws_access_key_id'] = S3_ACCESS_KEY
    s3_kwargs['aws_secret_access_key'] = S3_SECRET_KEY
s3 = boto3.client('s3', **s3_kwargs)

textract = boto3.client('textract', region_name=AWS_REGION)

def clean_and_compress_ocr(raw_text: str) -> str:
    if not raw_text:
        return ""
    cleaned = re.sub(r'[-_|=+\\.]{4,}', ' ', raw_text)
    cleaned = re.sub(r'(?<=\s)[~`\^_\xa0](?=\s)', ' ', cleaned)
    cleaned = re.sub(r'^[^\w\s]{3,}$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'\n{2,}', '\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip()

def get_file_size_s3(bucket, key, retries=3):
    """Get file size from S3 with retry logic"""
    for attempt in range(retries):
        try:
            response = s3.head_object(Bucket=bucket, Key=key)
            return response['ContentLength']
        except Exception as e:
            if attempt < retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                logger.warning(f'S3 access failed (attempt {attempt+1}/{retries}), retrying in {wait_time}s: {e}')
                time.sleep(wait_time)
            else:
                logger.error(f'S3 access failed after {retries} attempts: {e}')
                return None
    return None

def extract_with_openai_vision(bucket, key, file_size):
    """Extract one PDF/image through the OpenAI Responses API."""
    if not OPENAI_API_KEY:
        logger.warning('OPENAI_API_KEY not set, skipping OpenAI extraction')
        return None
    if file_size > OPENAI_MAX_FILE_BYTES:
        logger.info(
            'Skipping OpenAI for %s: %.2fMB exceeds %.2fMB limit',
            key,
            file_size / 1024 / 1024,
            OPENAI_MAX_FILE_BYTES / 1024 / 1024,
        )
        return None

    mime_type = mimetypes.guess_type(key)[0] or 'application/octet-stream'
    if mime_type != 'application/pdf' and not mime_type.startswith('image/'):
        logger.info('Skipping OpenAI for unsupported media type %s', mime_type)
        return None

    try:
        file_bytes = s3.get_object(Bucket=bucket, Key=key)['Body'].read()
        data_url = f'data:{mime_type};base64,{base64.b64encode(file_bytes).decode("ascii")}'
        file_name = key.rsplit('/', 1)[-1] or 'claim-document'
        file_part = (
            {'type': 'input_file', 'filename': file_name, 'file_data': data_url}
            if mime_type == 'application/pdf'
            else {'type': 'input_image', 'image_url': data_url}
        )
        payload = {
            'model': OPENAI_MODEL,
            'input': [{
                'role': 'user',
                'content': [
                    {
                        'type': 'input_text',
                        'text': (
                            'Extract all readable text from this medical claim document. '
                            'Preserve headings, table rows, dates, medicine names, investigation '
                            'values, units, and reference ranges. Return extracted text only.'
                        ),
                    },
                    file_part,
                ],
            }],
            'max_output_tokens': OPENAI_MAX_OUTPUT_TOKENS,
        }
        response = requests.post(
            'https://api.openai.com/v1/responses',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json=payload,
            timeout=180,
        )
        if response.status_code != 200:
            logger.warning('OpenAI API error %s: %s', response.status_code, response.text[:500])
            return None

        output_parts = []
        for item in response.json().get('output', []):
            if item.get('type') != 'message':
                continue
            for part in item.get('content', []):
                if part.get('type') == 'output_text' and part.get('text'):
                    output_parts.append(part['text'])
        extracted_text = '\n'.join(output_parts).strip()
        if extracted_text:
            logger.info('OpenAI %s extracted %s chars from %s', OPENAI_MODEL, len(extracted_text), key)
            return extracted_text
        logger.warning('OpenAI returned no output text for %s', key)
        return None
    except Exception as e:
        logger.warning('OpenAI extraction failed for %s: %s', key, e)
        return None


def extract_with_openai_vision_batch(documents):
    """Extract multiple PDFs in one request and return text keyed by document ID."""
    if not OPENAI_API_KEY or len(documents) < 2:
        return {}

    total_bytes = sum(int(document['file_size']) for document in documents)
    if total_bytes > OPENAI_BATCH_MAX_BYTES:
        logger.warning('Skipping oversized OpenAI batch: %.2fMB', total_bytes / 1024 / 1024)
        return {}

    try:
        content = [{
            'type': 'input_text',
            'text': (
                'OCR each attached medical-claim PDF independently. Do not mix patient or document data. '
                'Preserve headings, tables, dates, medicine names, investigation values, units, and reference ranges. '
                'Return ONLY valid JSON in this exact shape: '
                '{"documents":[{"document_id":"the supplied ID","text":"complete extracted text"}]}. '
                'Return exactly one item for every supplied document ID.'
            ),
        }]
        expected_ids = set()
        for document in documents:
            document_id = str(document['document_id'])
            expected_ids.add(document_id)
            file_bytes = s3.get_object(Bucket=document['s3_bucket'], Key=document['s3_key'])['Body'].read()
            data_url = 'data:application/pdf;base64,' + base64.b64encode(file_bytes).decode('ascii')
            content.extend([
                {
                    'type': 'input_text',
                    'text': f'Document ID: {document_id}\nFilename: {document["file_name"]}',
                },
                {
                    'type': 'input_file',
                    'filename': document['file_name'],
                    'file_data': data_url,
                },
            ])

        response = requests.post(
            'https://api.openai.com/v1/responses',
            headers={
                'Authorization': f'Bearer {OPENAI_API_KEY}',
                'Content-Type': 'application/json',
            },
            json={
                'model': OPENAI_MODEL,
                'input': [{'role': 'user', 'content': content}],
                'max_output_tokens': OPENAI_MAX_OUTPUT_TOKENS,
            },
            timeout=240,
        )
        if response.status_code != 200:
            logger.warning('OpenAI batch API error %s: %s', response.status_code, response.text[:500])
            return {}

        response_body = response.json()
        output_parts = []
        for item in response_body.get('output', []):
            if item.get('type') != 'message':
                continue
            for part in item.get('content', []):
                if part.get('type') == 'output_text' and part.get('text'):
                    output_parts.append(part['text'])
        if response_body.get('status') == 'incomplete':
            logger.warning('OpenAI batch response incomplete: %s', response_body.get('incomplete_details'))
            return {}

        results = parse_batched_ocr_response('\n'.join(output_parts), expected_ids)
        logger.info(
            'OpenAI %s batch extracted %s/%s PDFs in one request (%.2fMB)',
            OPENAI_MODEL,
            len(results),
            len(documents),
            total_bytes / 1024 / 1024,
        )
        return results
    except Exception as e:
        logger.warning('OpenAI batch extraction failed: %s', e, exc_info=True)
        return {}

def extract_with_textract(bucket, key):
    """Fallback: Use AWS Textract for text extraction"""
    try:
        logger.info(f'Running Textract for {key}')

        resp = textract.start_document_text_detection(
            DocumentLocation={'S3Object': {'Bucket': bucket, 'Name': key}}
        )
        job_id = resp['JobId']
        logger.info(f'Textract job started: {job_id}')

        # Wait for job completion
        while True:
            status = textract.get_document_text_detection(JobId=job_id)['JobStatus']
            if status == 'SUCCEEDED':
                logger.info(f'Textract job {job_id} succeeded')
                break
            elif status == 'FAILED':
                raise Exception('AWS Textract processing failed')
            logger.info(f'Textract job {job_id} status: {status}')
            time.sleep(5)

        # Collect text
        text_lines = []
        next_token = None
        while True:
            params = {'JobId': job_id}
            if next_token:
                params['NextToken'] = next_token
            res = textract.get_document_text_detection(**params)
            for block in res.get('Blocks', []):
                if block['BlockType'] == 'LINE':
                    text_lines.append(block['Text'])
            next_token = res.get('NextToken')
            if not next_token:
                break

        raw_ocr = '\n'.join(text_lines)
        logger.info(f'Textract extracted {len(text_lines)} lines, {len(raw_ocr)} chars')
        return raw_ocr

    except Exception as e:
        logger.error(f'Textract extraction failed: {e}')
        return None

def update_extraction_job(doc_id, status, error_msg=None, job_id=None):
    try:
        conn = psycopg.connect(DB_DSN)
        cur = conn.cursor()
        target_job_sql = '''
            id = COALESCE(
                %s::uuid,
                (
                    SELECT id FROM extraction_jobs
                    WHERE document_id = %s AND status IN ('queued', 'processing')
                    ORDER BY queued_at DESC NULLS LAST, created_at DESC
                    LIMIT 1
                )
            )
        '''
        if status == 'processing':
            cur.execute(
                f"UPDATE extraction_jobs SET status = %s, started_at = NOW() WHERE {target_job_sql} AND status = 'queued'",
                ('processing', job_id, doc_id),
            )
        elif status == 'succeeded':
            cur.execute(
                f'UPDATE extraction_jobs SET status = %s, finished_at = NOW(), error_message = NULL WHERE {target_job_sql}',
                ('succeeded', job_id, doc_id),
            )
        elif status == 'failed':
            cur.execute(
                f'UPDATE extraction_jobs SET status = %s, finished_at = NOW(), error_message = %s WHERE {target_job_sql}',
                ('failed', error_msg, job_id, doc_id),
            )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f'Failed to update extraction job: {str(e)}')


def get_document_state(doc_id):
    """Resolve canonical storage metadata and detect completed OCR."""
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT cd.claim_id, cd.storage_key, cd.parse_status, cd.file_name,
                       EXISTS (
                           SELECT 1 FROM document_extractions de
                           WHERE de.document_id = cd.id
                       ) AS has_extraction
                FROM claim_documents cd
                WHERE cd.id = %s
            ''', (doc_id,))
            row = cur.fetchone()
    if not row:
        return None
    return {
        'claim_id': str(row[0]),
        'storage_key': str(row[1] or ''),
        'parse_status': str(row[2] or ''),
        'file_name': str(row[3] or ''),
        'has_extraction': bool(row[4]) or str(row[2] or '').lower() == 'succeeded',
    }


def normalize_s3_location(storage_key, default_bucket):
    key = str(storage_key or '').strip()
    bucket = str(default_bucket or S3_BUCKET).strip() or S3_BUCKET
    if key.startswith('s3://'):
        location = key[5:].split('/', 1)
        bucket = location[0]
        key = location[1] if len(location) > 1 else ''
    return bucket, key


def reconcile_stale_extraction_jobs():
    """Repair DB jobs that were left active after their Redis task disappeared."""
    if not r.set(
        STAGE1_RECOVERY_LOCK,
        str(os.getpid()),
        nx=True,
        ex=STAGE1_RECOVERY_INTERVAL_SECONDS,
    ):
        return

    recovered_tasks = []
    completed_jobs = 0
    try:
        with psycopg.connect(DB_DSN) as conn:
            with conn.cursor() as cur:
                cur.execute('''
                    UPDATE extraction_jobs ej
                    SET status = 'succeeded',
                        finished_at = COALESCE(ej.finished_at, NOW()),
                        error_message = NULL
                    WHERE ej.status IN ('queued', 'processing', 'running')
                      AND EXISTS (
                          SELECT 1 FROM document_extractions de
                          WHERE de.document_id = ej.document_id
                      )
                    RETURNING ej.claim_id
                ''')
                completed_jobs = cur.rowcount
                completed_claim_ids = {
                    str(row[0]) for row in cur.fetchall()
                }
                cur.execute('''
                    UPDATE claim_documents cd
                    SET parse_status = 'succeeded'
                    WHERE COALESCE(cd.parse_status::text, '') <> 'succeeded'
                      AND EXISTS (
                          SELECT 1 FROM document_extractions de
                          WHERE de.document_id = cd.id
                      )
                ''')
                cur.execute('''
                    SELECT DISTINCT ON (ej.document_id)
                        ej.id, ej.document_id, ej.claim_id,
                        COALESCE(cd.storage_key, ''), COALESCE(cd.file_name, '')
                    FROM extraction_jobs ej
                    JOIN claim_documents cd ON cd.id = ej.document_id
                    WHERE ej.status = 'queued'
                      AND ej.queued_at < NOW() - (%s * INTERVAL '1 second')
                      AND NOT EXISTS (
                          SELECT 1 FROM document_extractions de
                          WHERE de.document_id = ej.document_id
                      )
                    ORDER BY ej.document_id, ej.queued_at DESC NULLS LAST, ej.created_at DESC
                    LIMIT 100
                ''', (STAGE1_RECOVERY_STALE_SECONDS,))
                rows = cur.fetchall()

        for job_id, document_id, claim_id, storage_key, file_name in rows:
            marker = f'queue:stage1_ocr_extraction:recovered:{job_id}'
            if not r.set(marker, '1', nx=True, ex=600):
                continue

            bucket, key = normalize_s3_location(storage_key, S3_BUCKET)
            if not key:
                update_extraction_job(
                    str(document_id),
                    'failed',
                    'Document storage_key is missing',
                    str(job_id),
                )
                continue

            recovered_tasks.append({
                'job_id': str(job_id),
                'document_id': str(document_id),
                'claim_id': str(claim_id),
                's3_bucket': bucket,
                's3_key': key,
                'file_name': str(file_name or ''),
                'force_refresh': False,
            })

        if recovered_tasks:
            pipe = r.pipeline(transaction=False)
            claim_ids = set()
            for task in recovered_tasks:
                claim_id = task['claim_id']
                claim_ids.add(claim_id)
                task_key = f'queue:stage1_ocr_extraction:claim:{claim_id}'
                pipe.hset(task_key, task['job_id'], json.dumps(task))
                pipe.expire(task_key, 86400)
            for claim_id in claim_ids:
                pipe.zadd(STAGE1_DELAYED_CLAIMS, {claim_id: time.time()})
            pipe.execute()

        if completed_jobs or recovered_tasks:
            logger.info(
                'Stage 1 recovery closed %s stale jobs and restored %s missing tasks',
                completed_jobs,
                len(recovered_tasks),
            )

        for claim_id in completed_claim_ids:
            queue_next_stage_if_ready(claim_id)
    except Exception:
        r.delete(STAGE1_RECOVERY_LOCK)
        raise


def save_document_extraction(document, raw_text, model_name, extracted_entities='{}'):
    compressed_text = clean_and_compress_ocr(raw_text)
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                INSERT INTO document_extractions (
                    document_id, claim_id, extraction_version, model_name,
                    extracted_entities, raw_response, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (document_id, extraction_version) DO UPDATE SET
                    model_name = EXCLUDED.model_name,
                    extracted_entities = EXCLUDED.extracted_entities,
                    raw_response = EXCLUDED.raw_response,
                    created_at = NOW()
            ''', (
                document['document_id'],
                document['claim_id'],
                'stage1_ocr_v2',
                model_name,
                extracted_entities,
                compressed_text,
            ))
            cur.execute(
                'UPDATE claim_documents SET parse_status = %s WHERE id = %s',
                ('succeeded', document['document_id']),
            )
    return compressed_text


def prepare_document_task(task):
    doc_id = str(task.get('document_id') or '').strip()
    job_id = str(task.get('job_id') or '').strip() or None
    force_refresh = bool(task.get('force_refresh', False))
    if not doc_id:
        logger.error('Discarding Stage 1 task without document_id: %s', task)
        return None, None

    document_state = get_document_state(doc_id)
    if not document_state:
        logger.error('Discarding Stage 1 task for missing document %s', doc_id)
        return None, None

    claim_id = str(task.get('claim_id') or document_state['claim_id'])
    s3_bucket, s3_key = normalize_s3_location(
        task.get('s3_key') or document_state['storage_key'],
        task.get('s3_bucket') or S3_BUCKET,
    )
    document = {
        'document_id': doc_id,
        'job_id': job_id,
        'claim_id': claim_id,
        's3_bucket': s3_bucket,
        's3_key': s3_key,
        'file_name': document_state['file_name'] or s3_key.rsplit('/', 1)[-1],
        'force_refresh': force_refresh,
    }

    if not s3_key:
        error_msg = 'Document storage_key is missing'
        update_extraction_job(doc_id, 'failed', error_msg, job_id)
        logger.error('%s for document %s', error_msg, doc_id)
        return None, claim_id

    if document_state['has_extraction'] and not force_refresh:
        logger.info('Skipping already-extracted document %s', doc_id)
        update_extraction_job(doc_id, 'succeeded', job_id=job_id)
        return None, claim_id

    exclusion_reason = excluded_document_reason(document['file_name'])
    if exclusion_reason:
        update_extraction_job(doc_id, 'processing', job_id=job_id)
        save_document_extraction(
            document,
            '',
            'policy-excluded',
            json.dumps({'excluded': True, 'reason': exclusion_reason}),
        )
        update_extraction_job(doc_id, 'succeeded', job_id=job_id)
        logger.info('Excluded %s from OCR: %s', document['file_name'], exclusion_reason)
        return None, claim_id

    file_size = get_file_size_s3(s3_bucket, s3_key)
    if file_size is None:
        error_msg = f'Could not determine file size for {s3_key}'
        update_extraction_job(doc_id, 'failed', error_msg, job_id)
        logger.error(error_msg)
        return None, claim_id

    document['file_size'] = int(file_size)
    document['mime_type'] = mimetypes.guess_type(document['file_name'])[0] or 'application/octet-stream'
    return document, claim_id


def group_documents_for_extraction(documents):
    return group_documents_by_size(
        documents,
        max_bytes=OPENAI_BATCH_MAX_BYTES,
        max_files=OPENAI_BATCH_MAX_FILES,
    )


def process_document_group(documents):
    for document in documents:
        update_extraction_job(document['document_id'], 'processing', job_id=document['job_id'])

    batch_results = extract_with_openai_vision_batch(documents) if len(documents) > 1 else {}
    for document in documents:
        try:
            extracted_text = batch_results.get(document['document_id'])
            extraction_model = f'{OPENAI_MODEL}-batch' if extracted_text else OPENAI_MODEL
            if len(documents) == 1:
                extracted_text = extract_with_openai_vision(
                    document['s3_bucket'],
                    document['s3_key'],
                    document['file_size'],
                )
            if not extracted_text:
                logger.info('Falling back to Textract for %s', document['s3_key'])
                extracted_text = extract_with_textract(document['s3_bucket'], document['s3_key'])
                extraction_model = 'aws_textract'
            if not extracted_text:
                raise RuntimeError('OpenAI and AWS Textract extraction failed')

            compressed_text = save_document_extraction(document, extracted_text, extraction_model)
            update_extraction_job(document['document_id'], 'succeeded', job_id=document['job_id'])
            logger.info(
                'Document %s completed: %s chars compressed to %s chars',
                document['document_id'],
                len(extracted_text),
                len(compressed_text),
            )
        except Exception as e:
            logger.error(
                'Stage 1 Error on Document %s: %s',
                document['document_id'],
                e,
                exc_info=True,
            )
            update_extraction_job(document['document_id'], 'failed', str(e), document['job_id'])


def queue_stage2_if_ready(claim_id):
    """Queue one Gemini pass only after every claim document has OCR text."""
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT COUNT(*), COUNT(*) FILTER (
                    WHERE EXISTS (
                        SELECT 1 FROM document_extractions de
                        WHERE de.document_id = cd.id
                    )
                    AND COALESCE(
                        (
                            SELECT ej.status FROM extraction_jobs ej
                            WHERE ej.document_id = cd.id
                            ORDER BY ej.queued_at DESC NULLS LAST, ej.created_at DESC
                            LIMIT 1
                        ),
                        'succeeded'
                    ) = 'succeeded'
                )
                FROM claim_documents cd
                WHERE cd.claim_id = %s
            ''', (claim_id,))
            total_documents, extracted_documents = cur.fetchone()

    if not total_documents or extracted_documents < total_documents:
        logger.info(
            'Claim %s waiting for remaining OCR documents (%s/%s)',
            claim_id,
            extracted_documents,
            total_documents,
        )
        return False

    schedule_key = f'queue:stage2_scheduled:{claim_id}'
    if not r.set(schedule_key, '1', nx=True, ex=21600):
        logger.info('Stage 2 already scheduled for claim %s', claim_id)
        return False

    r.lpush('queue:stage2_claim_reduction', json.dumps({'claim_id': claim_id}))
    logger.info('Queued one Stage 2 job for completed claim %s', claim_id)
    return True


def queue_next_stage_if_ready(claim_id):
    """Resume the first missing downstream stage for an OCR-complete claim."""
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                SELECT
                    EXISTS (
                        SELECT 1 FROM claim_structured_data csd
                        WHERE csd.claim_id = %s
                    ),
                    EXISTS (
                        SELECT 1 FROM medical_reports mr
                        WHERE mr.claim_id = %s
                    )
            ''', (claim_id, claim_id))
            has_structured_data, has_report = cur.fetchone()

    if has_report:
        return False
    if not has_structured_data:
        return queue_stage2_if_ready(claim_id)

    schedule_key = f'queue:stage3_scheduled:{claim_id}'
    if not r.set(schedule_key, '1', nx=True, ex=21600):
        return False
    r.lpush('queue:stage3_report_generation', json.dumps({'claim_id': claim_id}))
    logger.info('Recovered missing Stage 3 job for claim %s', claim_id)
    return True

def run_stage1_loop():
    logger.info(
        'Stage 1 OCR Worker Active - 60s claim debounce, %.2fMB OpenAI batches, Textract fallback',
        OPENAI_BATCH_MAX_BYTES / 1024 / 1024,
    )

    while True:
        try:
            reconcile_stale_extraction_jobs()
            tasks = pop_due_claim_tasks()
            if not tasks:
                raw_task = r.brpop(STAGE1_QUEUE, timeout=5)
                if not raw_task:
                    continue
                payload = json.loads(raw_task[1])
                tasks = payload.get('documents') if isinstance(payload, dict) else None
                if not isinstance(tasks, list):
                    tasks = [payload]

            prepared_documents = []
            claim_ids = set()
            for task in tasks:
                document, claim_id = prepare_document_task(task)
                if claim_id:
                    claim_ids.add(claim_id)
                if document:
                    prepared_documents.append(document)

            extraction_groups = group_documents_for_extraction(prepared_documents)
            logger.info(
                'Prepared %s documents as %s extraction requests',
                len(prepared_documents),
                len(extraction_groups),
            )
            for document_group in extraction_groups:
                process_document_group(document_group)

            for claim_id in claim_ids:
                queue_stage2_if_ready(claim_id)

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)
            time.sleep(5)

if __name__ == '__main__':
    run_stage1_loop()

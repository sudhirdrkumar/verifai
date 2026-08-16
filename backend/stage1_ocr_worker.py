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

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)

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
                SELECT cd.claim_id, cd.storage_key, cd.parse_status,
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
        'has_extraction': bool(row[3]) or str(row[2] or '').lower() == 'succeeded',
    }


def normalize_s3_location(storage_key, default_bucket):
    key = str(storage_key or '').strip()
    bucket = str(default_bucket or S3_BUCKET).strip() or S3_BUCKET
    if key.startswith('s3://'):
        location = key[5:].split('/', 1)
        bucket = location[0]
        key = location[1] if len(location) > 1 else ''
    return bucket, key


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

def run_stage1_loop():
    logger.info('Stage 1 OCR Worker Active - OpenAI primary, AWS Textract fallback')

    while True:
        try:
            raw_task = r.brpop('queue:stage1_ocr_extraction', timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            doc_id = str(task.get('document_id') or '').strip()
            job_id = str(task.get('job_id') or '').strip() or None
            force_refresh = bool(task.get('force_refresh', False))
            if not doc_id:
                logger.error('Discarding Stage 1 task without document_id: %s', task)
                continue

            document_state = get_document_state(doc_id)
            if not document_state:
                logger.error('Discarding Stage 1 task for missing document %s', doc_id)
                continue

            claim_id = str(task.get('claim_id') or document_state['claim_id'])
            s3_bucket, s3_key = normalize_s3_location(
                task.get('s3_key') or document_state['storage_key'],
                task.get('s3_bucket') or S3_BUCKET,
            )
            if not s3_key:
                error_msg = 'Document storage_key is missing'
                logger.error('%s for document %s', error_msg, doc_id)
                update_extraction_job(doc_id, 'failed', error_msg, job_id)
                continue

            if document_state['has_extraction'] and not force_refresh:
                logger.info('Skipping already-extracted document %s', doc_id)
                update_extraction_job(doc_id, 'succeeded', job_id=job_id)
                queue_stage2_if_ready(claim_id)
                continue

            logger.info(f'Processing Document {doc_id} from claim {claim_id}')
            update_extraction_job(doc_id, 'processing', job_id=job_id)

            try:
                # Get file size
                file_size = get_file_size_s3(s3_bucket, s3_key)
                if file_size is None:
                    raise Exception(f'Could not determine file size for {s3_key}')

                logger.info(f'File size: {file_size / 1024 / 1024:.2f}MB')

                extracted_text = extract_with_openai_vision(s3_bucket, s3_key, file_size)
                extraction_model = OPENAI_MODEL
                if not extracted_text:
                    logger.info('Falling back to Textract for %s', s3_key)
                    extracted_text = extract_with_textract(s3_bucket, s3_key)
                    extraction_model = 'aws_textract'
                if not extracted_text:
                    raise Exception('OpenAI and AWS Textract extraction failed')

                # Clean and compress
                compressed_text = clean_and_compress_ocr(extracted_text)
                logger.info(f'Extracted {len(extracted_text)} chars, compressed to {len(compressed_text)} chars')

                # Store in database
                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()

                cur.execute('''
                    INSERT INTO document_extractions (document_id, claim_id, extraction_version, model_name, extracted_entities, raw_response, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (document_id, extraction_version) DO UPDATE SET
                        model_name = EXCLUDED.model_name,
                        raw_response = EXCLUDED.raw_response,
                        created_at = NOW()
                ''', (doc_id, claim_id, 'stage1_ocr_v2', extraction_model, '{}', compressed_text))

                cur.execute('UPDATE claim_documents SET parse_status = %s WHERE id = %s', ('succeeded', doc_id))
                conn.commit()
                cur.close()
                conn.close()

                update_extraction_job(doc_id, 'succeeded', job_id=job_id)
                logger.info(f'Document {doc_id} processing completed')

                queue_stage2_if_ready(claim_id)

            except Exception as e:
                error_msg = str(e)
                logger.error(f'Stage 1 Error on Document {doc_id}: {error_msg}', exc_info=True)
                update_extraction_job(doc_id, 'failed', error_msg, job_id)

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)
            time.sleep(5)

if __name__ == '__main__':
    run_stage1_loop()

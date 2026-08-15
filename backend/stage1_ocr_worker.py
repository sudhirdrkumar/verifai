import os
import json
import time
import re
import boto3
import redis
import psycopg
import base64
import logging
import requests
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://postgres:Dhoom*2690@127.0.0.1:5432/qc_bkp_modern')
AWS_REGION = os.getenv('AWS_REGION', 'ap-south-1')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
S3_BUCKET = os.getenv('S3_BUCKET', 'rightworks-docs')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
MAX_BATCH_SIZE = 7 * 1024 * 1024  # 7MB

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)
textract = boto3.client('textract', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)

def clean_and_compress_ocr(raw_text: str) -> str:
    if not raw_text:
        return ""
    cleaned = re.sub(r'[-_|=+\\.]{4,}', ' ', raw_text)
    cleaned = re.sub(r'(?<=\s)[~`\^_\xa0](?=\s)', ' ', cleaned)
    cleaned = re.sub(r'^[^\w\s]{3,}$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'\n{2,}', '\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)
    return cleaned.strip()

def get_file_size_s3(bucket, key):
    """Get file size from S3"""
    try:
        response = s3.head_object(Bucket=bucket, Key=key)
        return response['ContentLength']
    except Exception as e:
        logger.error(f'Error getting S3 object size: {e}')
        return None

def extract_with_openai_vision(file_data_list):
    """
    Send files to OpenAI Vision for text extraction
    file_data_list: list of (filename, base64_data, mime_type)
    """
    if not OPENAI_API_KEY:
        logger.warning('OPENAI_API_KEY not set, skipping OpenAI extraction')
        return None

    try:
        # Prepare content for OpenAI
        content = [
            {"type": "text", "text": f"Extract ALL text from these medical documents. Return the complete extracted text only."}
        ]

        for filename, data_base64, mime_type in file_data_list:
            if mime_type == 'application/pdf':
                # For PDFs, use the file reference format
                content.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": data_base64
                    }
                })
            logger.info(f'Added {filename} to OpenAI extraction batch')

        # Call OpenAI GPT-4 Omni
        headers = {
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json"
        }

        payload = {
            "model": "gpt-4o",
            "messages": [
                {
                    "role": "user",
                    "content": content
                }
            ],
            "max_tokens": 4096
        }

        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=payload,
            timeout=60
        )

        if response.status_code == 200:
            result = response.json()
            extracted_text = result['choices'][0]['message']['content']
            logger.info(f'✅ OpenAI extraction successful: {len(extracted_text)} chars')
            return extracted_text
        else:
            logger.error(f'OpenAI API error: {response.status_code} - {response.text}')
            return None

    except Exception as e:
        logger.error(f'OpenAI extraction failed: {e}')
        return None

def extract_with_textract(bucket, key):
    """Fallback: Use AWS Textract for text extraction"""
    try:
        logger.info(f'Fallback to Textract for {key}')

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

def update_extraction_job(doc_id, status, error_msg=None):
    try:
        conn = psycopg.connect(DB_DSN)
        cur = conn.cursor()
        if status == 'processing':
            cur.execute('UPDATE extraction_jobs SET status = %s, started_at = NOW() WHERE document_id = %s AND status = %s',
                       ('processing', doc_id, 'queued'))
        elif status == 'succeeded':
            cur.execute('UPDATE extraction_jobs SET status = %s, finished_at = NOW() WHERE document_id = %s',
                       ('succeeded', doc_id))
        elif status == 'failed':
            cur.execute('UPDATE extraction_jobs SET status = %s, finished_at = NOW(), error_message = %s WHERE document_id = %s',
                       ('failed', error_msg, doc_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f'Failed to update extraction job: {str(e)}')

def run_stage1_loop():
    logger.info('Stage 1 OCR Worker Active - OpenAI Vision (primary) + Textract (fallback)')

    while True:
        try:
            raw_task = r.brpop('queue:stage1_ocr_extraction', timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            doc_id = task['document_id']
            claim_id = task['claim_id']
            s3_bucket = task.get('s3_bucket', S3_BUCKET)
            s3_key = task['s3_key']

            logger.info(f'Processing Document {doc_id} from claim {claim_id}')
            update_extraction_job(doc_id, 'processing')

            try:
                # Get file size
                file_size = get_file_size_s3(s3_bucket, s3_key)
                if file_size is None:
                    raise Exception(f'Could not determine file size for {s3_key}')

                logger.info(f'File size: {file_size / 1024 / 1024:.2f}MB')

                # Download and prepare file for OpenAI
                response = s3.get_object(Bucket=s3_bucket, Key=s3_key)
                file_content = response['Body'].read()
                file_base64 = base64.b64encode(file_content).decode('utf-8')

                # Try OpenAI Vision first
                extracted_text = extract_with_openai_vision([(s3_key, file_base64, 'application/pdf')])

                # Fallback to Textract if OpenAI fails
                if not extracted_text:
                    logger.warning(f'OpenAI extraction failed, falling back to Textract')
                    extracted_text = extract_with_textract(s3_bucket, s3_key)

                if not extracted_text:
                    raise Exception('Both OpenAI and Textract extraction failed')

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
                        raw_response = EXCLUDED.raw_response
                ''', (doc_id, claim_id, 'stage1_ocr_v2', 'openai_vision_textract', '{}', compressed_text))

                cur.execute('UPDATE claim_documents SET parse_status = %s WHERE id = %s', ('succeeded', doc_id))
                conn.commit()
                cur.close()
                conn.close()

                update_extraction_job(doc_id, 'succeeded')
                logger.info(f'Document {doc_id} processing completed')

                # Trigger Stage 2
                r.lpush('queue:stage2_claim_reduction', json.dumps({'claim_id': claim_id}))
                logger.info(f'Queued Stage 2 for claim {claim_id}')

            except Exception as e:
                error_msg = str(e)
                logger.error(f'Stage 1 Error on Document {doc_id}: {error_msg}', exc_info=True)
                update_extraction_job(doc_id, 'failed', error_msg)

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)
            time.sleep(5)

if __name__ == '__main__':
    run_stage1_loop()

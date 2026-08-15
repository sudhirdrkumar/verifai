import os
import json
import time
import re
import boto3
import redis
import psycopg
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://postgres:Dhoom*2690@127.0.0.1:5432/qc_bkp_modern')
AWS_REGION = os.getenv('AWS_REGION', 'ap-south-1')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
S3_BUCKET = os.getenv('S3_BUCKET', 'rightworks-docs')

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)
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

def update_extraction_job(doc_id, status, error_msg=None):
    try:
        conn = psycopg.connect(DB_DSN)
        cur = conn.cursor()
        if status == 'running' or status == 'processing':
            cur.execute('UPDATE extraction_jobs SET status = %s, started_at = NOW() WHERE document_id = %s AND status = %s', ('processing', doc_id, 'queued'))
        elif status == 'succeeded':
            cur.execute('UPDATE extraction_jobs SET status = %s, finished_at = NOW() WHERE document_id = %s', ('succeeded', doc_id))
        elif status == 'failed':
            cur.execute('UPDATE extraction_jobs SET status = %s, finished_at = NOW(), error_message = %s WHERE document_id = %s', ('failed', error_msg, doc_id))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.error(f'Failed to update extraction job: {str(e)}')

def run_stage1_loop():
    logger.info('Stage 1 OCR + Compression Worker Active')

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
                logger.info(f'Starting Textract for s3://{s3_bucket}/{s3_key}')
                resp = textract.start_document_text_detection(
                    DocumentLocation={'S3Object': {'Bucket': s3_bucket, 'Name': s3_key}}
                )
                job_id = resp['JobId']
                logger.info(f'Textract job started: {job_id}')

                while True:
                    status = textract.get_document_text_detection(JobId=job_id)['JobStatus']
                    if status == 'SUCCEEDED':
                        logger.info(f'Textract job {job_id} succeeded')
                        break
                    elif status == 'FAILED':
                        raise Exception('AWS Textract processing failed')
                    logger.info(f'Textract job {job_id} status: {status}')
                    time.sleep(5)

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

                raw_ocr_dump = '\n'.join(text_lines)
                compressed_text = clean_and_compress_ocr(raw_ocr_dump)
                logger.info(f'Extracted {len(text_lines)} lines, compressed to {len(compressed_text)} chars')

                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()
                # UPSERT: Update if exists, insert if new
                cur.execute('''
                    INSERT INTO document_extractions (document_id, claim_id, extraction_version, model_name, extracted_entities, raw_response, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (document_id, extraction_version) DO UPDATE SET
                        raw_response = EXCLUDED.raw_response
                ''', (doc_id, claim_id, 'stage1_ocr_v1', 'textract_ocr_compressed', '{}', compressed_text))

                cur.execute('UPDATE claim_documents SET parse_status = %s WHERE id = %s', ('succeeded', doc_id))
                conn.commit()
                cur.close()
                conn.close()

                update_extraction_job(doc_id, 'succeeded')
                logger.info(f'Document {doc_id} processing completed')

                # Trigger Stage 2 for this claim
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

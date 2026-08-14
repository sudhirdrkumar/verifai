import os
import json
import time
import re
import boto3
import redis
import psycopg2
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', "postgresql://admin:localpassword@127.0.0.1:5432/qc_bkp_modern_live")
AWS_REGION = os.getenv('AWS_REGION', 'ap-south-1')
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
textract = boto3.client('textract', region_name=AWS_REGION)

def clean_and_compress_ocr(raw_text: str) -> str:
    """Remove OCR artifacts and compress text before feeding to Gemini"""
    if not raw_text:
        return ""

    cleaned = re.sub(r'[-_|=+\\.]{4,}', ' ', raw_text)
    cleaned = re.sub(r'(?<=\s)[~`\^_\xa0](?=\s)', ' ', cleaned)
    cleaned = re.sub(r'^[^\w\s]{3,}$', '', cleaned, flags=re.MULTILINE)
    cleaned = re.sub(r'\n{2,}', '\n', cleaned)
    cleaned = re.sub(r'[ \t]{2,}', ' ', cleaned)

    return cleaned.strip()

def run_stage1_loop():
    logger.info("🚀 Stage 1 OCR + Compression Worker Active")

    while True:
        try:
            raw_task = r.brpoplpush("queue:stage1_ocr_extraction", "queue:stage1_active", timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task)
            doc_id = task['document_id']
            claim_id = task['claim_id']
            bucket = task['s3_bucket']
            key = task['s3_key']

            logger.info(f"Processing Document {doc_id} from claim {claim_id}")

            try:
                # Start Textract
                resp = textract.start_document_text_detection(
                    DocumentLocation={'S3Object': {'Bucket': bucket, 'Name': key}}
                )
                job_id = resp['JobId']

                # Poll for completion
                while True:
                    status = textract.get_document_text_detection(JobId=job_id)['JobStatus']
                    if status == 'SUCCEEDED':
                        break
                    elif status == 'FAILED':
                        raise Exception("AWS Textract processing failed")
                    time.sleep(5)

                # Extract text lines
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

                raw_ocr_dump = "\n".join(text_lines)
                compressed_text = clean_and_compress_ocr(raw_ocr_dump)

                # Save to database
                conn = psycopg2.connect(DB_DSN)
                cur = conn.cursor()
                cur.execute("""
                    INSERT INTO document_extractions (document_id, raw_response, model_name, created_at)
                    VALUES (%s, %s, 'textract_ocr_compressed', NOW())
                """, (doc_id, compressed_text))
                cur.execute("UPDATE claim_documents SET parse_status = 'SUCCEEDED' WHERE id = %s", (doc_id,))
                conn.commit()
                cur.close()
                conn.close()

                # Update Redis tracking
                r.hset(f"doc:state:{doc_id}", "status", "OCR_DONE")
                completed_count = r.hincrby(f"claim:tracker:{claim_id}", "completed", 1)
                total_expected = int(r.hget(f"claim:tracker:{claim_id}", "total") or 0)

                logger.info(f"✅ Document {doc_id}: {completed_count}/{total_expected} completed")

                if completed_count == total_expected and total_expected > 0:
                    logger.info(f"🔔 Claim {claim_id}: All documents processed. Triggering Stage 2")
                    r.lpush("queue:stage2_claim_reduction", json.dumps({"claim_id": claim_id}))

                r.lrem("queue:stage1_active", 1, raw_task)

            except Exception as e:
                logger.error(f"❌ Stage 1 Error on Document {doc_id}: {str(e)}")
                r.lrem("queue:stage1_active", 1, raw_task)
                r.sadd("queue:failed_stage1", raw_task)

        except Exception as e:
            logger.error(f"Worker loop error: {str(e)}")
            time.sleep(5)

if __name__ == "__main__":
    run_stage1_loop()

#!/usr/bin/env python3
"""
Dedicated Textract Extraction Worker
Processes documents using AWS Textract for fast text extraction.
Used when OpenAI Vision fails or for parallel processing.
"""

import os
import json
import redis
import psycopg
import boto3
import logging
import time
from datetime import datetime
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

AWS_REGION = os.getenv('S3_REGION', 'ap-south-1')
S3_BUCKET = os.getenv('S3_BUCKET', 'rightworks-docs')
DB_DSN = os.getenv('DATABASE_URL')
if not DB_DSN:
    pg_host = os.getenv('PG_HOST', '127.0.0.1')
    pg_port = os.getenv('PG_PORT', '5432')
    pg_user = os.getenv('PG_USER', 'postgres')
    pg_password = os.getenv('PG_PASSWORD', '')
    pg_database = os.getenv('PG_DATABASE', 'qc_bkp_modern')
    DB_DSN = f'postgresql://{pg_user}:{pg_password}@{pg_host}:{pg_port}/{pg_database}'
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))

textract = boto3.client('textract', region_name=AWS_REGION)
s3 = boto3.client('s3', region_name=AWS_REGION)
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)

logger.info('Textract Extraction Worker Started - Queue: textract_extraction')

def extract_with_textract(bucket, key):
    """Extract text using AWS Textract"""
    try:
        logger.info(f'Starting Textract extraction for {key}')

        # Start async job (no ClientRequestToken - it can cause InvalidParameterException)
        response = textract.start_document_text_detection(
            DocumentLocation={'S3Object': {'Bucket': bucket, 'Name': key}}
        )
        job_id = response['JobId']
        logger.info(f'Textract job started: {job_id}')

        # Poll for completion
        max_retries = 300  # 5 minutes
        retry_count = 0
        while retry_count < max_retries:
            status = textract.get_document_text_detection(JobId=job_id)
            job_status = status['JobStatus']

            if job_status == 'SUCCEEDED':
                logger.info(f'Textract job completed: {job_id}')
                break
            elif job_status == 'FAILED':
                raise Exception(f'Textract job failed: {job_id}')

            retry_count += 1
            time.sleep(1)

        # Extract text from pages
        extracted_text = ''
        params = {'JobId': job_id}

        while True:
            response = textract.get_document_text_detection(**params)

            for item in response.get('Blocks', []):
                if item['BlockType'] == 'LINE':
                    extracted_text += item['Text'] + '\n'

            if 'NextToken' not in response:
                break
            params['NextToken'] = response['NextToken']

        logger.info(f'Textract extracted {len(extracted_text)} chars')
        return extracted_text.strip()

    except Exception as e:
        logger.error(f'Textract extraction failed: {e}')
        return None

while True:
    conn = None
    try:
        # Pull from Textract queue
        task_json = r.brpop('queue:textract_extraction', timeout=10)

        if not task_json:
            continue

        task = json.loads(task_json[1])
        document_id = task.get('document_id')
        s3_path = task.get('s3_path')

        logger.info(f'Processing document {document_id}')

        # Extract text using Textract
        extracted_text = extract_with_textract(S3_BUCKET, s3_path)

        if extracted_text:
            try:
                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()

                cur.execute('SELECT claim_id FROM claim_documents WHERE id = %s', (document_id,))
                result = cur.fetchone()
                if not result:
                    logger.error(f'Document {document_id} not found')
                    conn.close()
                    continue

                claim_id = result[0]

                cur.execute('''
                    INSERT INTO document_extractions
                    (claim_id, document_id, raw_response, model_name, extraction_version, extracted_entities, evidence_refs, created_by, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (document_id, extraction_version) DO UPDATE
                    SET raw_response = EXCLUDED.raw_response, model_name = EXCLUDED.model_name, updated_at = NOW()
                ''', (claim_id, document_id, extracted_text, 'aws_textract', 'textract_v1', '{}', '[]', 'system'))

                conn.commit()
                logger.info(f'✅ Textract extraction saved for document {document_id}')
            except Exception as e:
                logger.error(f'Database error for {document_id}: {e}')
        else:
            logger.error(f'❌ Textract extraction failed for document {document_id}')

    except Exception as e:
        logger.error(f'Worker error: {e}')
        time.sleep(1)
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

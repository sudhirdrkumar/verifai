#!/usr/bin/env python3
"""
Reprocess claims with silent extraction failures through Textract → Stage 2 → Stage 3
"""

import os
import json
import redis
import psycopg
import logging
from dotenv import load_dotenv

env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

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

# Target claims with silent extraction failures
TARGET_CLAIMS = [
    '144393663', '144376295', '144378058', '144607276', '144390460',
    '51824395', '51466224', '51763209', '51391981', '51868809',
    '144375493', '51572953', '51204204', '49961875', '51217690',
    '144502204'
]

def get_claim_uuid(external_claim_id):
    """Get internal claim UUID from external claim ID"""
    conn = psycopg.connect(DB_DSN)
    cur = conn.cursor()

    try:
        cur.execute('SELECT id FROM claims WHERE external_claim_id = %s', (external_claim_id,))
        result = cur.fetchone()
        if result:
            return result[0]
        return None
    finally:
        conn.close()

def get_claim_documents(claim_uuid):
    """Get all documents for a claim"""
    conn = psycopg.connect(DB_DSN)
    cur = conn.cursor()

    try:
        cur.execute(
            'SELECT id, s3_key FROM claim_documents WHERE claim_id = %s ORDER BY created_at',
            (claim_uuid,)
        )
        return cur.fetchall()
    finally:
        conn.close()

def clear_failed_extraction(claim_uuid):
    """Clear failed extraction records for a claim"""
    conn = psycopg.connect(DB_DSN)
    cur = conn.cursor()

    try:
        # Delete empty extraction records
        cur.execute(
            'DELETE FROM document_extractions WHERE claim_id = %s AND (raw_response IS NULL OR raw_response = %s)',
            (claim_uuid, '')
        )
        deleted = cur.rowcount

        # Delete the extraction job
        cur.execute(
            'DELETE FROM extraction_jobs WHERE claim_id = %s',
            (claim_uuid,)
        )

        conn.commit()
        logger.info(f'✅ Cleared {deleted} empty extraction records for claim {claim_uuid}')
        return True
    except Exception as e:
        conn.rollback()
        logger.error(f'❌ Error clearing extractions for {claim_uuid}: {e}')
        return False
    finally:
        conn.close()

def queue_for_textract(claim_uuid, s3_key):
    """Queue document for Textract extraction"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    task = {
        'claim_id': claim_uuid,
        's3_path': s3_key
    }

    try:
        r.lpush('queue:textract_extraction', json.dumps(task))
        return True
    except Exception as e:
        logger.error(f'Error queuing Textract for {s3_key}: {e}')
        return False

def queue_for_stage2(claim_uuid):
    """Queue claim for Stage 2 (Gemini structuring)"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    task = {
        'claim_id': claim_uuid,
        'retry': True
    }

    try:
        r.lpush('queue:stage2_structuring', json.dumps(task))
        logger.info(f'🔄 Queued Stage 2 for claim {claim_uuid}')
        return True
    except Exception as e:
        logger.error(f'Error queuing Stage 2 for {claim_uuid}: {e}')
        return False

def queue_for_stage3(claim_uuid):
    """Queue claim for Stage 3 (Report generation)"""
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    task = {
        'claim_id': claim_uuid
    }

    try:
        r.lpush('queue:stage3_report_generation', json.dumps(task))
        logger.info(f'📋 Queued Stage 3 for claim {claim_uuid}')
        return True
    except Exception as e:
        logger.error(f'Error queuing Stage 3 for {claim_uuid}: {e}')
        return False

def main():
    logger.info('=' * 70)
    logger.info('REPROCESSING BROKEN CLAIMS - Textract → Stage 2 → Stage 3')
    logger.info('=' * 70)

    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)

    stats = {
        'total': 0,
        'processed': 0,
        'documents_queued': 0,
        'stage2_queued': 0,
        'stage3_queued': 0,
        'failed': 0
    }

    for external_claim_id in TARGET_CLAIMS:
        stats['total'] += 1

        # Get claim UUID
        claim_uuid = get_claim_uuid(external_claim_id)
        if not claim_uuid:
            logger.warning(f'❌ Claim {external_claim_id} not found')
            stats['failed'] += 1
            continue

        logger.info(f'\n📌 Processing claim {external_claim_id} ({claim_uuid})')

        # Step 1: Clear failed extraction records
        if not clear_failed_extraction(claim_uuid):
            stats['failed'] += 1
            continue

        # Step 2: Get claim documents
        documents = get_claim_documents(claim_uuid)
        if not documents:
            logger.warning(f'⚠️  No documents found for claim {external_claim_id}')
            continue

        logger.info(f'Found {len(documents)} documents')

        # Step 3: Queue for Textract extraction
        for doc_id, s3_key in documents:
            if queue_for_textract(claim_uuid, s3_key):
                stats['documents_queued'] += 1
                logger.info(f'  ✓ Queued Textract: {s3_key}')
            else:
                stats['failed'] += 1

        # Step 4: Queue for Stage 2 (Gemini structuring)
        if queue_for_stage2(claim_uuid):
            stats['stage2_queued'] += 1
        else:
            stats['failed'] += 1

        # Step 5: Queue for Stage 3 (Report generation)
        if queue_for_stage3(claim_uuid):
            stats['stage3_queued'] += 1
        else:
            stats['failed'] += 1

        stats['processed'] += 1

    # Print summary
    logger.info('\n' + '=' * 70)
    logger.info('REPROCESSING SUMMARY')
    logger.info('=' * 70)
    logger.info(f'Total claims: {stats["total"]}')
    logger.info(f'Successfully processed: {stats["processed"]}')
    logger.info(f'Documents queued for Textract: {stats["documents_queued"]}')
    logger.info(f'Claims queued for Stage 2: {stats["stage2_queued"]}')
    logger.info(f'Claims queued for Stage 3: {stats["stage3_queued"]}')
    logger.info(f'Failed: {stats["failed"]}')
    logger.info('=' * 70)
    logger.info('\n✅ Reprocessing initiated! Claims will flow through pipeline.')
    logger.info('   Monitor logs: tail -f /var/log/textract_extraction_worker.log')
    logger.info('                 tail -f /var/log/stage2_reduction_worker.log')
    logger.info('                 tail -f /var/log/stage3_report_worker.log')

if __name__ == '__main__':
    main()

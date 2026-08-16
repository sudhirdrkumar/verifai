#!/usr/bin/env python3
"""
Re-queue all extracted claims for Stage 2 (Gemini structuring) and Stage 3 (report generation)
with the improved extraction logic
"""

import os
import json
import redis
import psycopg
import logging
from datetime import datetime
from dotenv import load_dotenv

# Load .env
env_path = os.path.join(os.path.dirname(__file__), '..', '.env')
load_dotenv(env_path)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://verifai:yYv5Ny7outZG7XKrgEJ8JUxJ@127.0.0.1:5432/qc_bkp_modern')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)
conn = psycopg.connect(DB_DSN)
cur = conn.cursor()

print("=" * 70)
print("REPROCESSING EXTRACTED CLAIMS FOR STAGE 2 & 3")
print("=" * 70)
print()

# Find all claims with document extractions (Stage 1 completed)
cur.execute('''
    SELECT c.id, c.external_claim_id,
           COUNT(DISTINCT cd.id) as doc_count,
           COUNT(DISTINCT de.document_id) as extracted_count,
           MAX(c.created_at) as created_at
    FROM claims c
    INNER JOIN claim_documents cd ON c.id = cd.claim_id
    INNER JOIN document_extractions de ON cd.id = de.document_id
    WHERE de.raw_response IS NOT NULL AND de.raw_response != ''
    GROUP BY c.id, c.external_claim_id
    ORDER BY MAX(c.created_at) DESC
    LIMIT 1000
''')

claims = cur.fetchall()
print(f"📊 Found {len(claims)} claims with extracted OCR")
print()

stage2_count = 0
stage3_count = 0
skipped_count = 0

for claim_id, external_id, doc_count, extracted_count, created_at in claims:
    try:
        # Check if already has recent structured data
        cur.execute(
            'SELECT id, created_at FROM claim_structured_data WHERE claim_id = %s ORDER BY created_at DESC LIMIT 1',
            (claim_id,)
        )
        structured = cur.fetchone()

        # Always re-queue for stage 2 to use new extraction logic
        task = {'claim_id': str(claim_id)}
        r.lpush('queue:stage2_claim_reduction', json.dumps(task))
        stage2_count += 1

        # Delete old structured data to force reprocessing
        if structured:
            cur.execute('DELETE FROM claim_structured_data WHERE claim_id = %s', (claim_id,))
            logger.info(f'🔄 Requeued Stage 2 for claim {external_id} (had old structured data)')
        else:
            logger.info(f'✅ Queued Stage 2 for claim {external_id}')

    except Exception as e:
        logger.error(f'Error processing claim {external_id}: {e}')
        skipped_count += 1
        continue

conn.commit()
cur.close()
conn.close()

print()
print("=" * 70)
print(f"✅ STAGE 2 REQUEUED: {stage2_count} claims")
print(f"⏭️  STAGE 3 WILL AUTO-QUEUE after Stage 2 completes")
print(f"⏭️  SKIPPED: {skipped_count} claims")
print("=" * 70)
print()
print("📝 Monitor with:")
print("   pm2 logs stage2-reducer --lines 50")
print("   pm2 logs stage3-report --lines 50")
print()

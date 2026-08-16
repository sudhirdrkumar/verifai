#!/usr/bin/env python3
"""
SAFE REPROCESSING FILTER
Excludes completed and withdrawn cases from future processing.
Use this filter in all reprocessing scripts to avoid processing cases that should be skipped.
"""

import os
import json
import redis
import psycopg
import logging
from datetime import datetime
from dotenv import load_dotenv

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
print("SAFE REPROCESSING - EXCLUDE COMPLETED & WITHDRAWN CASES")
print("=" * 70)
print()

# Find all INCOMPLETE cases (excluding completed and withdrawn)
cur.execute('''
    SELECT c.id, c.external_claim_id,
           COUNT(DISTINCT cd.id) as doc_count
    FROM claims c
    INNER JOIN claim_documents cd ON c.id = cd.claim_id
    INNER JOIN document_extractions de ON cd.id = de.document_id
    WHERE de.raw_response IS NOT NULL AND de.raw_response != ''
    AND c.status NOT IN ('completed', 'withdrawn')
    AND NOT EXISTS (
        SELECT 1 FROM report_versions rv
        WHERE rv.claim_id = c.id
        AND rv.created_by = 'system-auto-generated'
    )
    GROUP BY c.id, c.external_claim_id
    ORDER BY c.created_at DESC
    LIMIT 1000
''')

claims = cur.fetchall()
print(f"📊 Found {len(claims)} SAFE cases to process")
print(f"   (Excluding: completed, withdrawn, already-reported)")
print()

stage2_count = 0
for claim_id, external_id, doc_count in claims:
    try:
        # Delete old structured data to force reprocessing with new logic
        cur.execute('DELETE FROM claim_structured_data WHERE claim_id = %s', (claim_id,))

        # Queue for Stage 2
        task = {'claim_id': str(claim_id)}
        r.lpush('queue:stage2_claim_reduction', json.dumps(task))
        stage2_count += 1

        logger.info(f'✅ Queued Stage 2 for claim {external_id}')

    except Exception as e:
        logger.error(f'Error processing claim {external_id}: {e}')
        continue

conn.commit()
cur.close()
conn.close()

print()
print("=" * 70)
print(f"✅ QUEUED FOR REPROCESSING: {stage2_count} safe cases")
print("=" * 70)
print()
print("FILTER APPLIED:")
print("  ❌ Excluded: status = 'completed'")
print("  ❌ Excluded: status = 'withdrawn'")
print("  ❌ Excluded: already have auto-generated reports")
print("  ✅ Only processing: in_review cases without reports")
print()

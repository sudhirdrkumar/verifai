import json
import logging

import psycopg
import redis

from stage2_reduction_worker import (
    DB_DSN,
    REDIS_HOST,
    REDIS_PORT,
    auto_generate_report,
)


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
r = redis.Redis(
    host=REDIS_HOST,
    port=REDIS_PORT,
    decode_responses=True,
    socket_keepalive=True,
)


def run_stage3_loop():
    logger.info('Stage 3 Report Worker Active')
    while True:
        try:
            raw_task = r.brpop('queue:stage3_report_generation', timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            claim_id = str(task.get('claim_id') or '').strip()
            if not claim_id:
                logger.error('Discarding Stage 3 task without claim_id: %s', task)
                continue

            try:
                with psycopg.connect(DB_DSN) as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            'SELECT raw_payload FROM claim_structured_data WHERE claim_id = %s',
                            (claim_id,),
                        )
                        row = cur.fetchone()
                        if not row or not row[0]:
                            raise ValueError('structured claim data is missing')
                        structured_json = row[0]
                        if isinstance(structured_json, str):
                            structured_json = json.loads(structured_json)
                        if not auto_generate_report(cur, claim_id, structured_json):
                            raise RuntimeError('report generation failed')
                    conn.commit()
                logger.info('Report saved and versioned for claim %s', claim_id)
            except Exception:
                logger.exception('Stage 3 failed for claim %s', claim_id)
            finally:
                r.delete(f'queue:stage3_scheduled:{claim_id}')

        except redis.exceptions.TimeoutError:
            continue
        except Exception:
            logger.exception('Stage 3 worker loop error')


if __name__ == '__main__':
    run_stage3_loop()

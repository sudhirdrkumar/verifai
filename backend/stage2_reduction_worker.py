import os
import json
import redis
import psycopg
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://postgres:Dhoom*2690@127.0.0.1:5432/qc_bkp_modern')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))

r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)


def generate_text_report(structured_data: dict) -> str:
    """Generate plain text medical report."""
    hospital = structured_data.get('hospital_name', 'Not Specified')
    doctor = structured_data.get('treating_doctor', 'Not Specified')
    diagnosis = structured_data.get('diagnosis', 'Not Specified')
    complaints = structured_data.get('complaints', 'Not Specified')
    medicine = structured_data.get('medicine_used', 'Not Specified')
    claim_amount = structured_data.get('claim_amount', 'Not Specified')
    conclusion = structured_data.get('conclusion', 'Not Specified')

    text_report = f"""
{'='*70}
MEDICAL CLAIM REPORT - AUTO GENERATED
{'='*70}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Report Type: AI-Generated (Gemini 3.5 Flash)
{'='*70}

FACILITY & PROVIDER INFORMATION
{'-'*70}
Hospital:          {hospital}
Treating Doctor:   {doctor}

CLINICAL DETAILS
{'-'*70}
Diagnosis:         {diagnosis}
Complaints:        {complaints}
Medications:       {medicine}

CLAIM DETAILS
{'-'*70}
Claim Amount:      ₹{claim_amount}

AI CONCLUSION
{'-'*70}
{conclusion}

{'='*70}
Note: This report was auto-generated using AI analysis of OCR-extracted
medical documents. Doctor review is required before final approval.
{'='*70}
    """.strip()

    return text_report


def auto_generate_report(cur, claim_id: str, structured_json: dict):
    """Auto-generate medical report for claim."""
    try:
        structured_data = {
            'hospital_name': structured_json.get('hospital_name', 'Not Specified'),
            'treating_doctor': structured_json.get('treating_doctor', 'Not Specified'),
            'diagnosis': structured_json.get('diagnosis', 'Not Specified'),
            'complaints': structured_json.get('complaints', 'Not Specified'),
            'medicine_used': structured_json.get('medicine_used', 'Not Specified'),
            'claim_amount': structured_json.get('claim_amount', 'Not Specified'),
            'conclusion': structured_json.get('conclusion', 'Not Specified'),
        }

        report_text = generate_text_report(structured_data)

        # Insert report into database
        cur.execute('''
            INSERT INTO medical_reports (
                claim_id, hospital_name, treating_doctor, diagnosis,
                complaints, medicine_used, claim_amount, conclusion,
                report_text, status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (claim_id) DO UPDATE SET
                report_text = EXCLUDED.report_text,
                status = 'generated',
                updated_at = NOW()
        ''', (
            claim_id,
            structured_data['hospital_name'],
            structured_data['treating_doctor'],
            structured_data['diagnosis'],
            structured_data['complaints'],
            structured_data['medicine_used'],
            structured_data['claim_amount'],
            structured_data['conclusion'],
            report_text,
            'generated'
        ))

        logger.info(f'📄 Auto-report generated for claim {claim_id}')
        return True

    except Exception as e:
        logger.error(f'Error auto-generating report: {str(e)}', exc_info=True)
        return False


def run_stage2_loop():
    logger.info('Stage 2: Auto-Report Trigger (no LLM processing)')

    while True:
        try:
            raw_task = r.brpop('queue:stage2_claim_reduction', timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            claim_id = task['claim_id']

            logger.info(f'Processing Claim {claim_id}: Triggering Stage 3 auto-report')

            try:
                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()

                # Check if structured data exists for this claim
                cur.execute('SELECT id FROM claim_structured_data WHERE claim_id = %s LIMIT 1', (claim_id,))
                structured_row = cur.fetchone()

                if structured_row:
                    logger.info(f'Found structured data for claim {claim_id}, generating auto-report')
                    # Get structured data for report generation
                    cur.execute('''
                        SELECT company_name, hospital_name, treating_doctor,
                               doa, dod, diagnosis, complaints, medicine_used,
                               claim_amount, conclusion
                        FROM claim_structured_data
                        WHERE claim_id = %s
                        LIMIT 1
                    ''', (claim_id,))

                    data_row = cur.fetchone()
                    if data_row:
                        structured_json = {
                            'company_name': data_row[0] or '',
                            'hospital_name': data_row[1] or '',
                            'treating_doctor': data_row[2] or '',
                            'doa': data_row[3] or '',
                            'dod': data_row[4] or '',
                            'diagnosis': data_row[5] or '',
                            'complaints': data_row[6] or '',
                            'medicine_used': data_row[7] or '',
                            'claim_amount': data_row[8] or '',
                            'conclusion': data_row[9] or '',
                        }
                        # AUTO-GENERATE REPORT - STAGE 3
                        auto_generate_report(cur, claim_id, structured_json)
                        conn.commit()
                        logger.info(f'✅ Claim {claim_id} auto-report generated')
                else:
                    logger.warning(f'No structured data found for claim {claim_id}, skipping report generation')

                cur.close()
                conn.close()

            except Exception as e:
                logger.error(f'Stage 2 Error on Claim {claim_id}: {str(e)}', exc_info=True)

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)


if __name__ == '__main__':
    run_stage2_loop()

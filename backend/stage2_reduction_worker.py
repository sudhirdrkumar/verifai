import os
import json
import redis
import psycopg
import logging
from datetime import datetime
from dotenv import load_dotenv
import google.generativeai as genai

# Load .env file from parent directory
import sys
from pathlib import Path
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

DB_DSN = os.getenv('DATABASE_URL', 'postgresql://verifai:yYv5Ny7outZG7XKrgEJ8JUxJ@127.0.0.1:5432/qc_bkp_modern')
REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
GEMINI_MODEL = os.getenv('GEMINI_FLASH_MODEL', 'gemini-3-flash-preview')

if not GEMINI_API_KEY:
    raise ValueError('GEMINI_API_KEY environment variable not set')

genai.configure(api_key=GEMINI_API_KEY)
r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True, socket_keepalive=True)

def extract_structured_data_gemini(ocr_text: str, claim_id: str) -> dict:
    """Extract structured medical data from OCR text using Gemini"""
    try:
        prompt = f'''Extract medical claim data from the OCR text and return ONLY valid JSON (no markdown, no extra text):
{{
  "company_name": "insurance company name",
  "hospital_name": "hospital name",
  "treating_doctor": "doctor name or '-' if not found",
  "treating_doctor_registration_number": "registration number or '-'",
  "doa": "date of admission (DD-MM-YYYY)",
  "dod": "date of discharge (DD-MM-YYYY)",
  "diagnosis": "main diagnosis or chief complaint",
  "complaints": "patient complaints/symptoms",
  "findings": "clinical findings",
  "medicine_used": "list of medicines used, comma-separated",
  "high_end_antibiotic_for_rejection": "any high-end antibiotics if mentioned",
  "deranged_investigation": "any deranged investigation values",
  "investigation_finding_in_details": "detailed investigation findings",
  "claim_amount": "claimed amount or total bill amount",
  "conclusion": "conclusion or recommendation",
  "recommendation": "final recommendation (APPROVE/REJECT/QUERY)"
}}

OCR TEXT:
{ocr_text}

Return ONLY the JSON object, nothing else.'''

        model = genai.GenerativeModel(GEMINI_MODEL)
        response = model.generate_content(prompt)

        response_text = response.text.strip()

        # Clean up markdown code blocks if present
        if response_text.startswith('```'):
            response_text = response_text.split('```')[1]
            if response_text.startswith('json'):
                response_text = response_text[4:]
            response_text = response_text.strip()

        structured_data = json.loads(response_text)
        logger.info(f'✅ {GEMINI_MODEL} structured extraction for claim {claim_id} successful')
        return structured_data

    except json.JSONDecodeError as e:
        logger.error(f'Failed to parse Gemini JSON response: {e}')
        logger.error(f'Response was: {response_text[:200]}')
        return {}
    except Exception as e:
        logger.error(f'Gemini extraction failed: {e}', exc_info=True)
        return {}

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

        text_report = f"""
{'='*70}
MEDICAL CLAIM REPORT - AUTO GENERATED
{'='*70}
Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Report Type: AI-Generated ({GEMINI_MODEL})
{'='*70}

FACILITY & PROVIDER INFORMATION
{'-'*70}
Hospital:          {structured_data['hospital_name']}
Treating Doctor:   {structured_data['treating_doctor']}

CLINICAL DETAILS
{'-'*70}
Diagnosis:         {structured_data['diagnosis']}
Complaints:        {structured_data['complaints']}
Medications:       {structured_data['medicine_used']}

CLAIM DETAILS
{'-'*70}
Claim Amount:      ₹{structured_data['claim_amount']}

CONCLUSION
{'-'*70}
{structured_data['conclusion']}

{'='*70}
Note: This report was auto-generated using AI analysis of OCR-extracted
medical documents. Doctor review is required before final approval.
{'='*70}
        """.strip()

        # Convert to HTML for display
        html_report = f"""<h1 class="title">MEDICAL CLAIM REPORT - AUTO GENERATED</h1>
<div class="meta">Generated: {datetime.now().strftime('%m/%d/%Y, %I:%M:%S %p')} | Report Type: AI-Generated ({GEMINI_MODEL})</div>
<div class="content">
<h2>FACILITY & PROVIDER INFORMATION</h2>
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 5px;"><b>Hospital:</b></td><td style="padding: 5px;">{structured_data['hospital_name']}</td></tr>
<tr><td style="padding: 5px;"><b>Treating Doctor:</b></td><td style="padding: 5px;">{structured_data['treating_doctor']}</td></tr>
</table>

<h2>CLINICAL DETAILS</h2>
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 5px;"><b>Diagnosis:</b></td><td style="padding: 5px;">{structured_data['diagnosis']}</td></tr>
<tr><td style="padding: 5px;"><b>Complaints:</b></td><td style="padding: 5px;">{structured_data['complaints']}</td></tr>
<tr><td style="padding: 5px;"><b>Medications:</b></td><td style="padding: 5px;">{structured_data['medicine_used']}</td></tr>
</table>

<h2>CLAIM DETAILS</h2>
<table style="width: 100%; border-collapse: collapse;">
<tr><td style="padding: 5px;"><b>Claim Amount:</b></td><td style="padding: 5px;">₹{structured_data['claim_amount']}</td></tr>
</table>

<h2>CONCLUSION</h2>
<p>{structured_data['conclusion']}</p>

<hr style="margin-top: 20px;">
<p style="font-size: 12px; color: #666;">Note: This report was auto-generated using AI analysis of OCR-extracted medical documents. Doctor review is required before final approval.</p>
</div>"""

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
            text_report,
            'generated'
        ))

        # Also save to report_versions so it shows as latest report in the system
        try:
            cur.execute('''
                INSERT INTO report_versions (
                    claim_id, version_no, report_markdown, report_status, created_by, created_at
                )
                VALUES (
                    %s,
                    COALESCE((SELECT MAX(version_no) FROM report_versions WHERE claim_id = %s), 0) + 1,
                    %s,
                    'completed',
                    'system-auto-generated',
                    NOW()
                )
            ''', (claim_id, claim_id, html_report))
        except Exception as e:
            logger.warning(f'Could not save to report_versions: {str(e)}')

        logger.info(f'📄 Auto-report generated for claim {claim_id}')
        return True

    except Exception as e:
        logger.error(f'Error auto-generating report: {str(e)}', exc_info=True)
        return False

def run_stage2_loop():
    logger.info(f'Stage 2 Structuring Worker Active ({GEMINI_MODEL})')

    while True:
        try:
            raw_task = r.brpop('queue:stage2_claim_reduction', timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            claim_id = task['claim_id']

            logger.info(f'Processing Claim {claim_id}: Extracting structured data with {GEMINI_MODEL}')

            try:
                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()

                # Get OCR text from ALL documents in Stage 1 and combine
                cur.execute('''
                    SELECT de.raw_response, cd.file_name
                    FROM document_extractions de
                    JOIN claim_documents cd ON de.document_id = cd.id
                    WHERE cd.claim_id = %s
                    ORDER BY de.created_at ASC
                ''', (claim_id,))

                ocr_rows = cur.fetchall()
                if not ocr_rows:
                    logger.warning(f'No OCR text found for claim {claim_id}')
                    cur.close()
                    conn.close()
                    continue

                # Combine OCR from all documents
                combined_texts = []
                for row in ocr_rows:
                    ocr_data, filename = row
                    if ocr_data:
                        combined_texts.append(f'--- Document: {filename} ---\n{ocr_data}')

                ocr_text = '\n\n'.join(combined_texts)
                logger.info(f'Combined OCR from {len(ocr_rows)} documents: {len(ocr_text)} chars for claim {claim_id}')

                # Extract structured data with Gemini
                structured_json = extract_structured_data_gemini(ocr_text, claim_id)

                if not structured_json:
                    logger.warning(f'Failed to extract structured data for claim {claim_id}')
                    cur.close()
                    conn.close()
                    continue

                # Get external claim ID
                cur.execute('SELECT external_claim_id FROM claims WHERE id = %s', (claim_id,))
                external_id_row = cur.fetchone()
                external_id = external_id_row[0] if external_id_row else ''

                # Store structured data in database
                cur.execute('''
                    INSERT INTO claim_structured_data (
                        claim_id, external_claim_id, company_name, hospital_name, treating_doctor,
                        treating_doctor_registration_number, doa, dod, diagnosis, complaints,
                        findings, medicine_used, high_end_antibiotic_for_rejection,
                        deranged_investigation, investigation_finding_in_details,
                        claim_amount, conclusion, recommendation,
                        raw_payload, source, created_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                    ON CONFLICT (claim_id) DO UPDATE SET
                        company_name = EXCLUDED.company_name,
                        hospital_name = EXCLUDED.hospital_name,
                        treating_doctor = EXCLUDED.treating_doctor,
                        treating_doctor_registration_number = EXCLUDED.treating_doctor_registration_number,
                        doa = EXCLUDED.doa,
                        dod = EXCLUDED.dod,
                        diagnosis = EXCLUDED.diagnosis,
                        complaints = EXCLUDED.complaints,
                        findings = EXCLUDED.findings,
                        medicine_used = EXCLUDED.medicine_used,
                        high_end_antibiotic_for_rejection = EXCLUDED.high_end_antibiotic_for_rejection,
                        deranged_investigation = EXCLUDED.deranged_investigation,
                        investigation_finding_in_details = EXCLUDED.investigation_finding_in_details,
                        claim_amount = EXCLUDED.claim_amount,
                        conclusion = EXCLUDED.conclusion,
                        recommendation = EXCLUDED.recommendation,
                        raw_payload = EXCLUDED.raw_payload,
                        updated_at = NOW()
                ''', (
                    claim_id, external_id,
                    structured_json.get('company_name', ''),
                    structured_json.get('hospital_name', ''),
                    structured_json.get('treating_doctor', ''),
                    structured_json.get('treating_doctor_registration_number', ''),
                    structured_json.get('doa', ''),
                    structured_json.get('dod', ''),
                    structured_json.get('diagnosis', ''),
                    structured_json.get('complaints', ''),
                    structured_json.get('findings', ''),
                    structured_json.get('medicine_used', ''),
                    structured_json.get('high_end_antibiotic_for_rejection', ''),
                    structured_json.get('deranged_investigation', ''),
                    structured_json.get('investigation_finding_in_details', ''),
                    structured_json.get('claim_amount', ''),
                    structured_json.get('conclusion', ''),
                    structured_json.get('recommendation', ''),
                    json.dumps(structured_json),
                    GEMINI_MODEL
                ))

                conn.commit()

                # AUTO-GENERATE REPORT - STAGE 3
                auto_generate_report(cur, claim_id, structured_json)
                conn.commit()

                cur.close()
                conn.close()

                logger.info(f'✅ Claim {claim_id} structured + report generated by {GEMINI_MODEL}')

            except Exception as e:
                logger.error(f'Stage 2 Error on Claim {claim_id}: {str(e)}', exc_info=True)

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)

if __name__ == '__main__':
    run_stage2_loop()

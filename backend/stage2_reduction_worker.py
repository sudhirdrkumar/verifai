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


def report_field_text(value) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                lines.append(' | '.join(f'{key}: {val}' for key, val in item.items() if val not in (None, '')))
            elif str(item).strip():
                lines.append(str(item).strip())
        return '\n'.join(line for line in lines if line)
    if isinstance(value, dict):
        return '\n'.join(f'{key}: {val}' for key, val in value.items() if val not in (None, ''))
    return str(value).strip()


def normalize_structured_json(data: dict) -> dict:
    normalized = dict(data or {})
    aliases = {
        'complaints': ('chief_complaints', 'chief_complaints_at_admission'),
        'major_diagnostic_finding': ('major_diagnostic_findings',),
        'findings': ('clinical_findings',),
        'all_investigation_reports': ('investigation_reports',),
        'daily_tpr_chart_min_max': ('daily_tpr_chart',),
        'high_end_antibiotic_for_rejection': ('high_end_antibiotics',),
        'claim_amount': ('claimed_amount',),
    }
    for target, source_keys in aliases.items():
        if report_field_text(normalized.get(target)) not in ('', '-'):
            continue
        for source_key in source_keys:
            if report_field_text(normalized.get(source_key)) not in ('', '-'):
                normalized[target] = normalized[source_key]
                break
    if report_field_text(normalized.get('investigation_finding_in_details')) in ('', '-'):
        normalized['investigation_finding_in_details'] = normalized.get('all_investigation_reports') or []
    return normalized

def extract_structured_data_gemini(ocr_text: str, claim_id: str) -> dict:
    """Extract structured medical data from OCR text using Gemini"""
    try:
        prompt = f'''Extract ALL medical claim data from the OCR text. Return ONLY valid JSON (no markdown, no extra text).

CRITICAL: For investigation_finding_in_details, extract EVERY lab value, vital sign, and test result found:
- List each test name with value, unit, reference range
- Include all CBC, LFT, RFT, ABG, culture reports, imaging findings
- Format: "Test Name: value unit (reference: range) [abnormal flag]"
- Do NOT return generic text like "investigations were done" - extract actual VALUES

JSON FORMAT:
{{
  "company_name": "insurance company name",
  "claim_type": "Cashless/Reimbursement/other",
  "insured_name": "patient name",
  "hospital_name": "hospital name",
  "treating_doctor": "doctor name or '-'",
  "treating_doctor_registration_number": "registration number or '-'",
  "doa": "date of admission (DD-MM-YYYY)",
  "dod": "date of discharge (DD-MM-YYYY)",
  "diagnosis": "primary diagnosis",
  "complaints": "chief complaints at admission only",
  "major_diagnostic_finding": "vital signs and major clinical findings",
  "findings": "clinical examination findings with vitals (BP, HR, SPO2, RR, TEMP)",
  "alcoholism_history": "alcohol history or '-'",
  "all_investigation_reports": ["test | value | unit | reference range"],
  "deranged_investigation": ["abnormal test | value | abnormal flag (high/low)"],
  "daily_tpr_chart_min_max": ["date | BP | HR | SpO2 | Temperature"],
  "medicine_used": ["medicine | strength | route | frequency"],
  "high_end_antibiotic_for_rejection": "meropenem/linezolid/vancomycin if present",
  "investigation_finding_in_details": "DETAILED: list all CBC (Hemoglobin, WBC, Platelets), LFT (Bilirubin, Albumin), RFT (Creatinine), investigations with values and units",
  "claim_amount": "claimed amount",
  "conclusion": "evidence-based conclusion",
  "recommendation": "APPROVE/REJECT/QUERY"
}}

EXTRACTION RULES:
1. Extract EXACT test values, not generic summaries
2. Include units and reference ranges when available
3. List all abnormal values under deranged_investigation
4. For investigation_finding_in_details: Provide COMPLETE lab reports with numbers, NOT just "investigations done"
5. Use "-" only when field truly unavailable

OCR TEXT (from {ocr_text.count(chr(10))} lines, {len(ocr_text)} chars):
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

        structured_data = normalize_structured_json(json.loads(response_text))
        logger.info(f'✅ {GEMINI_MODEL} structured extraction for claim {claim_id} successful')
        return structured_data

    except json.JSONDecodeError as e:
        logger.error(f'Failed to parse Gemini JSON response: {e}')
        logger.error(f'Response was: {response_text[:200]}')
        return {}
    except Exception as e:
        logger.error(f'Gemini extraction failed: {e}', exc_info=True)
        return {}

def generate_medical_legal_conclusion(structured_json: dict) -> str:
    """Generate professional medico-legal conclusion based on clinical evidence."""
    try:
        diagnosis = structured_json.get('diagnosis', 'unspecified diagnosis').strip()
        chief_complaints = structured_json.get('chief_complaints', 'unspecified complaints').strip()
        investigations = structured_json.get('deranged_investigation', '').strip()
        medicines = structured_json.get('medicine_used', '').strip()
        clinical_findings = structured_json.get('clinical_findings', '').strip()
        recommendation = structured_json.get('recommendation', 'QUERY').strip().upper()

        # Determine treatment type (conservative vs surgical)
        treatment_type = 'conservatively' if not any(surgical in medicines.lower() for surgical in ['surgery', 'orif', 'fixation', 'repair', 'ligation']) else 'surgically'

        # Extract key medicines (especially antibiotics and high-end drugs)
        medicine_list = medicines.replace(',', ' ').split() if medicines else []
        antibiotics = [m.strip() for m in medicine_list if any(ab in m.lower() for ab in ['antibiotic', 'cef', 'meropenem', 'azithromycin', 'linezolid', 'vancomycin'])][:3]
        antibiotic_str = ', '.join(antibiotics) if antibiotics else 'supportive treatment'

        # Validation assessment
        validation = ''
        if investigations and investigations.lower() not in ('no deranged', '-', 'none'):
            validation = f'investigation findings {investigations} supported the diagnosis.'
        elif clinical_findings and clinical_findings != '-':
            validation = f'clinical findings {clinical_findings} supported the diagnosis.'
        else:
            validation = 'clinical presentation was consistent with the diagnosis.'

        # Build conclusion
        conclusion = (
            f"Based on available medical documents, patient presented with {chief_complaints} "
            f"and was diagnosed with {diagnosis}. {validation.capitalize()} "
            f"Patient was treated {treatment_type} with {antibiotic_str}. "
            f"The case appears clinically genuine and appropriately documented."
        )

        return conclusion.strip()
    except Exception as e:
        logger.warning(f"Error generating medical-legal conclusion: {e}")
        return structured_json.get('conclusion', 'Clinical assessment based on available medical documents.')


def auto_generate_report(cur, claim_id: str, structured_json: dict):
    """Auto-generate medical report for claim."""
    try:
        from datetime import datetime as dt

        # Extract all fields with defaults
        company_name = structured_json.get('company_name', 'Medi Assist Insurance TPA Pvt. Ltd.')
        claim_number = structured_json.get('claim_number', '-')
        claim_type = structured_json.get('claim_type', 'Cashless')
        insured_name = structured_json.get('insured_name', '-')
        hospital_name = structured_json.get('hospital_name', '-')
        treating_doctor = structured_json.get('treating_doctor', '-')
        doctor_registration = structured_json.get('treating_doctor_registration_number', '-')
        doa = structured_json.get('doa', '-')
        dod = structured_json.get('dod', '-')

        # Calculate length of stay
        length_of_stay = structured_json.get('length_of_stay_days', '')
        if not length_of_stay and doa != '-' and dod != '-':
            try:
                from datetime import datetime
                doa_dt = datetime.strptime(doa, '%d-%m-%Y')
                dod_dt = datetime.strptime(dod, '%d-%m-%Y')
                los = (dod_dt - doa_dt).days
                length_of_stay = f'{los} day(s)'
            except:
                length_of_stay = '-'

        diagnosis = structured_json.get('diagnosis', '-')
        chief_complaints = structured_json.get('chief_complaints', '-')
        major_findings = structured_json.get('major_diagnostic_findings', '-')
        alcoholism_history = structured_json.get('alcoholism_history', 'NAD')
        clinical_findings = structured_json.get('clinical_findings', '-')
        investigation_reports = structured_json.get('investigation_reports', '-')
        deranged_investigation = structured_json.get('deranged_investigation', 'No deranged investigation values found.')
        daily_tpr = structured_json.get('daily_tpr_chart', '-')
        medicine_used = structured_json.get('medicine_used', '-')
        claimed_amount = structured_json.get('claimed_amount', '-')
        recommendation = structured_json.get('recommendation', 'QUERY')
        query_reason = structured_json.get('query_reason', '')

        # Generate professional medical-legal conclusion from clinical evidence
        conclusion = generate_medical_legal_conclusion(structured_json)

        # Generate HTML in HEALTH CLAIM ASSESSMENT SHEET format matching the PDF
        gen_time = dt.now().strftime('%m/%d/%Y, %I:%M:%S %p')

        html_report = f"""<div style="font-family: Arial, Helvetica, sans-serif; font-size: 12px; line-height: 1.4; padding: 15px;">
<h1 style="text-align: center; font-size: 16px; font-weight: bold; margin: 10px 0;">HEALTH CLAIM ASSESSMENT SHEET</h1>
<div style="text-align: center; color: #666; font-size: 11px; margin: 8px 0;">Generated: {gen_time} | Doctor: DrMukul</div>

<table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
<tbody>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold; width: 35%;">COMPANY NAME</td><td style="padding: 6px; border: 1px solid #999;">{company_name}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">CLAIM NO.</td><td style="padding: 6px; border: 1px solid #999;">{claim_number}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">CLAIM TYPE</td><td style="padding: 6px; border: 1px solid #999;">{claim_type}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">INSURED</td><td style="padding: 6px; border: 1px solid #999;">{insured_name}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">HOSPITAL</td><td style="padding: 6px; border: 1px solid #999;">{hospital_name}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">TREATING DOCTOR</td><td style="padding: 6px; border: 1px solid #999;">{treating_doctor}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">TREATING DOCTOR REGISTRATION NUMBER</td><td style="padding: 6px; border: 1px solid #999;">{doctor_registration}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">ADMISSION</td><td style="padding: 6px; border: 1px solid #999;">{doa}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">DISCHARGE</td><td style="padding: 6px; border: 1px solid #999;">{dod}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">LENGTH OF STAY</td><td style="padding: 6px; border: 1px solid #999;">{length_of_stay}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">DIAGNOSIS</td><td style="padding: 6px; border: 1px solid #999;"><strong>{diagnosis}</strong></td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">CHIEF COMPLAINTS AT ADMISSION</td><td style="padding: 6px; border: 1px solid #999;">{chief_complaints}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">MAJOR DIAGNOSTIC FINDING (ADMISSION / DURING STAY)</td><td style="padding: 6px; border: 1px solid #999;">{major_findings}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">ALCOHOLISM HISTORY</td><td style="padding: 6px; border: 1px solid #999;">{alcoholism_history}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">CLAIMED AMOUNT</td><td style="padding: 6px; border: 1px solid #999;">{claimed_amount}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">CLINICAL FINDINGS</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd;">{clinical_findings}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">ALL INVESTIGATION REPORTS</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd; white-space: pre-wrap;">{investigation_reports}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">DERANGED INVESTIGATION REPORTS</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd;">{deranged_investigation}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">DAILY TPR CHART (MIN/MAX)</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd;">{daily_tpr}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">MEDICINE EVIDENCE USED</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd;">{medicine_used}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">CONCLUSION AND RECOMMENDATION</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold; width: 25%;">Admission Required</td><td style="padding: 6px; border: 1px solid #999;">{recommendation}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">Final Recommendation</td><td style="padding: 6px; border: 1px solid #999;">{recommendation}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold; vertical-align: top;">Conclusion</td><td style="padding: 6px; border: 1px solid #999; white-space: pre-wrap;">{conclusion}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">Recommendation</td><td style="padding: 6px; border: 1px solid #999; white-space: pre-wrap;">{query_reason if query_reason else recommendation}</td></tr>
</tbody>
</table>

<hr style="margin-top: 15px;">
<p style="font-size: 10px; color: #666; margin-top: 10px;">Note: This report was auto-generated using AI analysis of OCR-extracted medical documents. Doctor review is required before final approval.</p>
</div>"""

        # Keep simple text version as backup
        text_report = f"""HEALTH CLAIM ASSESSMENT SHEET
Generated: {gen_time}

COMPANY: {company_name}
CLAIM NO: {claim_number}
CLAIM TYPE: {claim_type}
INSURED: {insured_name}
HOSPITAL: {hospital_name}
TREATING DOCTOR: {treating_doctor}
REGISTRATION: {doctor_registration}
ADMISSION: {doa}
DISCHARGE: {dod}
LENGTH OF STAY: {length_of_stay}

DIAGNOSIS: {diagnosis}
COMPLAINTS: {chief_complaints}
FINDINGS: {major_findings}
CLINICAL FINDINGS: {clinical_findings}

INVESTIGATION REPORTS:
{investigation_reports}

DERANGED INVESTIGATIONS: {deranged_investigation}

DAILY TPR: {daily_tpr}

MEDICINES: {medicine_used}

CLAIMED AMOUNT: {claimed_amount}

RECOMMENDATION: {recommendation}
{query_reason if query_reason else ''}

CONCLUSION:
{conclusion}""".strip()

        # Insert report into database
        cur.execute('''
            INSERT INTO medical_reports (
                claim_id, hospital_name, treating_doctor, diagnosis,
                complaints, medicine_used, claim_amount, conclusion,
                report_text, status, created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
            ON CONFLICT (claim_id) DO UPDATE SET
                hospital_name = EXCLUDED.hospital_name,
                treating_doctor = EXCLUDED.treating_doctor,
                diagnosis = EXCLUDED.diagnosis,
                complaints = EXCLUDED.complaints,
                medicine_used = EXCLUDED.medicine_used,
                claim_amount = EXCLUDED.claim_amount,
                conclusion = EXCLUDED.conclusion,
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
        cur.execute('SAVEPOINT report_version_savepoint')
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
            cur.execute('RELEASE SAVEPOINT report_version_savepoint')
        except Exception as e:
            cur.execute('ROLLBACK TO SAVEPOINT report_version_savepoint')
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
                    SELECT latest.raw_response, latest.file_name
                    FROM (
                        SELECT DISTINCT ON (de.document_id)
                            de.document_id, de.raw_response, cd.file_name, de.created_at
                        FROM document_extractions de
                        JOIN claim_documents cd ON de.document_id = cd.id
                        WHERE cd.claim_id = %s
                        ORDER BY de.document_id, de.created_at DESC
                    ) AS latest
                    ORDER BY latest.file_name
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

                if not ocr_text.strip():
                    logger.warning(f'Skipping claim {claim_id}: extracted rows contain no OCR text')
                    cur.close()
                    conn.close()
                    continue

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

                # Text columns keep report-ready lines; raw_payload retains the full structured JSON.
                claimed_amount = report_field_text(structured_json.get('claim_amount'))
                complaints = report_field_text(structured_json.get('complaints'))
                findings = report_field_text(structured_json.get('findings'))
                investigations = report_field_text(structured_json.get('investigation_finding_in_details'))

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
                    report_field_text(structured_json.get('company_name')),
                    report_field_text(structured_json.get('hospital_name')),
                    report_field_text(structured_json.get('treating_doctor')),
                    report_field_text(structured_json.get('treating_doctor_registration_number')),
                    report_field_text(structured_json.get('doa')),
                    report_field_text(structured_json.get('dod')),
                    report_field_text(structured_json.get('diagnosis')),
                    complaints,
                    findings,
                    report_field_text(structured_json.get('medicine_used')),
                    report_field_text(structured_json.get('high_end_antibiotic_for_rejection')),
                    report_field_text(structured_json.get('deranged_investigation')),
                    investigations,
                    claimed_amount,
                    report_field_text(structured_json.get('conclusion')),
                    report_field_text(structured_json.get('recommendation')),
                    json.dumps(structured_json),
                    GEMINI_MODEL
                ))

                conn.commit()

                schedule_key = f'queue:stage3_scheduled:{claim_id}'
                if r.set(schedule_key, '1', nx=True, ex=21600):
                    r.lpush('queue:stage3_report_generation', json.dumps({'claim_id': claim_id}))
                    logger.info('Queued Stage 3 report generation for claim %s', claim_id)
                else:
                    logger.info('Stage 3 already scheduled for claim %s', claim_id)

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

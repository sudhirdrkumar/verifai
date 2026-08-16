import os
import json
import re
import time
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

# Import ML predictor
from ml_claim_predictor import predict_claim

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
STAGE2_QUEUE = 'queue:stage2_claim_reduction'
STAGE2_RETRY_SET = 'queue:stage2_claim_reduction:retries'
STAGE2_RETRY_PAYLOADS = 'queue:stage2_claim_reduction:retry_payloads'
STAGE2_RETRY_BASE_SECONDS = int(os.getenv('STAGE2_RETRY_BASE_SECONDS', '300'))
STAGE2_RETRY_MAX_SECONDS = int(os.getenv('STAGE2_RETRY_MAX_SECONDS', '3600'))
GEMINI_CREDIT_CIRCUIT_KEY = 'circuit:gemini:credit_depleted'
GEMINI_CREDIT_PAUSE_SECONDS = int(os.getenv('GEMINI_CREDIT_PAUSE_SECONDS', '1800'))
GEMINI_REQUESTS_PER_MINUTE = int(os.getenv('GEMINI_REQUESTS_PER_MINUTE', '30'))


class GeminiCreditPausedError(RuntimeError):
    pass


class GeminiRateLimitPausedError(RuntimeError):
    pass


def reserve_gemini_request() -> None:
    credit_pause_ttl = r.ttl(GEMINI_CREDIT_CIRCUIT_KEY)
    if credit_pause_ttl > 0:
        raise GeminiCreditPausedError(
            f'Gemini credit circuit is open; retry available in {credit_pause_ttl}s'
        )

    minute_bucket = int(time.time() // 60)
    rate_key = f'rate:gemini:stage2:{minute_bucket}'
    request_count = r.incr(rate_key)
    if request_count == 1:
        r.expire(rate_key, 120)
    if request_count > GEMINI_REQUESTS_PER_MINUTE:
        raise GeminiRateLimitPausedError(
            f'Gemini Stage 2 rate limit reached ({GEMINI_REQUESTS_PER_MINUTE}/minute)'
        )


def open_gemini_credit_circuit(error: Exception) -> None:
    r.setex(
        GEMINI_CREDIT_CIRCUIT_KEY,
        GEMINI_CREDIT_PAUSE_SECONDS,
        str(error)[:500],
    )
    logger.error(
        'Gemini credit circuit opened for %ss after billing failure',
        GEMINI_CREDIT_PAUSE_SECONDS,
    )


def schedule_stage2_retry(task: dict, error: Exception) -> None:
    claim_id = str(task.get('claim_id') or '').strip()
    if not claim_id:
        logger.error('Cannot retry Stage 2 task without claim_id: %s', task)
        return

    attempt = max(int(task.get('attempt') or 0) + 1, 1)
    delay = min(STAGE2_RETRY_BASE_SECONDS * (2 ** min(attempt - 1, 4)), STAGE2_RETRY_MAX_SECONDS)
    retry_task = dict(task)
    retry_task.update({
        'claim_id': claim_id,
        'attempt': attempt,
        'last_error': str(error)[:500],
    })
    payload = json.dumps(retry_task)
    due_at = time.time() + delay
    pipe = r.pipeline(transaction=False)
    pipe.hset(STAGE2_RETRY_PAYLOADS, claim_id, payload)
    pipe.zadd(STAGE2_RETRY_SET, {claim_id: due_at})
    pipe.execute()
    logger.warning('Stage 2 retry %s scheduled for claim %s in %ss', attempt, claim_id, delay)


def promote_due_stage2_retries(limit: int = 20) -> int:
    promoted = 0
    for claim_id in r.zrangebyscore(STAGE2_RETRY_SET, 0, time.time(), start=0, num=limit):
        schedule_key = f'queue:stage2_scheduled:{claim_id}'
        if not r.set(schedule_key, '1', nx=True, ex=21600):
            continue
        payload = r.hget(STAGE2_RETRY_PAYLOADS, claim_id)
        if not payload or not r.zrem(STAGE2_RETRY_SET, claim_id):
            r.delete(schedule_key)
            continue
        r.hdel(STAGE2_RETRY_PAYLOADS, claim_id)
        r.lpush(STAGE2_QUEUE, payload)
        promoted += 1
    if promoted:
        logger.info('Promoted %s delayed Stage 2 retries', promoted)
    return promoted


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


def legacy_extraction_text(raw_response, extracted_entities, model_name='') -> str:
    """Return report-ready source text from current or legacy extraction rows."""
    raw_text = str(raw_response or '').strip()
    if raw_text:
        return raw_text
    if str(model_name or '').lower() == 'policy-excluded':
        return ''

    entities = extracted_entities
    if isinstance(entities, str):
        try:
            entities = json.loads(entities)
        except json.JSONDecodeError:
            return entities.strip()
    if not entities:
        return ''
    if isinstance(entities, dict) and (
        entities.get('excluded') is True or entities.get('kyc_excluded') is True
    ):
        return ''

    ignored_keys = {
        'mime_type', 'text_source', 'document_name', 'excluded',
        'kyc_excluded', 'reason',
    }

    def prune(value):
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                if str(key).lower() in ignored_keys:
                    continue
                cleaned = prune(item)
                if cleaned not in (None, '', [], {}):
                    result[key] = cleaned
            return result
        if isinstance(value, list):
            return [cleaned for item in value if (cleaned := prune(item)) not in (None, '', [], {})]
        if isinstance(value, str):
            return value.strip()
        return value

    cleaned_entities = prune(entities)
    if cleaned_entities in (None, '', [], {}):
        return ''
    if isinstance(cleaned_entities, str):
        return cleaned_entities
    return json.dumps(cleaned_entities, ensure_ascii=False, separators=(',', ':'))


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
        'clinical_course_and_discharge_condition': ('clinical_course', 'discharge_condition', 'outcome'),
        'procedure_or_surgery': ('procedure', 'surgery', 'procedure_performed', 'treatment_procedure'),
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
        prompt = f'''You are a medical data extraction expert. Extract ALL medical claim data from the OCR text provided.
Return ONLY valid JSON (no markdown, no extra text).

CRITICAL EXTRACTION RULES:
1. For investigation_finding_in_details: Extract EVERY lab/test result with ACTUAL VALUES
   - Blood tests: Hemoglobin, RBC, WBC, Platelets, Hematocrit, MCV, MCH
   - Chemistry: Creatinine, BUN, Glucose, Sodium, Potassium, Calcium
   - Liver: Bilirubin, ALT, AST, ALP, Albumin, Total Protein
   - Vital Signs: BP, HR, SpO2, RR, Temperature (with values and units)
   - Imaging: USG findings, X-ray reports, CT/MRI findings (specific findings, not just "done")
   - Culture/Serology: Blood culture, urine culture, sensitivity reports
   - Format: "TestName: value unit (reference: normal_range)" or for vitals "BP: 120/80 mmHg, HR: 85/min"
   - If value exceeds reference range, mark as HIGH/LOW

2. For deranged_investigation: List ONLY abnormal values
   - Format: "TestName: abnormal_value unit [HIGH/LOW vs reference]"
   - Example: "Hemoglobin: 7.5 g/dL [LOW vs 12-16]"

3. For daily_tpr_chart: Extract vital signs by date
   - Format: "DD-MM-YYYY: BP min-max | HR min-max | SpO2 | Temp"

4. For medicines: Extract with dosage
   - Format: "Medicine | Strength | Route | Frequency | Duration"

5. Do NOT return generic text:
   - ❌ "USG: YES, all investigations done"
   - ❌ "relevant lab investigations done"
   - ✅ "USG: Normal liver, GB, spleen; no free fluid. Kidneys normal, no hydronephrosis"
   - ✅ "Hemoglobin: 12.5 g/dL, WBC: 8,000/μL, Platelets: 250,000/μL"

REQUIRED JSON - EXTRACT ALL SECTIONS:
{{
  "company_name": "insurance company",
  "claim_type": "Cashless/Reimbursement",
  "insured_name": "patient name",
  "hospital_name": "hospital",
  "treating_doctor": "doctor name",
  "treating_doctor_registration_number": "reg number or '-'",
  "doa": "DD-MM-YYYY",
  "dod": "DD-MM-YYYY",
  "diagnosis": "primary diagnosis",
  "complaints": "chief complaints at admission",
  "major_diagnostic_finding": "vital signs and major findings at admission/during stay",
  "findings": "clinical examination - vitals with values",
  "alcoholism_history": "yes/no or '-'",
  "all_investigation_reports": "ALL lab tests with values: list line-by-line each test (CBC: Hemoglobin, WBC, Platelets, RBC, etc; LFT: Bilirubin, ALT, AST, Albumin, etc; RFT: Creatinine, BUN, etc; imaging: USG findings, X-ray findings, etc)",
  "date_wise_investigation_reports": "if dates available: DD-MM-YYYY: Test1 value, Test2 value, etc. List by date of investigation",
  "deranged_investigation": "ONLY abnormal values: TestName: abnormal_value [HIGH/LOW]",
  "daily_tpr_chart_min_max": "Vital signs with min-max: Date | BP min-max | HR min-max | SpO2 | Temp min-max",
  "medicine_used": "Every medicine listed with strength and frequency: Medicine | Strength | Route | Frequency | Duration",
  "high_end_antibiotic_for_rejection": "meropenem/linezolid/vancomycin/ciprofloxacin/etc if present",
  "investigation_finding_in_details": "Complete lab report with ALL values - CBC (Hemoglobin, WBC, Platelets, RBC, Hematocrit), LFT (Bilirubin, Albumin, AST, ALT, ALP), RFT (Creatinine, BUN), imaging findings with details",
  "claim_amount": "claimed amount",
  "procedure_or_surgery": "exact procedure/surgery performed, or '-' when none is documented",
  "clinical_course_and_discharge_condition": "response to treatment, improvement/deterioration, and condition at discharge; do not invent",
  "admission_required": "Justified/Not Justified/Query",
  "conclusion": "100-160 word medico-legal conclusion. Include presenting complaints and duration, diagnosis, objective clinical findings, specific abnormal or diagnosis-supporting investigations, conservative treatment with key medicines/antibiotics or exact surgery, documented response/discharge condition, medical necessity of admission, and a final admissibility statement aligned with recommendation. Use only documented facts; explicitly identify material evidence gaps instead of inventing facts.",
  "recommendation": "exactly one of APPROVE, REJECT, QUERY",
  "query_reason": "specific missing evidence when recommendation is QUERY, otherwise empty"
}}

OCR TEXT ({len(ocr_text)} chars):
{ocr_text}

CRITICAL: Extract actual investigation VALUES, not generic summaries. Include units, reference ranges, and abnormal flags.
Return ONLY the JSON object, nothing else.'''

        reserve_gemini_request()
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
        raise
    except (GeminiCreditPausedError, GeminiRateLimitPausedError):
        raise
    except Exception as e:
        error_text = str(e).lower()
        if 'prepayment credits are depleted' in error_text or 'billing#prepay' in error_text:
            open_gemini_credit_circuit(e)
        logger.error(f'Gemini extraction failed: {e}', exc_info=True)
        raise

def _conclusion_value(structured_json: dict, *keys: str) -> str:
    for key in keys:
        raw_value = report_field_text(structured_json.get(key))
        value = re.sub(r'\s*\n\s*', '; ', raw_value)
        value = re.sub(r'[ \t]+', ' ', value).strip(' -;,.')
        if not value:
            continue
        if value.lower() in {'none', 'nil', 'null', 'not available', 'not documented', 'unknown'}:
            continue
        return value
    return ''


def _has_specific_investigation(value: str) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    return not re.search(
        r'^(?:no (?:deranged )?investigation(?: values?)?(?: (?:were )?found| available)?|no significant abnormality|relevant investigations? (?:were )?done|investigations? done)[\s.]*$',
        text,
        re.IGNORECASE,
    )


def generate_medical_legal_conclusion(structured_json: dict) -> str:
    """Build a decision-aligned conclusion from the single-pass Gemini structure."""
    diagnosis = _conclusion_value(structured_json, 'diagnosis') or 'the documented diagnosis'
    complaints = _conclusion_value(
        structured_json,
        'complaints',
        'chief_complaints',
        'chief_complaints_at_admission',
    ) or 'the documented presenting complaints'
    findings = _conclusion_value(
        structured_json,
        'major_diagnostic_finding',
        'major_diagnostic_findings',
        'findings',
        'clinical_findings',
    )
    deranged = _conclusion_value(structured_json, 'deranged_investigation')
    investigations = _conclusion_value(
        structured_json,
        'investigation_finding_in_details',
        'all_investigation_reports',
        'investigation_reports',
    )
    medicines = _conclusion_value(structured_json, 'medicine_used', 'medicines', 'treatment_medicines')
    procedure = _conclusion_value(
        structured_json,
        'procedure_or_surgery',
        'procedure',
        'surgery',
        'procedure_performed',
    )
    clinical_course = _conclusion_value(
        structured_json,
        'clinical_course_and_discharge_condition',
        'clinical_course',
        'discharge_condition',
        'outcome',
    )
    query_reason = _conclusion_value(structured_json, 'query_reason')
    recommendation_raw = _conclusion_value(structured_json, 'recommendation', 'final_recommendation').upper()
    if any(token in recommendation_raw for token in ('REJECT', 'INADMISSIBLE', 'NOT JUSTIFIED')):
        recommendation = 'REJECT'
    elif any(token in recommendation_raw for token in ('APPROVE', 'ADMISSIBLE', 'JUSTIFIED')):
        recommendation = 'APPROVE'
    else:
        recommendation = 'QUERY'

    sentences = [
        f"Based on the available medical records, the patient presented with {complaints} and was diagnosed with {diagnosis}."
    ]
    if findings:
        sentences.append(f"Objective clinical findings included {findings}.")
    if _has_specific_investigation(deranged):
        sentences.append(f"The relevant abnormal investigation findings were {deranged}.")
    elif _has_specific_investigation(investigations):
        sentences.append(f"The documented investigations included {investigations}.")

    surgical_text = ' '.join((procedure, medicines)).lower()
    is_surgical = bool(re.search(
        r'\b(?:surgery|surgical|procedure|operation|operative|orif|fixation|repair|ligation|lscs|caesarean|excision|appendectomy)\b',
        surgical_text,
    ))
    if is_surgical and procedure:
        treatment_sentence = f"The patient was managed surgically with {procedure}"
        if medicines:
            treatment_sentence += f", together with {medicines}"
        sentences.append(treatment_sentence + '.')
    elif medicines:
        sentences.append(f"The patient was managed conservatively with {medicines}.")
    elif procedure:
        sentences.append(f"The documented treatment/procedure was {procedure}.")

    if clinical_course:
        sentences.append(f"The documented clinical course and discharge status were {clinical_course}.")

    if recommendation == 'APPROVE':
        sentences.append(
            "The documented presentation, objective findings, and treatment support the medical necessity of admission. "
            "The case appears clinically consistent and is recommended as admissible, subject to policy terms and bill verification."
        )
    elif recommendation == 'REJECT':
        sentences.append(
            "The submitted evidence does not adequately establish the medical necessity or admissibility of the claimed inpatient care. "
            "The claim is therefore not recommended for approval, subject to policy terms and final medical review."
        )
    else:
        gap = query_reason or 'material clinical or supporting evidence remains insufficiently documented'
        sentences.append(
            f"However, {gap}. The claim should remain under query until the required records are provided and verified."
        )

    return re.sub(r'\s+', ' ', ' '.join(sentences)).strip()


def auto_generate_report(cur, claim_id: str, structured_json: dict):
    """Auto-generate medical report for claim with ML-based recommendations."""
    try:
        from datetime import datetime as dt

        # Get ML prediction for recommendation
        ml_result = predict_claim(structured_json)
        ml_recommendation = None
        ml_confidence = 0.0
        ml_probabilities = {}

        if ml_result:
            ml_recommendation = ml_result.get('recommendation')
            ml_confidence = ml_result.get('confidence', 0.0)
            ml_probabilities = ml_result.get('probabilities', {})
            logger.info(f'📊 ML prediction for {claim_id}: {ml_recommendation} ({ml_confidence:.2%})')

        # Get Gemini recommendation (fallback)
        gemini_recommendation = structured_json.get('recommendation', 'need_more_evidence')

        # Use ML recommendation if available and confident, else use Gemini
        final_recommendation = ml_recommendation or gemini_recommendation

        if ml_result:
            logger.info(f'Recommendations - ML: {ml_recommendation} | Gemini: {gemini_recommendation} | Final: {final_recommendation}')

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
        investigation_reports = structured_json.get('all_investigation_reports', structured_json.get('investigation_reports', '-'))
        investigation_details = structured_json.get('investigation_finding_in_details', 'Not detailed')
        datewise_investigations = structured_json.get('date_wise_investigation_reports', '-')
        deranged_investigation = structured_json.get('deranged_investigation', 'No deranged investigation values found.')
        daily_tpr = structured_json.get('daily_tpr_chart_min_max', structured_json.get('daily_tpr_chart', '-'))
        medicine_used = structured_json.get('medicine_used', '-')
        claimed_amount = structured_json.get('claimed_amount', '-')
        recommendation = final_recommendation.upper() if final_recommendation else 'QUERY'
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
<tr><td style="padding: 6px; border: 1px solid #ddd; white-space: pre-wrap; font-family: monospace;">{investigation_reports if investigation_reports and investigation_reports != '-' else 'No specific investigation values documented'}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">DATE-WISE INVESTIGATION REPORTS</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd; white-space: pre-wrap; font-family: monospace;">{datewise_investigations if datewise_investigations and datewise_investigations != '-' else 'No date-wise investigation reports available.'}</td></tr>
</tbody>
</table>

<div style="background-color: #f5f5f5; padding: 8px; margin: 12px 0; font-weight: bold; font-size: 12px;">INVESTIGATION FINDINGS IN DETAIL</div>
<table style="width: 100%; border-collapse: collapse; margin: 8px 0;">
<tbody>
<tr><td style="padding: 6px; border: 1px solid #ddd; white-space: pre-wrap; font-family: monospace;">{investigation_details if investigation_details and investigation_details != '-' else 'No detailed investigation findings'}</td></tr>
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
<div style="background-color: #f0f8ff; padding: 8px; margin-top: 10px; border-radius: 4px; font-size: 10px; color: #333;">
<strong>AI Analysis:</strong>
<br/>ML Model: {recommendation} (confidence: {ml_confidence:.1%})<br/>Gemini: {gemini_recommendation}<br/>
{f'ML Probabilities - Approve: {ml_probabilities.get("approve", 0):.1%}, Reject: {ml_probabilities.get("reject", 0):.1%}, Need Evidence: {ml_probabilities.get("need_more_evidence", 0):.1%}' if ml_result else 'ML model not available'}
<br/><br/>
<em>Note: This report was auto-generated using ML and OCR analysis of medical documents. Doctor review is required before final approval.</em>
</div>
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
            hospital_name,
            treating_doctor,
            diagnosis,
            chief_complaints,
            medicine_used,
            claimed_amount,
            conclusion,
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
            promote_due_stage2_retries()
            raw_task = r.brpop(STAGE2_QUEUE, timeout=30)
            if not raw_task:
                continue

            task = json.loads(raw_task[1])
            claim_id = task['claim_id']

            logger.info(f'Processing Claim {claim_id}: Extracting structured data with {GEMINI_MODEL}')

            conn = None
            cur = None
            try:
                conn = psycopg.connect(DB_DSN)
                cur = conn.cursor()

                # Get OCR text from ALL documents in Stage 1 and combine
                cur.execute('''
                    SELECT latest.raw_response, latest.extracted_entities,
                           latest.model_name, latest.file_name
                    FROM (
                        SELECT DISTINCT ON (de.document_id)
                            de.document_id, de.raw_response, de.extracted_entities,
                            de.model_name, cd.file_name, de.created_at
                        FROM document_extractions de
                        JOIN claim_documents cd ON de.document_id = cd.id
                        WHERE cd.claim_id = %s
                        ORDER BY de.document_id, de.created_at DESC
                    ) AS latest
                    ORDER BY latest.file_name
                ''', (claim_id,))

                ocr_rows = cur.fetchall()
                if not ocr_rows:
                    raise ValueError(f'No OCR text found for claim {claim_id}')

                # Combine OCR from all documents
                combined_texts = []
                for row in ocr_rows:
                    raw_response, extracted_entities, model_name, filename = row
                    source_text = legacy_extraction_text(
                        raw_response,
                        extracted_entities,
                        model_name,
                    )
                    if source_text:
                        combined_texts.append(f'--- Document: {filename} ---\n{source_text}')

                ocr_text = '\n\n'.join(combined_texts)
                logger.info(f'Combined OCR from {len(ocr_rows)} documents: {len(ocr_text)} chars for claim {claim_id}')

                if not ocr_text.strip():
                    raise ValueError(f'Extracted rows contain no OCR text for claim {claim_id}')

                # Extract structured data with Gemini
                structured_json = extract_structured_data_gemini(ocr_text, claim_id)

                if not structured_json:
                    raise RuntimeError(f'Failed to extract structured data for claim {claim_id}')

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

                r.zrem(STAGE2_RETRY_SET, claim_id)
                r.hdel(STAGE2_RETRY_PAYLOADS, claim_id)

                cur.close()
                conn.close()

                logger.info(f'✅ Claim {claim_id} structured + report generated by {GEMINI_MODEL}')

            except Exception as e:
                logger.error(f'Stage 2 Error on Claim {claim_id}: {str(e)}', exc_info=True)
                schedule_stage2_retry(task, e)
            finally:
                if cur is not None:
                    cur.close()
                if conn is not None:
                    conn.close()
                r.delete(f'queue:stage2_scheduled:{claim_id}')

        except redis.exceptions.TimeoutError:
            continue
        except Exception as e:
            logger.error(f'Worker loop error: {str(e)}', exc_info=True)

if __name__ == '__main__':
    run_stage2_loop()

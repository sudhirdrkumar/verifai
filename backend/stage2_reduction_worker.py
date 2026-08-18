import os
import json
import re
import time
import redis
import psycopg
import logging
from datetime import datetime
from uuid import uuid4
from dotenv import load_dotenv
import google.generativeai as genai

# Load .env file from parent directory
import sys
from pathlib import Path
env_path = Path(__file__).parent.parent / '.env'
load_dotenv(env_path)

# Import ML predictor and ensemble
from ml_claim_predictor import predict_claim
from ml_ensemble_predictor import predict_with_ensemble
from ocr_batching import excluded_document_reason

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
STAGE2_RETRY_MAX_ATTEMPTS = max(1, int(os.getenv('STAGE2_RETRY_MAX_ATTEMPTS', '3')))
GEMINI_CREDIT_CIRCUIT_KEY = 'circuit:gemini:credit_depleted'
GEMINI_CREDIT_PAUSE_SECONDS = int(os.getenv('GEMINI_CREDIT_PAUSE_SECONDS', '1800'))
GEMINI_REQUESTS_PER_MINUTE = int(os.getenv('GEMINI_REQUESTS_PER_MINUTE', '30'))
ML_RECOMMENDATION_MIN_CONFIDENCE = float(os.getenv('ML_RECOMMENDATION_MIN_CONFIDENCE', '0.55'))
STAGE1_DELAYED_CLAIMS = 'queue:stage1_ocr_extraction:delayed_claims'
NO_EXTRACTION_SCAN_INTERVAL_SECONDS = max(
    30,
    int(os.getenv('NO_EXTRACTION_SCAN_INTERVAL_SECONDS', '60')),
)
_last_no_extraction_scan = 0.0


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


def mark_claim_for_manual_process(claim_id: str, error: Exception, attempts: int) -> None:
    reason = str(error or 'Stage 2 structuring failed').strip()[:500]
    marker = json.dumps({
        'manual_process_required': True,
        'manual_process_stage': 'structure',
        'manual_process_reason': reason,
        'manual_process_attempts': int(attempts),
    })
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute('''
                UPDATE extraction_jobs ej
                SET job_payload = COALESCE(ej.job_payload, '{}'::jsonb) || %s::jsonb
                WHERE ej.id IN (
                    SELECT DISTINCT ON (latest.document_id) latest.id
                    FROM extraction_jobs latest
                    WHERE latest.claim_id = %s
                    ORDER BY latest.document_id, latest.queued_at DESC NULLS LAST, latest.created_at DESC
                )
            ''', (marker, claim_id))
        conn.commit()

    pipe = r.pipeline(transaction=False)
    pipe.zrem(STAGE2_RETRY_SET, claim_id)
    pipe.hdel(STAGE2_RETRY_PAYLOADS, claim_id)
    pipe.delete(f'queue:stage2_scheduled:{claim_id}')
    pipe.execute()
    logger.error(
        'Claim %s moved to Manual Process after %s Stage 2 attempts: %s',
        claim_id,
        attempts,
        reason,
    )


def schedule_stage2_retry(task: dict, error: Exception) -> None:
    claim_id = str(task.get('claim_id') or '').strip()
    if not claim_id:
        logger.error('Cannot retry Stage 2 task without claim_id: %s', task)
        return

    attempt = max(int(task.get('attempt') or 0) + 1, 1)
    if attempt >= STAGE2_RETRY_MAX_ATTEMPTS:
        mark_claim_for_manual_process(claim_id, error, attempt)
        return

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


def has_meaningful_extraction(structured_json: dict) -> bool:
    placeholders = {'', '-', 'na', 'n/a', 'none', 'nil', 'null', 'not available'}
    clinical_fields = (
        'diagnosis',
        'complaints',
        'findings',
        'investigation_finding_in_details',
        'medicine_used',
        'deranged_investigation',
    )
    return any(
        report_field_text(structured_json.get(field)).strip().lower() not in placeholders
        for field in clinical_fields
    )


def queue_textract_recovery(claim_id: str) -> int:
    """Queue one Textract-only recovery pass for a placeholder extraction."""
    tasks = []
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(
                'SELECT pg_advisory_xact_lock(hashtext(%s))',
                (f'textract-recovery:{claim_id}',),
            )
            cur.execute('''
                SELECT
                    EXISTS (
                        SELECT 1 FROM extraction_jobs
                        WHERE claim_id = %s
                          AND status IN ('queued', 'processing', 'running')
                    ),
                    EXISTS (
                        SELECT 1 FROM extraction_jobs
                        WHERE claim_id = %s
                          AND provider = 'aws_textract'
                          AND job_payload ->> 'queued_by' = 'no_extraction_fallback'
                    )
            ''', (claim_id, claim_id))
            has_active_job, recovery_already_attempted = cur.fetchone()
            if has_active_job or recovery_already_attempted:
                return 0

            cur.execute('''
                SELECT id, COALESCE(storage_key, ''), COALESCE(file_name, '')
                FROM claim_documents
                WHERE claim_id = %s
                ORDER BY uploaded_at NULLS LAST, id
            ''', (claim_id,))
            documents = cur.fetchall()
            for document_id, storage_key, file_name in documents:
                if not storage_key:
                    continue
                exclusion_reason = excluded_document_reason(file_name)
                if exclusion_reason:
                    logger.info(
                        'Skipping excluded document during Textract recovery: %s (%s)',
                        file_name,
                        exclusion_reason,
                    )
                    continue
                job_id = uuid4()
                cur.execute('''
                    INSERT INTO extraction_jobs (
                        id, document_id, claim_id, provider, actor_id, force_refresh,
                        status, priority, queued_at, job_payload
                    ) VALUES (
                        %s, %s, %s, 'aws_textract', 'pipeline-recovery', TRUE,
                        'queued', 50, NOW(), %s::jsonb
                    )
                ''', (
                    job_id,
                    document_id,
                    claim_id,
                    json.dumps({'queued_by': 'no_extraction_fallback'}),
                ))
                cur.execute(
                    "UPDATE claim_documents SET parse_status = 'queued' WHERE id = %s",
                    (document_id,),
                )
                bucket = os.getenv('S3_BUCKET', 'rightworks-docs')
                key = str(storage_key)
                if key.startswith('s3://'):
                    location = key[5:].split('/', 1)
                    bucket = location[0]
                    key = location[1] if len(location) > 1 else ''
                if not key:
                    continue
                tasks.append({
                    'job_id': str(job_id),
                    'document_id': str(document_id),
                    'claim_id': str(claim_id),
                    'provider': 'aws_textract',
                    's3_bucket': bucket,
                    's3_key': key,
                    'file_name': str(file_name or ''),
                    'force_refresh': True,
                })
        conn.commit()

    if not tasks:
        return 0
    pipe = r.pipeline(transaction=False)
    task_key = f'queue:stage1_ocr_extraction:claim:{claim_id}'
    for task in tasks:
        pipe.hset(task_key, task['job_id'], json.dumps(task))
    pipe.expire(task_key, 86400)
    pipe.zadd(STAGE1_DELAYED_CLAIMS, {str(claim_id): time.time()})
    pipe.delete(f'queue:stage2_scheduled:{claim_id}')
    pipe.delete(f'queue:stage3_scheduled:{claim_id}')
    pipe.execute()
    logger.warning('Queued %s Textract recovery documents for claim %s', len(tasks), claim_id)
    return len(tasks)


def reconcile_no_extraction_claims(limit: int = 50) -> int:
    global _last_no_extraction_scan
    now = time.time()
    if now - _last_no_extraction_scan < NO_EXTRACTION_SCAN_INTERVAL_SECONDS:
        return 0
    _last_no_extraction_scan = now

    placeholders = "('', '-', 'na', 'n/a', 'none', 'nil', 'null', 'not available')"
    with psycopg.connect(DB_DSN) as conn:
        with conn.cursor() as cur:
            cur.execute(f'''
                SELECT csd.claim_id
                FROM claim_structured_data csd
                WHERE NOT (
                    LOWER(BTRIM(COALESCE(csd.diagnosis, ''))) NOT IN {placeholders}
                    OR LOWER(BTRIM(COALESCE(csd.complaints, ''))) NOT IN {placeholders}
                    OR LOWER(BTRIM(COALESCE(csd.findings, ''))) NOT IN {placeholders}
                    OR LOWER(BTRIM(COALESCE(csd.investigation_finding_in_details, ''))) NOT IN {placeholders}
                    OR LOWER(BTRIM(COALESCE(csd.medicine_used, ''))) NOT IN {placeholders}
                    OR LOWER(BTRIM(COALESCE(csd.deranged_investigation, ''))) NOT IN {placeholders}
                )
                  AND EXISTS (SELECT 1 FROM claim_documents cd WHERE cd.claim_id = csd.claim_id)
                  AND NOT EXISTS (
                      SELECT 1 FROM extraction_jobs ej
                      WHERE ej.claim_id = csd.claim_id
                        AND ej.status IN ('queued', 'processing', 'running')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM extraction_jobs ej
                      WHERE ej.claim_id = csd.claim_id
                        AND ej.provider = 'aws_textract'
                        AND ej.job_payload ->> 'queued_by' = 'no_extraction_fallback'
                  )
                ORDER BY csd.updated_at NULLS LAST
                LIMIT %s
            ''', (limit,))
            claim_ids = [str(row[0]) for row in cur.fetchall()]

    queued = sum(queue_textract_recovery(claim_id) for claim_id in claim_ids)
    if queued:
        logger.info(
            'No Extraction reconciler queued %s documents across %s claims',
            queued,
            len(claim_ids),
        )
    return queued


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
  "conclusion": "Leave empty. The final conclusion is generated locally from this structured evidence after the ML verdict is selected.",
  "recommendation": "exactly one of APPROVE, REJECT, QUERY",
  "query_reason": "When recommendation is QUERY, list the exact missing documents/evidence (for example OT notes, investigation reports, treatment chart, discharge summary, or another relevant record). Never return QUERY without a specific deficiency. Leave empty for APPROVE or REJECT."
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


def _conclusion_clip(value: str, limit: int = 260) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip(' -;,.')
    if len(text) <= limit:
        return text
    clipped = text[:limit].rsplit(' ', 1)[0].strip(' -;,.')
    return f'{clipped}...' if clipped else text[:limit]


def _length_of_stay_text(structured_json: dict) -> str:
    raw_los = _conclusion_value(structured_json, 'length_of_stay_days', 'length_of_stay', 'los_days')
    match = re.search(r'\d+(?:\.\d+)?', raw_los)
    if match:
        value = match.group(0)
        return f'{value}-day'

    doa = _conclusion_value(structured_json, 'doa', 'admission_date', 'date_of_admission')
    dod = _conclusion_value(structured_json, 'dod', 'discharge_date', 'date_of_discharge')
    for date_format in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            admission = datetime.strptime(doa[:10], date_format)
            discharge = datetime.strptime(dod[:10], date_format)
            return f'{max(0, (discharge - admission).days)}-day'
        except (TypeError, ValueError):
            continue
    return 'documented'


def _documented_antibiotics(medicine_text: str, high_end_signal: str) -> list[str]:
    combined = f'{medicine_text} {high_end_signal}'
    antibiotic_names = (
        'meropenem', 'imipenem', 'ertapenem', 'doripenem', 'linezolid', 'vancomycin',
        'teicoplanin', 'colistin', 'polymyxin', 'tigecycline', 'cefoperazone',
        'sulbactam', 'piperacillin', 'tazobactam', 'cefepime', 'ceftazidime',
        'ceftriaxone', 'cefotaxime', 'amoxicillin', 'clavulanate', 'azithromycin',
        'clarithromycin', 'doxycycline', 'ciprofloxacin', 'levofloxacin',
        'moxifloxacin', 'amikacin', 'gentamicin', 'metronidazole', 'clindamycin',
    )
    found: list[str] = []
    for name in antibiotic_names:
        if re.search(rf'\b{re.escape(name)}\b', combined, flags=re.IGNORECASE):
            label = name.title()
            if label not in found:
                found.append(label)
    return found


def _medicine_phrase_matches(source: str, candidate: str) -> bool:
    source_norm = re.sub(r'[^a-z0-9]+', ' ', str(source or '').lower()).strip()
    candidate_norm = re.sub(r'[^a-z0-9]+', ' ', str(candidate or '').lower()).strip()
    if len(candidate_norm) < 4:
        return False
    if re.search(rf'(?<![a-z0-9]){re.escape(candidate_norm)}(?![a-z0-9])', source_norm):
        return True

    ignored = {
        'inj', 'injection', 'tab', 'tablet', 'cap', 'capsule', 'syp', 'syrup',
        'vial', 'ampoule', 'solution', 'suspension', 'oral', 'iv', 'im', 'mg',
        'mcg', 'gm', 'g', 'ml', 'unit', 'units',
    }
    core_tokens = [
        token for token in candidate_norm.split()
        if token not in ignored and not re.fullmatch(r'\d+(?:\.\d+)?', token)
    ]
    if not core_tokens:
        return False
    core = ' '.join(core_tokens)
    return len(core) >= 4 and bool(
        re.search(rf'(?<![a-z0-9]){re.escape(core)}(?![a-z0-9])', source_norm)
    )


def _medicine_aliases(name: str, components: str) -> list[str]:
    values: list[str] = []
    for value in (name, components):
        for part in re.split(r'[,;/+|]|\band\b|\bwith\b', str(value or ''), flags=re.IGNORECASE):
            alias = re.sub(r'\s+', ' ', part).strip(' .:-')
            if len(re.sub(r'[^a-z0-9]+', '', alias.lower())) >= 4 and alias not in values:
                values.append(alias)
    return values


def _has_non_negated_evidence(text: str, pattern: str) -> bool:
    for match in re.finditer(pattern, text, flags=re.IGNORECASE):
        prefix_start = max(0, match.start() - 55)
        prefix = text[prefix_start:match.start()]
        suffix = text[match.end():min(len(text), match.end() + 45)]
        negated_before = re.search(
            r'\b(?:no|not|without|absent|missing|unavailable|negative for|not documented|not provided)\b.{0,45}$',
            prefix,
            flags=re.IGNORECASE,
        )
        negated_after = re.search(
            r'^.{0,35}\b(?:not documented|not provided|not done|not available|missing|unavailable|absent|negative)\b',
            suffix,
            flags=re.IGNORECASE,
        )
        if not negated_before and not negated_after:
            return True
    return False


def _antibiotic_support(rule_name: str, evidence: str, antibiotic_count: int) -> tuple[bool, str]:
    name = rule_name.lower()
    culture = _has_non_negated_evidence(evidence, r'\b(?:culture|sensitivity|susceptibility|antibiogram|c\s*&\s*s)\b')
    severe = _has_non_negated_evidence(
        evidence,
        r'\b(?:sepsis|septic shock|organ dysfunction|hemodynamic instability|hypotension|vasopressor|bacteremia|clinical deterioration)\b',
    )
    resistant = _has_non_negated_evidence(
        evidence,
        r'\b(?:esbl|mdr|xdr|carbapenem[- ]resistant|drug[- ]resistant|resistant organism|resistant infection)\b',
    )
    gram_positive = _has_non_negated_evidence(
        evidence,
        r'\b(?:mrsa|vre|resistant gram[- ]positive|methicillin[- ]resistant|vancomycin[- ]resistant)\b',
    )
    gram_negative = _has_non_negated_evidence(
        evidence,
        r'\b(?:gram[- ]negative|pseudomonas|acinetobacter|klebsiella|enterobacter)\b',
    )
    complicated_bacterial = _has_non_negated_evidence(
        evidence,
        r'\b(?:complicated infection|bacterial infection|abscess|peritonitis|pyelonephritis|hospital[- ]acquired pneumonia)\b',
    )
    allergy = _has_non_negated_evidence(evidence, r'\b(?:beta[- ]lactam allergy|penicillin allergy|cephalosporin allergy)\b')
    specialist = _has_non_negated_evidence(evidence, r'\b(?:infectious disease specialist|microbiologist advice|specialist rationale)\b')

    if 'levofloxacin' in name or 'moxifloxacin' in name:
        return antibiotic_count <= 1, 'no unnecessary broad-spectrum antibiotic combination documented'
    if 'aztreonam' in name:
        supported = gram_negative or allergy or culture
        return supported, 'Gram-negative indication, culture support, or beta-lactam allergy documented' if supported else ''
    if any(token in name for token in ('linezolid', 'daptomycin', 'vancomycin', 'teicoplanin')):
        supported = gram_positive or culture
        return supported, 'resistant Gram-positive or culture evidence documented' if supported else ''
    if any(token in name for token in ('colistin', 'polymyxin', 'ceftazidime-avibactam', 'cefiderocol')):
        supported = resistant or (gram_negative and culture) or specialist
        return supported, 'resistant Gram-negative, susceptibility, or specialist evidence documented' if supported else ''
    if any(token in name for token in ('meropenem', 'imipenem', 'ertapenem', 'tigecycline')):
        supported = resistant or severe or culture
        return supported, 'sepsis, resistance, culture, or sensitivity evidence documented' if supported else ''
    if any(token in name for token in ('piperacillin', 'cefoperazone')):
        supported = severe or complicated_bacterial or culture
        return supported, 'severe/complicated bacterial or culture evidence documented' if supported else ''
    supported = culture or severe or resistant
    return supported, 'culture, severity, or resistance evidence documented' if supported else ''


def assess_antibiotic_scrutiny(cur, structured_json: dict) -> dict:
    """Resolve extracted medicines through the DB catalog and assess scrutiny rules locally."""
    medicine_text = _conclusion_value(structured_json, 'medicine_used', 'medicines', 'treatment_medicines')
    high_end_signal = _conclusion_value(structured_json, 'high_end_antibiotic_for_rejection')
    source_text = f'{medicine_text}\n{high_end_signal}'.strip()
    if not source_text:
        return {'matched': [], 'flagged': [], 'catalog_used': False}

    savepoint = 'antibiotic_scrutiny_lookup'
    try:
        cur.execute(f'SAVEPOINT {savepoint}')
        cur.execute('''
            SELECT medicine_name, components, scrutiny_level, scrutiny_flag_condition, source
            FROM medicine_component_lookup
            WHERE scrutiny_level IS NOT NULL OR is_high_end_antibiotic = TRUE
            ORDER BY CASE WHEN source = 'antibiotic_scrutiny' THEN 0 ELSE 1 END, medicine_name
        ''')
        rows = cur.fetchall()
        cur.execute(f'RELEASE SAVEPOINT {savepoint}')
    except Exception as exc:
        try:
            cur.execute(f'ROLLBACK TO SAVEPOINT {savepoint}')
            cur.execute(f'RELEASE SAVEPOINT {savepoint}')
        except Exception:
            pass
        logger.warning('Medicine scrutiny catalog lookup failed: %s', exc)
        return {'matched': [], 'flagged': [], 'catalog_used': False, 'error': str(exc)[:240]}

    rules: list[dict] = []
    catalog: list[dict] = []
    for medicine_name, components, level, flag_condition, source in rows:
        entry = {
            'name': str(medicine_name or '').strip(),
            'components': str(components or '').strip(),
            'level': str(level or '').strip(),
            'flag_condition': str(flag_condition or '').strip(),
            'source': str(source or '').strip(),
        }
        entry['aliases'] = _medicine_aliases(entry['name'], entry['components'])
        catalog.append(entry)
        if entry['level']:
            rules.append(entry)

    matched_rule_names: set[str] = set()
    for rule in rules:
        if any(_medicine_phrase_matches(source_text, alias) for alias in rule['aliases']):
            matched_rule_names.add(rule['name'])

    # Resolve a documented brand through its DB components, then map those components to a rule.
    for medicine in catalog:
        if medicine['level'] or not any(
            _medicine_phrase_matches(source_text, alias) for alias in medicine['aliases']
        ):
            continue
        catalog_identity = f"{medicine['name']} {medicine['components']}"
        for rule in rules:
            if any(_medicine_phrase_matches(catalog_identity, alias) for alias in rule['aliases']):
                matched_rule_names.add(rule['name'])

    evidence = ' '.join(str(structured_json.get(field) or '') for field in (
        'diagnosis', 'complaints', 'chief_complaints', 'findings', 'clinical_findings',
        'major_diagnostic_findings', 'investigation_finding_in_details',
        'all_investigation_reports', 'deranged_investigation', 'query_reason',
    )).lower()
    antibiotic_count = len(_documented_antibiotics(medicine_text, high_end_signal))
    matched: list[dict] = []
    flagged: list[dict] = []
    for rule in rules:
        if rule['name'] not in matched_rule_names:
            continue
        supported, support_evidence = _antibiotic_support(rule['name'], evidence, antibiotic_count)
        result = {
            'medicine': rule['name'],
            'scrutiny_level': rule['level'],
            'supported': supported,
            'support_evidence': support_evidence,
            'flag_condition': rule['flag_condition'],
        }
        matched.append(result)
        if not supported:
            flagged.append(result)

    return {'matched': matched, 'flagged': flagged, 'catalog_used': bool(rows)}


def _supports_diagnosis(value: str) -> bool:
    text = str(value or '').strip()
    if not text:
        return False
    return not re.search(
        r'\b(?:within normal limits?|normal study|vitals? stable|hemodynamically stable|no significant abnormality|no abnormality|nad)\b',
        text,
        flags=re.IGNORECASE,
    )


def _missing_record_label(value: str) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip(' -;,.')
    text = re.sub(
        r'\s+(?:is|are|was|were)?\s*not\s+(?:submitted|provided|available|documented|attached)\b.*$',
        '',
        text,
        flags=re.IGNORECASE,
    ).strip(' -;,.')
    text = re.sub(r'\s+(?:is|are)\s+missing\b.*$', '', text, flags=re.IGNORECASE).strip(' -;,.')
    return text


def generate_medical_legal_conclusion(structured_json: dict) -> str:
    """Build a concise, evidence-led conclusion using the ML-selected verdict."""
    diagnosis = _conclusion_clip(_conclusion_value(structured_json, 'diagnosis'), 180)
    findings = _conclusion_clip(_conclusion_value(
        structured_json, 'major_diagnostic_finding', 'major_diagnostic_findings', 'findings', 'clinical_findings'
    ), 240)
    deranged = _conclusion_clip(_conclusion_value(structured_json, 'deranged_investigation'), 240)
    investigations = _conclusion_clip(_conclusion_value(
        structured_json, 'investigation_finding_in_details', 'all_investigation_reports', 'investigation_reports'
    ), 260)
    medicines = _conclusion_clip(_conclusion_value(
        structured_json, 'medicine_used', 'medicines', 'treatment_medicines'
    ), 260)
    procedure = _conclusion_clip(_conclusion_value(
        structured_json, 'procedure_or_surgery', 'procedure', 'surgery', 'procedure_performed'
    ), 220)
    query_reason = _conclusion_clip(_conclusion_value(structured_json, 'query_reason'), 220)
    decision_reason = _conclusion_clip(_conclusion_value(structured_json, 'decision_reason'), 260)
    registration = _conclusion_value(
        structured_json,
        'treating_doctor_registration_number',
        'doctor_registration_number',
        'registration_number',
    )
    high_end_signal = _conclusion_value(structured_json, 'high_end_antibiotic_for_rejection')
    recommendation_raw = _conclusion_value(structured_json, 'recommendation', 'final_recommendation').upper()
    if any(token in recommendation_raw for token in ('REJECT', 'INADMISSIBLE', 'NOT JUSTIFIED')):
        recommendation = 'REJECT'
    elif any(token in recommendation_raw for token in ('APPROVE', 'ADMISSIBLE', 'JUSTIFIED')):
        recommendation = 'APPROVE'
    else:
        recommendation = 'QUERY'

    diagnosis_label = diagnosis or 'the stated diagnosis'
    specific_investigation = deranged if _has_specific_investigation(deranged) else (
        investigations if _has_specific_investigation(investigations) else ''
    )
    diagnosis_supported = bool(
        diagnosis
        and (_supports_diagnosis(findings) or _supports_diagnosis(specific_investigation))
    )
    support_text = 'support' if diagnosis_supported else 'do not adequately support'
    correlation_text = 'correlate with' if diagnosis_supported and specific_investigation else 'do not adequately establish correlation with'

    admission_raw = _conclusion_value(structured_json, 'admission_required', 'hospitalization_justified').upper()
    if 'NOT JUSTIFIED' in admission_raw or recommendation == 'REJECT':
        admission_text = 'not justified'
    elif 'JUSTIFIED' in admission_raw or recommendation == 'APPROVE':
        admission_text = 'justified'
    else:
        admission_text = 'not fully assessable'
    severity_text = findings or 'the limited documented clinical severity and findings'

    investigation_text = specific_investigation or 'no specific abnormal or diagnosis-supporting finding'
    treatment_parts = [value for value in (procedure, medicines) if value]
    treatment_text = '; '.join(treatment_parts) if treatment_parts else 'no sufficiently detailed treatment record'
    treatment_assessment = 'appropriate' if recommendation == 'APPROVE' and treatment_parts else 'not adequately justified'

    scrutiny = structured_json.get('antibiotic_scrutiny') if isinstance(structured_json.get('antibiotic_scrutiny'), dict) else {}
    scrutiny_matches = scrutiny.get('matched') if isinstance(scrutiny.get('matched'), list) else []
    scrutiny_flags = scrutiny.get('flagged') if isinstance(scrutiny.get('flagged'), list) else []
    antibiotics = _documented_antibiotics(medicines, high_end_signal)
    antibiotic_evidence = ' '.join((diagnosis, findings, deranged, investigations, query_reason)).lower()
    has_antibiotic_support = bool(re.search(
        r'\b(?:culture|sensitivity|sepsis|septic|resistan\w*|deteriorat\w*|bacteremia|positive blood culture)\b',
        antibiotic_evidence,
    )) and not bool(re.search(
        r'\b(?:no|not|without|missing|unavailable|absent)\b.{0,40}\b(?:culture|sensitivity|sepsis|resistan\w*|deteriorat\w*)\b|\b(?:culture|sensitivity)\b.{0,40}\b(?:not submitted|not provided|missing|unavailable|absent)\b',
        antibiotic_evidence,
    ))
    antibiotic_sentence = ''
    if scrutiny_flags:
        flag_labels = ', '.join(
            f"{item.get('medicine')} ({item.get('scrutiny_level')})"
            for item in scrutiny_flags[:4]
        )
        flag_reasons = '; '.join(
            str(item.get('flag_condition') or '').strip()
            for item in scrutiny_flags[:2]
            if str(item.get('flag_condition') or '').strip()
        )
        antibiotic_sentence = f" Antibiotic scrutiny flags {flag_labels}"
        antibiotic_sentence += f"; the applicable concern is: {flag_reasons}." if flag_reasons else ' due to missing supporting evidence.'
        treatment_assessment = 'not adequately justified'
    elif scrutiny_matches:
        supported_labels = ', '.join(
            f"{item.get('medicine')} ({item.get('scrutiny_level')})"
            for item in scrutiny_matches[:4]
        )
        antibiotic_sentence = f" Antibiotic scrutiny found documented support for {supported_labels}."
    elif antibiotics:
        antibiotic_label = ', '.join(antibiotics[:4])
        if len(antibiotics) > 1 or high_end_signal:
            if has_antibiotic_support:
                antibiotic_sentence = f" Use of {antibiotic_label} is supported by documented culture, sepsis, resistance, or deterioration evidence."
            else:
                antibiotic_sentence = f" Specifically, high-end or multiple antibiotic use ({antibiotic_label}) lacks documented culture/sensitivity, sepsis, resistant infection, or clinical deterioration evidence."

    registration_available = bool(
        registration
        and registration.strip().lower() not in {'-', 'na', 'n/a', 'none', 'nil', 'null', 'not available', 'not documented'}
        and not re.fullmatch(r'\d{2}[-/]\d{2}[-/]\d{4}|\d{4}[-/]\d{2}[-/]\d{2}', registration.strip())
    )
    registration_text = 'available' if registration_available else 'not available'

    missing_records: list[str] = []
    if not findings:
        missing_records.append('complete clinical findings')
    if not specific_investigation:
        missing_records.append('supporting investigation reports')
    if not treatment_parts:
        missing_records.append('treatment chart')
    if not registration_available:
        missing_records.append("the treating doctor's registration")
    if query_reason:
        query_record = _missing_record_label(query_reason)
        if query_record:
            missing_records.insert(0, query_record)
    if scrutiny_flags:
        missing_records.insert(0, 'antibiotic indication/culture and sensitivity justification')
    verification_text = ', '.join(missing_records[:4]) or 'original records and final bill details'

    los_text = _length_of_stay_text(structured_json)
    if recommendation == 'APPROVE':
        final_statement = 'Therefore, the claim is payable, subject to policy terms and bill verification.'
    elif recommendation == 'REJECT':
        rejection_basis = decision_reason or 'the documented clinical/admissibility concern'
        final_statement = f'Therefore, the claim is recommended for rejection because {rejection_basis}, subject to policy terms and final medical review.'
    else:
        requested_records = query_reason or 'Please provide relevant clinical documents supporting diagnosis, treatment, and admission.'
        final_statement = f'Therefore, the claim remains under query. {requested_records}'
    conclusion = (
        f"Conclusion: The available records {support_text} the diagnosis of {diagnosis_label}. "
        f"The {los_text} hospitalization is {admission_text} considering {severity_text}. "
        f"Investigations show {investigation_text}, which {correlation_text} the diagnosis. "
        f"Treatment with {treatment_text} is {treatment_assessment}.{antibiotic_sentence} "
        f"The treating doctor's registration is {registration_text}. "
        f"{final_statement}"
    )
    return re.sub(r'\s+', ' ', conclusion).strip()


def normalize_report_recommendation(value) -> str:
    normalized = str(value or '').strip().upper().replace('-', '_').replace(' ', '_')
    if any(token in normalized for token in ('REJECT', 'INADMISSIBLE', 'NOT_JUSTIFIED')):
        return 'REJECT'
    if any(token in normalized for token in ('APPROVE', 'ADMISSIBLE', 'JUSTIFIED', 'PAYABLE')):
        return 'APPROVE'
    return 'QUERY'


def _is_meaningful_report_value(value) -> bool:
    text = re.sub(r'\s+', ' ', str(value or '')).strip().lower()
    if text in {'', '-', 'na', 'n/a', 'none', 'nil', 'null', 'not available', 'not documented', 'not detailed'}:
        return False
    return not bool(re.fullmatch(
        r'no (?:specific |relevant |date-wise )?(?:investigation|clinical|treatment|medicine|finding|report)s?(?: values?)?(?: available| documented| found)?[.]*',
        text,
    ))


def _length_of_stay_days(structured_json: dict) -> float | None:
    raw = _conclusion_value(structured_json, 'length_of_stay_days', 'length_of_stay', 'los_days')
    match = re.search(r'\d+(?:\.\d+)?', raw)
    if match:
        return float(match.group(0))
    doa = _conclusion_value(structured_json, 'doa', 'admission_date', 'date_of_admission')
    dod = _conclusion_value(structured_json, 'dod', 'discharge_date', 'date_of_discharge')
    for date_format in ('%d-%m-%Y', '%d/%m/%Y', '%Y-%m-%d'):
        try:
            return float(max(0, (datetime.strptime(dod[:10], date_format) - datetime.strptime(doa[:10], date_format)).days))
        except (TypeError, ValueError):
            continue
    return None


def _specific_query_reason(value) -> str:
    text = re.sub(r'\s+', ' ', str(value or '')).strip(' -;,.')
    if text.lower() in {
        '', 'query', 'under query', 'need more evidence', 'insufficient documents',
        'documents required', 'relevant documents required', 'clarification required',
    }:
        return ''
    return _missing_record_label(text) or text


def resolve_report_decision(structured_json: dict, candidate_recommendation, antibiotic_scrutiny: dict) -> dict:
    """Require a clinical reason for rejection and named deficiencies for every query."""
    candidate = normalize_report_recommendation(candidate_recommendation)
    diagnosis = _conclusion_value(structured_json, 'diagnosis')
    findings = _conclusion_value(
        structured_json, 'major_diagnostic_finding', 'major_diagnostic_findings', 'findings', 'clinical_findings'
    )
    deranged = _conclusion_value(structured_json, 'deranged_investigation')
    investigation_details = _conclusion_value(
        structured_json, 'investigation_finding_in_details', 'all_investigation_reports', 'investigation_reports'
    )
    investigations = deranged if _has_specific_investigation(deranged) else investigation_details
    medicines = _conclusion_value(structured_json, 'medicine_used', 'medicines', 'treatment_medicines')
    procedure = _conclusion_value(
        structured_json, 'procedure_or_surgery', 'procedure', 'surgery', 'procedure_performed'
    )
    admission = _conclusion_value(structured_json, 'admission_required', 'hospitalization_justified')
    explicit_reason = _specific_query_reason(_conclusion_value(structured_json, 'query_reason'))
    scrutiny_flags = antibiotic_scrutiny.get('flagged') if isinstance(antibiotic_scrutiny.get('flagged'), list) else []

    diagnosis_present = _is_meaningful_report_value(diagnosis)
    findings_present = _is_meaningful_report_value(findings)
    investigations_present = _has_specific_investigation(investigations)
    treatment_present = _is_meaningful_report_value(medicines) or _is_meaningful_report_value(procedure)
    los_days = _length_of_stay_days(structured_json)
    routine_los = los_days is not None and 0 <= los_days <= 7

    rejection_reason = ''
    if 'NOT JUSTIFIED' in admission.upper():
        rejection_reason = 'the medical necessity of inpatient admission is not supported by the submitted records'
    elif candidate == 'REJECT' and explicit_reason and re.search(
        r'\b(?:policy exclusion|not covered|fraud|forg|mismatch|non[- ]disclosure|inadmissible|not justified)\b',
        explicit_reason,
        flags=re.IGNORECASE,
    ):
        rejection_reason = explicit_reason
    if rejection_reason:
        return {'recommendation': 'REJECT', 'query_reason': '', 'decision_reason': rejection_reason,
                'evidence_guardrail': 'documented_rejection_reason'}

    deficiencies: list[str] = []
    if explicit_reason and candidate == 'QUERY':
        deficiencies.append(explicit_reason)
    if scrutiny_flags:
        deficiencies.append('antibiotic indication with culture/sensitivity or resistant-infection justification')
    if not diagnosis_present:
        deficiencies.append('final diagnosis and detailed discharge summary')
    if not findings_present:
        deficiencies.append('admission notes and documented clinical examination findings')
    if not investigations_present:
        deficiencies.append('investigation reports supporting the diagnosis')
    if not treatment_present:
        deficiencies.append('treatment/medication chart and discharge summary')

    if (
        diagnosis_present and findings_present and investigations_present and treatment_present
        and routine_los and not scrutiny_flags and not deficiencies
    ):
        return {
            'recommendation': 'APPROVE',
            'query_reason': '',
            'decision_reason': 'diagnosis is supported by clinical findings and investigations, LOS is within the routine range, and no adverse evidence is documented',
            'evidence_guardrail': 'supported_payable_claim',
        }

    unique_deficiencies: list[str] = []
    for deficiency in deficiencies:
        clean = re.sub(r'\s+', ' ', str(deficiency or '')).strip(' -;,.')
        if clean and clean.lower() not in {item.lower() for item in unique_deficiencies}:
            unique_deficiencies.append(clean)
    if not unique_deficiencies:
        unique_deficiencies.append('relevant clinical documents supporting diagnosis, treatment, and medical necessity of admission')
    query_reason = 'Please provide ' + '; '.join(unique_deficiencies[:4]) + '.'
    return {'recommendation': 'QUERY', 'query_reason': query_reason, 'decision_reason': query_reason,
            'evidence_guardrail': 'specific_document_deficiency'}


def auto_generate_report(cur, claim_id: str, structured_json: dict):
    """Auto-generate medical report for claim with ML-based recommendations."""
    try:
        from datetime import datetime as dt

        # Get ensemble ML prediction combining XGBoost + Naive Bayes
        ensemble_result = predict_with_ensemble(structured_json)
        ml_recommendation = ensemble_result.get('recommendation')
        ml_confidence = ensemble_result.get('confidence', 0.0)
        decision_source_ensemble = ensemble_result.get('decision_source', 'ensemble')
        models_used = ensemble_result.get('models_used', [])
        ensemble_reasoning = ensemble_result.get('reasoning', '')
        models_agreement = ensemble_result.get('agreement', False)

        # Fallback to original predict_claim if ensemble unavailable
        ml_result = None
        ml_probabilities = {}
        ml_top_signals = []
        ml_model_version = None
        ml_training_examples = 0

        if not ml_recommendation:
            logger.warning(f'Ensemble prediction failed for {claim_id}, falling back to single model')
            ml_result = predict_claim(structured_json)
            if ml_result:
                ml_recommendation = ml_result.get('recommendation')
                ml_confidence = ml_result.get('confidence', 0.0)
                ml_probabilities = ml_result.get('probabilities', {})
                ml_top_signals = ml_result.get('top_signals', [])
                ml_model_version = ml_result.get('model_version')
                ml_training_examples = int(ml_result.get('training_examples') or 0)
                decision_source_ensemble = 'single_model_fallback'
                models_used = ['naive_bayes']
        else:
            logger.info(
                f'📊 Ensemble prediction for {claim_id}: {ml_recommendation} ({ml_confidence:.2%}) '
                f'[{", ".join(models_used)}] {"✅ agreement" if models_agreement else "⚠️ disagreement"}'
            )

        # Get Gemini recommendation (fallback)
        gemini_recommendation = structured_json.get('recommendation', 'need_more_evidence')

        # Use the trained model when it meets the confidence floor; Gemini is only a fallback.
        ml_is_confident = bool(
            ml_recommendation
            and ml_confidence >= ML_RECOMMENDATION_MIN_CONFIDENCE
        )
        final_recommendation = ml_recommendation if ml_is_confident else gemini_recommendation
        decision_source = 'ml_ensemble' if ml_is_confident else 'gemini_fallback'
        if not ml_is_confident:
            decision_source = decision_source_ensemble if ml_recommendation else 'gemini_fallback'

        if ml_recommendation:
            logger.info(
                f'Recommendations - ML (ensemble): {ml_recommendation} ({ml_confidence:.2%}) '
                f'| Gemini: {gemini_recommendation} | Final: {final_recommendation} '
                f'| Reasoning: {ensemble_reasoning[:100]}...' if len(ensemble_reasoning) > 100 else f'| Reasoning: {ensemble_reasoning}'
            )

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
        chief_complaints = _conclusion_value(
            structured_json, 'chief_complaints', 'complaints', 'chief_complaints_at_admission'
        ) or '-'
        major_findings = _conclusion_value(
            structured_json, 'major_diagnostic_findings', 'major_diagnostic_finding', 'findings', 'clinical_findings'
        ) or '-'
        alcoholism_history = structured_json.get('alcoholism_history', 'NAD')
        clinical_findings = _conclusion_value(
            structured_json, 'clinical_findings', 'findings', 'major_diagnostic_finding', 'major_diagnostic_findings'
        ) or '-'
        investigation_reports = structured_json.get('all_investigation_reports', structured_json.get('investigation_reports', '-'))
        investigation_details = structured_json.get('investigation_finding_in_details', 'Not detailed')
        datewise_investigations = structured_json.get('date_wise_investigation_reports', '-')
        deranged_investigation = structured_json.get('deranged_investigation', 'No deranged investigation values found.')
        daily_tpr = structured_json.get('daily_tpr_chart_min_max', structured_json.get('daily_tpr_chart', '-'))
        medicine_used = structured_json.get('medicine_used', '-')
        claimed_amount = report_field_text(
            structured_json.get('claimed_amount', structured_json.get('claim_amount', '-'))
        ).strip() or '-'
        claimed_amount_db = claimed_amount[:50]

        # The evidence narrative is deterministic; its verdict is selected by the trained ML model.
        antibiotic_scrutiny = assess_antibiotic_scrutiny(cur, structured_json)
        decision = resolve_report_decision(structured_json, final_recommendation, antibiotic_scrutiny)
        recommendation = decision['recommendation']
        query_reason = decision['query_reason']
        decision_reason = decision['decision_reason']
        decision_source += '+' + decision['evidence_guardrail']
        conclusion_payload = dict(structured_json)
        conclusion_payload['recommendation'] = recommendation
        conclusion_payload['query_reason'] = query_reason
        conclusion_payload['decision_reason'] = decision_reason
        conclusion_payload['antibiotic_scrutiny'] = antibiotic_scrutiny
        conclusion = generate_medical_legal_conclusion(conclusion_payload)
        admission_display = {
            'APPROVE': 'Justified',
            'REJECT': 'Not Justified',
            'QUERY': 'Pending requested documents',
        }[recommendation]
        recommendation_text = {
            'APPROVE': 'The claim is payable',
            'REJECT': 'The claim is recommended for rejection',
            'QUERY': query_reason,
        }[recommendation]
        ml_metadata = {
            'available': bool(ml_result),
            'used_for_decision': ml_is_confident,
            'recommendation': ml_recommendation,
            'confidence': ml_confidence,
            'probabilities': ml_probabilities,
            'top_signals': ml_top_signals,
            'model_version': ml_model_version,
            'training_examples': ml_training_examples,
            'minimum_confidence': ML_RECOMMENDATION_MIN_CONFIDENCE,
            'decision_source': decision_source,
        }

        cur.execute('''
            UPDATE claim_structured_data
            SET conclusion = %s,
                recommendation = %s,
                raw_payload = COALESCE(raw_payload, '{}'::jsonb) || %s::jsonb,
                updated_at = NOW()
            WHERE claim_id = %s
        ''', (
            conclusion,
            recommendation,
            json.dumps({
                'conclusion': conclusion,
                'recommendation': recommendation,
                'ml_prediction': ml_metadata,
                'antibiotic_scrutiny': antibiotic_scrutiny,
                'query_reason': query_reason,
                'decision_reason': decision_reason,
            }),
            claim_id,
        ))

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
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold; width: 25%;">Admission Required</td><td style="padding: 6px; border: 1px solid #999;">{admission_display}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">Final Recommendation</td><td style="padding: 6px; border: 1px solid #999;">{recommendation_text}</td></tr>
<tr style="background-color: #f5f0f0;"><td style="padding: 6px; border: 1px solid #999; font-weight: bold; vertical-align: top;">Conclusion</td><td style="padding: 6px; border: 1px solid #999; white-space: pre-wrap;">{conclusion}</td></tr>
<tr><td style="padding: 6px; border: 1px solid #999; font-weight: bold;">Recommendation</td><td style="padding: 6px; border: 1px solid #999; white-space: pre-wrap;">{recommendation_text}</td></tr>
</tbody>
</table>

<hr style="margin-top: 15px;">
<div style="background-color: #f0f8ff; padding: 8px; margin-top: 10px; border-radius: 4px; font-size: 10px; color: #333;">
<strong>AI Analysis:</strong>
<br/>Decision Source: {decision_source}<br/>ML Model: {ml_recommendation or 'unavailable'} (confidence: {ml_confidence:.1%}, version: {ml_model_version or '-'})<br/>Gemini fallback: {gemini_recommendation}<br/>
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

RECOMMENDATION: {recommendation_text}

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
            claimed_amount_db,
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
            reconcile_no_extraction_claims()
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
                included_documents = 0
                for row in ocr_rows:
                    raw_response, extracted_entities, model_name, filename = row
                    exclusion_reason = excluded_document_reason(filename)
                    if exclusion_reason:
                        logger.info(
                            'Ignoring excluded OCR source during structuring: %s (%s)',
                            filename,
                            exclusion_reason,
                        )
                        continue
                    source_text = legacy_extraction_text(
                        raw_response,
                        extracted_entities,
                        model_name,
                    )
                    if source_text:
                        combined_texts.append(f'--- Document: {filename} ---\n{source_text}')
                        included_documents += 1

                ocr_text = '\n\n'.join(combined_texts)
                logger.info(
                    'Combined OCR from %s/%s included documents: %s chars for claim %s',
                    included_documents,
                    len(ocr_rows),
                    len(ocr_text),
                    claim_id,
                )

                if not ocr_text.strip():
                    raise ValueError(f'Extracted rows contain no OCR text for claim {claim_id}')

                # Extract structured data with Gemini
                structured_json = extract_structured_data_gemini(ocr_text, claim_id)

                if not structured_json:
                    raise RuntimeError(f'Failed to extract structured data for claim {claim_id}')
                meaningful_extraction = has_meaningful_extraction(structured_json)

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

                if not meaningful_extraction:
                    queued_documents = queue_textract_recovery(str(claim_id))
                    r.zrem(STAGE2_RETRY_SET, claim_id)
                    r.hdel(STAGE2_RETRY_PAYLOADS, claim_id)
                    logger.warning(
                        'Claim %s produced no meaningful extraction; queued %s Textract documents',
                        claim_id,
                        queued_documents,
                    )
                    continue

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

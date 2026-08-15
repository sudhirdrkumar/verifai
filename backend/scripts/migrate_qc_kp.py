import argparse
import json
import mimetypes
import sys
from pathlib import Path
from typing import Any

import psycopg
import pymysql
from pymysql.cursors import DictCursor
from psycopg import OperationalError

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings


VALID_ROLES = {"super_admin", "doctor", "user"}
VALID_CLAIM_STATUS = {
    "ready_for_assignment",
    "waiting_for_documents",
    "in_review",
    "needs_qc",
    "completed",
    "withdrawn",
}


def parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return default
        try:
            parsed = json.loads(raw)
            return parsed
        except Exception:
            return default
    return default


def normalize_role(raw: Any) -> str:
    role = str(raw or "user").strip().lower()
    return role if role in VALID_ROLES else "user"


def map_claim_status(final_status: Any, document_status: Any, assigned_doctor: Any) -> str:
    final_status = str(final_status or "").strip().lower()
    document_status = str(document_status or "").strip().lower()

    if final_status == "completed":
        return "completed"
    if document_status == "pending":
        return "waiting_for_documents"
    if assigned_doctor:
        return "in_review"
    return "ready_for_assignment"


def map_decision(admission_required: Any) -> tuple[str, str, bool, int]:
    ar = str(admission_required or "uncertain").strip().lower()
    if ar == "yes":
        return "approve", "auto_approve_queue", False, 4
    if ar == "no":
        return "reject", "reject_queue", True, 1
    return "manual_review", "manual_review_queue", True, 3


def connect_legacy_mysql():
    return pymysql.connect(
        host=settings.legacy_db_host,
        port=settings.legacy_db_port,
        user=settings.legacy_db_user,
        password=settings.legacy_db_pass,
        database=settings.legacy_db_name,
        charset="utf8mb4",
        cursorclass=DictCursor,
        connect_timeout=10,
        read_timeout=30,
        write_timeout=30,
        autocommit=True,
    )


def connect_modern_postgres():
    return psycopg.connect(settings.psycopg_database_uri)


def fetch_claim_uuid_map(pg_conn) -> dict[str, str]:
    out: dict[str, str] = {}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id::text, external_claim_id FROM claims")
        for claim_id, external_claim_id in cur.fetchall():
            out[str(external_claim_id)] = str(claim_id)
    return out


def migrate_users(mysql_conn, pg_conn) -> int:
    with mysql_conn.cursor() as cur:
        cur.execute("SELECT id, username, password_hash, role, created_at FROM users")
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            role = normalize_role(row.get("role"))
            cur.execute(
                """
                INSERT INTO users (legacy_user_id, username, password_hash, role, is_active, created_at, updated_at)
                VALUES (%s, %s, %s, %s, TRUE, %s, %s)
                ON CONFLICT (username) DO UPDATE
                SET legacy_user_id = EXCLUDED.legacy_user_id,
                    password_hash = EXCLUDED.password_hash,
                    role = EXCLUDED.role,
                    is_active = TRUE
                """,
                (
                    int(row.get("id")),
                    str(row.get("username") or "").strip(),
                    str(row.get("password_hash") or ""),
                    role,
                    row.get("created_at"),
                    row.get("created_at"),
                ),
            )
            count += 1
    pg_conn.commit()
    return count


def migrate_auth_logs(mysql_conn, pg_conn) -> int:
    with mysql_conn.cursor() as cur:
        cur.execute("SELECT id, user_id, username, role, ip_address, user_agent, success, created_at FROM auth_logs")
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            role = normalize_role(row.get("role"))
            cur.execute(
                """
                INSERT INTO auth_logs (legacy_auth_log_id, user_id, username, role, ip_address, user_agent, success, created_at)
                VALUES (
                    %s,
                    (SELECT id FROM users WHERE legacy_user_id = %s),
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT (legacy_auth_log_id) DO NOTHING
                """,
                (
                    int(row.get("id")),
                    int(row.get("user_id")) if row.get("user_id") is not None else None,
                    str(row.get("username") or ""),
                    role,
                    str(row.get("ip_address") or "unknown")[:45],
                    str(row.get("user_agent") or "unknown")[:255],
                    bool(row.get("success")),
                    row.get("created_at"),
                ),
            )
            count += 1
    pg_conn.commit()
    return count


def migrate_claims(mysql_conn, pg_conn) -> int:
    with mysql_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                e.claim_id,
                e.benef_name,
                e.policy_number,
                e.final_status,
                e.document_status,
                e.claim_type,
                e.policy_type,
                e.primary_icd_group,
                e.treatment_type,
                e.hospital_name,
                e.claim_amount,
                e.created_at,
                e.updated_at,
                ca.doctor_username
            FROM excel_case_uploads e
            LEFT JOIN case_assignments ca ON ca.claim_id = e.claim_id
            """
        )
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            external_claim_id = str(row.get("claim_id") or "").strip()
            if not external_claim_id:
                continue

            assigned_doctor = str(row.get("doctor_username") or "").strip() or None
            status = map_claim_status(row.get("final_status"), row.get("document_status"), assigned_doctor)
            if status not in VALID_CLAIM_STATUS:
                status = "waiting_for_documents"

            tags = [
                str(v)
                for v in [
                    row.get("claim_type"),
                    row.get("policy_type"),
                    row.get("primary_icd_group"),
                    row.get("treatment_type"),
                    row.get("hospital_name"),
                ]
                if v is not None and str(v).strip()
            ]

            cur.execute(
                """
                INSERT INTO claims (
                    external_claim_id,
                    patient_name,
                    patient_identifier,
                    status,
                    assigned_doctor_id,
                    priority,
                    source_channel,
                    tags,
                    created_at,
                    updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                ON CONFLICT (external_claim_id) DO UPDATE
                SET patient_name = EXCLUDED.patient_name,
                    patient_identifier = EXCLUDED.patient_identifier,
                    status = EXCLUDED.status,
                    assigned_doctor_id = EXCLUDED.assigned_doctor_id,
                    priority = EXCLUDED.priority,
                    source_channel = EXCLUDED.source_channel,
                    tags = EXCLUDED.tags,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    external_claim_id,
                    str(row.get("benef_name") or "").strip() or None,
                    str(row.get("policy_number") or "").strip() or None,
                    status,
                    assigned_doctor,
                    3,
                    "legacy_qc_kp",
                    json.dumps(tags),
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            count += 1
    pg_conn.commit()
    return count


def migrate_documents(mysql_conn, pg_conn) -> int:
    claim_map = fetch_claim_uuid_map(pg_conn)

    with mysql_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                claim_id,
                original_filename,
                stored_filename,
                local_path,
                s3_bucket,
                s3_object_key,
                s3_url,
                uploaded_by_username,
                uploaded_at
            FROM case_documents
            """
        )
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            external_claim_id = str(row.get("claim_id") or "").strip()
            claim_uuid = claim_map.get(external_claim_id)
            if claim_uuid is None:
                continue

            original_name = str(row.get("original_filename") or "document").strip() or "document"
            guessed_mime, _ = mimetypes.guess_type(original_name)
            storage_key = (
                str(row.get("s3_object_key") or "").strip()
                or str(row.get("stored_filename") or "").strip()
                or str(row.get("local_path") or "").strip()
                or f"legacy/{row.get('id')}"
            )
            metadata = {
                "legacy_local_path": row.get("local_path"),
                "legacy_s3_bucket": row.get("s3_bucket"),
                "legacy_s3_url": row.get("s3_url"),
                "legacy_claim_id": external_claim_id,
            }

            cur.execute(
                """
                INSERT INTO claim_documents (
                    legacy_document_id,
                    claim_id,
                    storage_key,
                    file_name,
                    mime_type,
                    parse_status,
                    retention_class,
                    uploaded_by,
                    uploaded_at,
                    metadata
                )
                VALUES (%s, %s::uuid, %s, %s, %s, 'succeeded', 'standard', %s, %s, %s::jsonb)
                ON CONFLICT (legacy_document_id) DO UPDATE
                SET claim_id = EXCLUDED.claim_id,
                    storage_key = EXCLUDED.storage_key,
                    file_name = EXCLUDED.file_name,
                    mime_type = EXCLUDED.mime_type,
                    uploaded_by = EXCLUDED.uploaded_by,
                    uploaded_at = EXCLUDED.uploaded_at,
                    metadata = EXCLUDED.metadata
                """,
                (
                    int(row.get("id")),
                    claim_uuid,
                    storage_key,
                    original_name,
                    guessed_mime or "application/octet-stream",
                    str(row.get("uploaded_by_username") or "legacy_migration"),
                    row.get("uploaded_at"),
                    json.dumps(metadata),
                ),
            )
            count += 1

    pg_conn.commit()
    return count


def migrate_analysis_results(mysql_conn, pg_conn) -> int:
    claim_map = fetch_claim_uuid_map(pg_conn)

    with mysql_conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                id,
                claim_id,
                doctor_username,
                model_name,
                admission_required,
                confidence,
                rationale,
                evidence_json,
                missing_information_json,
                disclaimer,
                raw_response_json,
                report_html,
                created_at
            FROM openai_analysis_results
            """
        )
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            external_claim_id = str(row.get("claim_id") or "").strip()
            claim_uuid = claim_map.get(external_claim_id)
            if claim_uuid is None:
                continue

            recommendation, route_target, manual_review_required, review_priority = map_decision(row.get("admission_required"))
            confidence_raw = row.get("confidence")
            qc_risk = None
            try:
                if confidence_raw is not None:
                    qc_risk = max(0.0, min(1.0, float(confidence_raw) / 100.0))
            except Exception:
                qc_risk = None

            evidence = parse_json(row.get("evidence_json"), [])
            missing_info = parse_json(row.get("missing_information_json"), [])
            raw_response = parse_json(row.get("raw_response_json"), {})

            decision_payload = {
                "legacy_analysis_id": row.get("id"),
                "legacy_claim_id": external_claim_id,
                "admission_required": row.get("admission_required"),
                "rationale": row.get("rationale"),
                "disclaimer": row.get("disclaimer"),
                "report_html": row.get("report_html"),
                "raw_response": raw_response,
                "evidence": evidence,
                "missing_information": missing_info,
            }

            consistency_checks = missing_info if isinstance(missing_info, list) else [missing_info]
            rule_hits = evidence if isinstance(evidence, list) else [evidence]

            cur.execute(
                """
                INSERT INTO decision_results (
                    legacy_analysis_id,
                    claim_id,
                    rule_version,
                    model_version,
                    qc_risk_score,
                    consistency_checks,
                    rule_hits,
                    explanation_summary,
                    recommendation,
                    route_target,
                    manual_review_required,
                    review_priority,
                    decision_payload,
                    generated_by,
                    generated_at,
                    is_active
                )
                VALUES (
                    %s,
                    %s::uuid,
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    %s::jsonb,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s::jsonb,
                    %s,
                    %s,
                    FALSE
                )
                ON CONFLICT (legacy_analysis_id) DO UPDATE
                SET claim_id = EXCLUDED.claim_id,
                    model_version = EXCLUDED.model_version,
                    qc_risk_score = EXCLUDED.qc_risk_score,
                    consistency_checks = EXCLUDED.consistency_checks,
                    rule_hits = EXCLUDED.rule_hits,
                    explanation_summary = EXCLUDED.explanation_summary,
                    recommendation = EXCLUDED.recommendation,
                    route_target = EXCLUDED.route_target,
                    manual_review_required = EXCLUDED.manual_review_required,
                    review_priority = EXCLUDED.review_priority,
                    decision_payload = EXCLUDED.decision_payload,
                    generated_by = EXCLUDED.generated_by,
                    generated_at = EXCLUDED.generated_at
                """,
                (
                    int(row.get("id")),
                    claim_uuid,
                    "legacy-qc-kp-openai-v1",
                    str(row.get("model_name") or "legacy-openai"),
                    qc_risk,
                    json.dumps(consistency_checks),
                    json.dumps(rule_hits),
                    str(row.get("rationale") or "")[:4000] or None,
                    recommendation,
                    route_target,
                    manual_review_required,
                    review_priority,
                    json.dumps(decision_payload),
                    str(row.get("doctor_username") or "legacy_qc_kp"),
                    row.get("created_at"),
                ),
            )
            count += 1

    pg_conn.commit()
    return count


def migrate_analysis_jobs(mysql_conn, pg_conn) -> int:
    claim_map = fetch_claim_uuid_map(pg_conn)

    with mysql_conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, claim_id, doctor_username, status, use_raw_files, result_analysis_id, error_message, created_at, updated_at
            FROM openai_analysis_jobs
            """
        )
        rows = cur.fetchall() or []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            external_claim_id = str(row.get("claim_id") or "").strip()
            claim_uuid = claim_map.get(external_claim_id)
            if claim_uuid is None:
                continue

            payload = {
                "legacy_claim_id": external_claim_id,
                "doctor_username": row.get("doctor_username"),
                "status": row.get("status"),
                "use_raw_files": bool(row.get("use_raw_files")),
                "result_analysis_id": row.get("result_analysis_id"),
                "error_message": row.get("error_message"),
                "created_at": str(row.get("created_at")) if row.get("created_at") else None,
                "updated_at": str(row.get("updated_at")) if row.get("updated_at") else None,
            }

            cur.execute(
                """
                INSERT INTO workflow_events (legacy_job_id, claim_id, actor_type, actor_id, event_type, event_payload, occurred_at)
                VALUES (%s, %s::uuid, 'system', %s, %s, %s::jsonb, %s)
                ON CONFLICT (legacy_job_id) DO UPDATE
                SET claim_id = EXCLUDED.claim_id,
                    actor_id = EXCLUDED.actor_id,
                    event_type = EXCLUDED.event_type,
                    event_payload = EXCLUDED.event_payload,
                    occurred_at = EXCLUDED.occurred_at
                """,
                (
                    int(row.get("id")),
                    claim_uuid,
                    str(row.get("doctor_username") or "legacy_qc_kp"),
                    f"legacy_openai_job_{str(row.get('status') or 'unknown').strip().lower()}",
                    json.dumps(payload),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            count += 1

    pg_conn.commit()
    return count


def migrate_checklist_catalog(mysql_conn, pg_conn) -> tuple[int, int]:
    rule_rows = []
    criteria_rows = []

    with mysql_conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT rule_id, name, scope_json, decision, severity, priority, required_evidence_json, is_active, created_at, updated_at
                FROM openai_claim_rules
                """
            )
            rule_rows = cur.fetchall() or []
        except Exception:
            rule_rows = []

        try:
            cur.execute(
                """
                SELECT criteria_id, diagnosis_name, aliases_json, decision, severity, priority, required_evidence_json, is_active, created_at, updated_at
                FROM openai_diagnosis_criteria
                """
            )
            criteria_rows = cur.fetchall() or []
        except Exception:
            criteria_rows = []

    rule_count = 0
    criteria_count = 0

    with pg_conn.cursor() as cur:
        for row in rule_rows:
            cur.execute(
                """
                INSERT INTO openai_claim_rules (
                    rule_id, name, scope_json, decision, severity, priority, required_evidence_json, is_active, created_at, updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (rule_id) DO UPDATE
                SET name = EXCLUDED.name,
                    scope_json = EXCLUDED.scope_json,
                    decision = EXCLUDED.decision,
                    severity = EXCLUDED.severity,
                    priority = EXCLUDED.priority,
                    required_evidence_json = EXCLUDED.required_evidence_json,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(row.get("rule_id") or "").strip().upper(),
                    str(row.get("name") or "").strip(),
                    json.dumps(parse_json(row.get("scope_json"), [])),
                    str(row.get("decision") or "QUERY").strip().upper(),
                    str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                    int(row.get("priority") or 999),
                    json.dumps(parse_json(row.get("required_evidence_json"), [])),
                    bool(row.get("is_active")),
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            rule_count += 1

        for row in criteria_rows:
            cur.execute(
                """
                INSERT INTO openai_diagnosis_criteria (
                    criteria_id, diagnosis_name, aliases_json, decision, severity, priority, required_evidence_json, is_active, created_at, updated_at
                )
                VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s, %s)
                ON CONFLICT (criteria_id) DO UPDATE
                SET diagnosis_name = EXCLUDED.diagnosis_name,
                    aliases_json = EXCLUDED.aliases_json,
                    decision = EXCLUDED.decision,
                    severity = EXCLUDED.severity,
                    priority = EXCLUDED.priority,
                    required_evidence_json = EXCLUDED.required_evidence_json,
                    is_active = EXCLUDED.is_active,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    str(row.get("criteria_id") or "").strip().upper(),
                    str(row.get("diagnosis_name") or "").strip(),
                    json.dumps(parse_json(row.get("aliases_json"), [])),
                    str(row.get("decision") or "QUERY").strip().upper(),
                    str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                    int(row.get("priority") or 999),
                    json.dumps(parse_json(row.get("required_evidence_json"), [])),
                    bool(row.get("is_active")),
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            criteria_count += 1

    pg_conn.commit()
    return rule_count, criteria_count



def migrate_medicines(mysql_conn, pg_conn) -> int:
    with mysql_conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT medicine_key, medicine_name, components, subclassification,
                       is_high_end_antibiotic, source, last_checked_at, created_at, updated_at
                FROM medicine_component_lookup
                """
            )
            rows = cur.fetchall() or []
        except Exception:
            rows = []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            key = str(row.get("medicine_key") or "").strip().lower()
            name = str(row.get("medicine_name") or "").strip()
            components = str(row.get("components") or "").strip()
            if not key or not name or not components:
                continue
            cur.execute(
                """
                INSERT INTO medicine_component_lookup (
                    medicine_key, medicine_name, components, subclassification,
                    is_high_end_antibiotic, source, last_checked_at, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (medicine_key) DO UPDATE
                SET medicine_name = EXCLUDED.medicine_name,
                    components = EXCLUDED.components,
                    subclassification = EXCLUDED.subclassification,
                    is_high_end_antibiotic = EXCLUDED.is_high_end_antibiotic,
                    source = EXCLUDED.source,
                    last_checked_at = EXCLUDED.last_checked_at,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    key,
                    name,
                    components,
                    str(row.get("subclassification") or "").strip(),
                    bool(row.get("is_high_end_antibiotic")),
                    str(row.get("source") or "table").strip() or "table",
                    row.get("last_checked_at"),
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            count += 1

    pg_conn.commit()
    return count


def migrate_rule_suggestions(mysql_conn, pg_conn) -> int:
    with mysql_conn.cursor() as cur:
        try:
            cur.execute(
                """
                SELECT
                    source_analysis_id,
                    claim_id,
                    suggestion_type,
                    target_rule_id,
                    proposed_rule_id,
                    suggested_name,
                    suggested_decision,
                    suggested_conditions,
                    suggested_remark_template,
                    suggested_required_evidence_json,
                    source_context_text,
                    generator_confidence,
                    generator_reasoning,
                    generator_response_json,
                    status,
                    reviewed_by_user_id,
                    reviewed_at,
                    approved_rule_id,
                    created_at,
                    updated_at
                FROM openai_claim_rule_suggestions
                """
            )
            rows = cur.fetchall() or []
        except Exception:
            rows = []

    count = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            claim_id = str(row.get("claim_id") or "").strip()
            if not claim_id:
                continue

            suggested_name = str(row.get("suggested_name") or "").strip()
            suggested_conditions = str(row.get("suggested_conditions") or "").strip()
            if not suggested_name or not suggested_conditions:
                continue

            required_json = parse_json(row.get("suggested_required_evidence_json"), [])
            response_json = parse_json(row.get("generator_response_json"), None)

            cur.execute(
                """
                INSERT INTO openai_claim_rule_suggestions (
                    source_analysis_id,
                    claim_id,
                    suggestion_type,
                    target_rule_id,
                    proposed_rule_id,
                    suggested_name,
                    suggested_decision,
                    suggested_conditions,
                    suggested_remark_template,
                    suggested_required_evidence_json,
                    source_context_text,
                    generator_confidence,
                    generator_reasoning,
                    generator_response_json,
                    status,
                    reviewed_by_user_id,
                    reviewed_at,
                    approved_rule_id,
                    created_at,
                    updated_at
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s, %s, %s::jsonb, %s,
                    (SELECT id FROM users WHERE legacy_user_id = %s),
                    %s, %s, %s, %s
                )
                ON CONFLICT (source_analysis_id) DO UPDATE
                SET claim_id = EXCLUDED.claim_id,
                    suggestion_type = EXCLUDED.suggestion_type,
                    target_rule_id = EXCLUDED.target_rule_id,
                    proposed_rule_id = EXCLUDED.proposed_rule_id,
                    suggested_name = EXCLUDED.suggested_name,
                    suggested_decision = EXCLUDED.suggested_decision,
                    suggested_conditions = EXCLUDED.suggested_conditions,
                    suggested_remark_template = EXCLUDED.suggested_remark_template,
                    suggested_required_evidence_json = EXCLUDED.suggested_required_evidence_json,
                    source_context_text = EXCLUDED.source_context_text,
                    generator_confidence = EXCLUDED.generator_confidence,
                    generator_reasoning = EXCLUDED.generator_reasoning,
                    generator_response_json = EXCLUDED.generator_response_json,
                    status = EXCLUDED.status,
                    reviewed_by_user_id = EXCLUDED.reviewed_by_user_id,
                    reviewed_at = EXCLUDED.reviewed_at,
                    approved_rule_id = EXCLUDED.approved_rule_id,
                    updated_at = EXCLUDED.updated_at
                """,
                (
                    row.get("source_analysis_id"),
                    claim_id,
                    str(row.get("suggestion_type") or "new_rule"),
                    str(row.get("target_rule_id") or "").strip() or None,
                    str(row.get("proposed_rule_id") or "").strip() or None,
                    suggested_name,
                    str(row.get("suggested_decision") or "QUERY").strip().upper(),
                    suggested_conditions,
                    str(row.get("suggested_remark_template") or "").strip() or None,
                    json.dumps(required_json),
                    str(row.get("source_context_text") or "").strip() or None,
                    int(row.get("generator_confidence") or 0),
                    str(row.get("generator_reasoning") or "").strip() or None,
                    json.dumps(response_json) if response_json is not None else None,
                    str(row.get("status") or "pending").strip().lower(),
                    row.get("reviewed_by_user_id"),
                    row.get("reviewed_at"),
                    str(row.get("approved_rule_id") or "").strip() or None,
                    row.get("created_at"),
                    row.get("updated_at") or row.get("created_at"),
                ),
            )
            count += 1

    pg_conn.commit()
    return count

def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate legacy QC-KP MySQL data into modern PostgreSQL")
    parser.add_argument("--skip-auth", action="store_true", help="Skip users and auth_logs migration")
    parser.add_argument("--skip-claims", action="store_true", help="Skip claims/documents/analysis migration")
    parser.add_argument("--skip-checklist", action="store_true", help="Skip checklist catalog migration")
    parser.add_argument("--skip-tools", action="store_true", help="Skip admin tool catalogs migration")
    args = parser.parse_args()

    try:
        mysql_conn = connect_legacy_mysql()
    except Exception as exc:
        print("Could not connect to legacy MySQL (QC-KP).")
        print(f"Connection target: {settings.legacy_db_host}:{settings.legacy_db_port}/{settings.legacy_db_name}")
        raise SystemExit(1) from exc

    try:
        pg_conn = connect_modern_postgres()
    except OperationalError as exc:
        print("Could not connect to PostgreSQL.")
        print(f"Connection target: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
        raise SystemExit(1) from exc

    try:
        print("Starting migration from legacy QC-KP to modern PostgreSQL...")

        if not args.skip_auth:
            users_count = migrate_users(mysql_conn, pg_conn)
            print(f"users migrated: {users_count}")
            auth_logs_count = migrate_auth_logs(mysql_conn, pg_conn)
            print(f"auth_logs migrated: {auth_logs_count}")

        if not args.skip_claims:
            claims_count = migrate_claims(mysql_conn, pg_conn)
            print(f"claims migrated: {claims_count}")
            documents_count = migrate_documents(mysql_conn, pg_conn)
            print(f"claim_documents migrated: {documents_count}")
            decisions_count = migrate_analysis_results(mysql_conn, pg_conn)
            print(f"decision_results migrated: {decisions_count}")
            jobs_count = migrate_analysis_jobs(mysql_conn, pg_conn)
            print(f"workflow_events migrated from jobs: {jobs_count}")

        if not args.skip_checklist:
            rules_count, criteria_count = migrate_checklist_catalog(mysql_conn, pg_conn)
            print(f"checklist rules migrated: {rules_count}")
            print(f"diagnosis criteria migrated: {criteria_count}")

        if not args.skip_tools:
            medicines_count = migrate_medicines(mysql_conn, pg_conn)
            print(f"medicines migrated: {medicines_count}")
            suggestions_count = migrate_rule_suggestions(mysql_conn, pg_conn)
            print(f"rule suggestions migrated: {suggestions_count}")

        print("Migration completed.")
    finally:
        try:
            mysql_conn.close()
        except Exception:
            pass
        try:
            pg_conn.close()
        except Exception:
            pass


if __name__ == "__main__":
    main()




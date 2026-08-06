import json
import re
import threading
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps.auth import require_roles
from app.core.config import settings
from app.db.session import SessionLocal, get_db
from app.schemas.auth import UserRole
from app.schemas.qc_tools import (
    ClaimRuleUpsertRequest,
    DiagnosisCriteriaUpsertRequest,
    LegacyMigrationStartRequest,
    MedicineUpsertRequest,
    SuggestionReviewRequest,
)
from app.services.auth_service import AuthenticatedUser, hash_password
from app.services.analysis_import_service import import_analysis_results_from_rows
from app.services.sql_dump_parser import iter_table_rows_from_sql_dump_bytes
router = APIRouter(prefix="/admin", tags=["admin-tools"])


def _normalize_json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            return [value]
    return []


def _normalize_rule_decision(value: str) -> str:
    v = (value or "QUERY").strip().upper()
    return v if v in {"APPROVE", "QUERY", "REJECT"} else "QUERY"


def _normalize_severity(value: str) -> str:
    v = (value or "SOFT_QUERY").strip().upper()
    return v if v in {"INFO", "SOFT_QUERY", "HARD_REJECT"} else "SOFT_QUERY"


def _medicine_key(name: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", name.lower())).strip()

LEGACY_MIGRATION_LOCK = threading.Lock()
LEGACY_MIGRATION_JOBS: dict[str, dict[str, Any]] = {}
LEGACY_MIGRATION_ACTIVE_JOB_ID: str | None = None
LEGACY_MIGRATION_JOB_ORDER: list[str] = []


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_job_snapshot(job_id: str) -> dict[str, Any] | None:
    with LEGACY_MIGRATION_LOCK:
        job = LEGACY_MIGRATION_JOBS.get(job_id)
        return dict(job) if isinstance(job, dict) else None


def _update_job(job_id: str, **updates: Any) -> None:
    with LEGACY_MIGRATION_LOCK:
        job = LEGACY_MIGRATION_JOBS.get(job_id)
        if not isinstance(job, dict):
            return
        job.update(updates)


def _sanitize_legacy_username(value: str) -> str:
    cleaned = re.sub(r"\s+", "", str(value or "").strip())
    cleaned = re.sub(r"[^A-Za-z0-9_.@-]", "", cleaned)
    return cleaned[:60]


def _normalize_legacy_role(raw_role: str, username: str, doctor_usernames: set[str]) -> str:
    role_key = str(raw_role or "").strip().lower()
    uname = str(username or "").strip().lower()

    if uname in doctor_usernames:
        return UserRole.doctor.value
    if role_key in {"super_admin", "superadmin", "admin"}:
        return UserRole.super_admin.value
    if role_key in {"doctor", "dr", "physician"}:
        return UserRole.doctor.value
    if role_key in {"auditor", "audit", "qa"}:
        return UserRole.auditor.value
    if "audit" in uname:
        return UserRole.auditor.value
    return UserRole.user.value


def _fetch_legacy_sync_json(params: dict[str, Any]) -> dict[str, Any]:
    sync_url = str(settings.teamrightworks_sync_trigger_url or "").strip()
    sync_key = str(settings.teamrightworks_sync_trigger_key or "").strip()
    if not sync_url:
        raise RuntimeError("TEAMRIGHTWORKS_SYNC_TRIGGER_URL is not configured")
    if not sync_key:
        raise RuntimeError("TEAMRIGHTWORKS_SYNC_TRIGGER_KEY is not configured")

    full_params: dict[str, Any] = {"key": sync_key}
    full_params.update(params)

    with httpx.Client(timeout=httpx.Timeout(180.0, connect=20.0), follow_redirects=True) as client:
        response = client.get(sync_url, params=full_params)

    body_text = response.text or ""
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(f"legacy sync returned non-JSON (HTTP {response.status_code})") from exc

    if response.status_code >= 400:
        detail = payload.get("error") if isinstance(payload, dict) else None
        detail_text = str(detail or body_text[:250] or f"HTTP {response.status_code}")
        raise RuntimeError(f"legacy sync failed: {detail_text}")

    if not isinstance(payload, dict):
        raise RuntimeError("legacy sync returned invalid payload")

    return payload


def _reset_claims_to_raw_mode(db: Session, external_claim_ids: list[str]) -> dict[str, int]:
    cleaned_ids: list[str] = []
    seen: set[str] = set()
    for raw in external_claim_ids:
        ext_id = str(raw or "").strip()
        if not ext_id or ext_id in seen:
            continue
        seen.add(ext_id)
        cleaned_ids.append(ext_id)

    stats = {
        "claims_touched": 0,
        "report_versions_deleted": 0,
        "claim_report_uploads_deleted": 0,
        "feedback_labels_deleted": 0,
        "decision_results_deleted": 0,
        "document_extractions_deleted": 0,
        "documents_reset": 0,
    }
    if not cleaned_ids:
        return stats

    for external_claim_id in cleaned_ids:
        claim_row = db.execute(
            text(
                """
                SELECT id
                FROM claims
                WHERE external_claim_id = :external_claim_id
                  AND COALESCE(source_channel, '') = 'teamrightworks.in'
                LIMIT 1
                """
            ),
            {"external_claim_id": external_claim_id},
        ).mappings().first()
        if claim_row is None:
            continue

        claim_id = str(claim_row["id"])
        stats["claims_touched"] += 1
        stats["report_versions_deleted"] += int(
            db.execute(text("DELETE FROM report_versions WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
        )
        stats["claim_report_uploads_deleted"] += int(
            db.execute(text("DELETE FROM claim_report_uploads WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
        )
        stats["feedback_labels_deleted"] += int(
            db.execute(text("DELETE FROM feedback_labels WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
        )
        stats["decision_results_deleted"] += int(
            db.execute(text("DELETE FROM decision_results WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
        )
        stats["document_extractions_deleted"] += int(
            db.execute(text("DELETE FROM document_extractions WHERE claim_id = :claim_id"), {"claim_id": claim_id}).rowcount or 0
        )
        stats["documents_reset"] += int(
            db.execute(
                text(
                    """
                    UPDATE claim_documents
                    SET parse_status = 'pending',
                        parsed_at = NULL
                    WHERE claim_id = :claim_id
                    """
                ),
                {"claim_id": claim_id},
            ).rowcount
            or 0
        )

    return stats


def _collect_candidate_usernames(db: Session) -> tuple[set[str], set[str]]:
    doctor_usernames: set[str] = set()
    candidate_usernames: set[str] = set()

    doctor_rows = db.execute(
        text(
            """
            SELECT assigned_doctor_id
            FROM claims
            WHERE COALESCE(source_channel, '') = 'teamrightworks.in'
              AND COALESCE(assigned_doctor_id, '') <> ''
            """
        )
    ).mappings().all()
    for row in doctor_rows:
        raw = str(row.get("assigned_doctor_id") or "")
        for part in raw.split(","):
            user = _sanitize_legacy_username(part)
            if user and ":" not in user:
                doctor_usernames.add(user.lower())
                candidate_usernames.add(user)

    try:
        actor_rows = db.execute(
            text(
                """
                SELECT created_by AS username
                FROM report_versions
                WHERE COALESCE(created_by, '') <> ''
                UNION
                SELECT updated_by AS username
                FROM claim_report_uploads
                WHERE COALESCE(updated_by, '') <> ''
                """
            )
        ).mappings().all()
    except Exception:
        actor_rows = []
    for row in actor_rows:
        user = _sanitize_legacy_username(str(row.get("username") or ""))
        if user and ":" not in user:
            candidate_usernames.add(user)

    return doctor_usernames, candidate_usernames


def _sync_users_from_migrated_data(db: Session, default_password: str) -> dict[str, Any]:
    doctor_usernames, candidate_usernames = _collect_candidate_usernames(db)
    if not candidate_usernames:
        return {
            "candidates": 0,
            "created": 0,
            "updated": 0,
            "skipped": 0,
            "failed": 0,
            "errors": [],
        }

    safe_password = str(default_password or "").strip() or "ChangeMe123A"
    password_hash = hash_password(safe_password)

    created = 0
    updated = 0
    skipped = 0
    failed = 0
    errors: list[dict[str, str]] = []

    for username in sorted(candidate_usernames, key=lambda x: x.lower()):
        clean_username = _sanitize_legacy_username(username)
        if not clean_username:
            skipped += 1
            continue

        try:
            role_value = _normalize_legacy_role("", clean_username.lower(), doctor_usernames)
            existing = db.execute(
                text(
                    """
                    SELECT id, role, is_active
                    FROM users
                    WHERE REPLACE(LOWER(username), ' ', '') = :username_norm
                    LIMIT 1
                    """
                ),
                {"username_norm": clean_username.lower()},
            ).mappings().first()

            if existing is None:
                db.execute(
                    text(
                        """
                        INSERT INTO users (username, password_hash, role, is_active)
                        VALUES (:username, :password_hash, :role, TRUE)
                        """
                    ),
                    {
                        "username": clean_username,
                        "password_hash": password_hash,
                        "role": role_value,
                    },
                )
                created += 1
            else:
                should_update = str(existing.get("role") or "") != role_value or not bool(existing.get("is_active"))
                if should_update:
                    db.execute(
                        text("UPDATE users SET role = :role, is_active = TRUE WHERE id = :id"),
                        {"role": role_value, "id": int(existing["id"])},
                    )
                    updated += 1
                else:
                    skipped += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 30:
                errors.append({"username": clean_username, "error": str(exc)})

    db.commit()

    return {
        "candidates": len(candidate_usernames),
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "failed": failed,
        "errors": errors,
    }


def _run_legacy_migration_job(job_id: str) -> None:
    job = _get_job_snapshot(job_id)
    if not job:
        return

    config = dict(job.get("config") or {})
    include_users = bool(config.get("include_users", True))
    include_claims = bool(config.get("include_claims", True))
    raw_files_only = bool(config.get("raw_files_only", True))
    status_filter = str(config.get("status_filter") or "completed")
    batch_size = int(config.get("batch_size") or 200)
    max_batches = int(config.get("max_batches") or 200)

    _update_job(
        job_id,
        status="running",
        started_at=_utc_now_iso(),
        message="Migration started",
        progress={
            "phase": "initializing",
            "claims": {"selected": 0, "success": 0, "failed": 0, "batches": 0, "last_offset": 0},
            "users": {"candidates": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0},
            "raw_cleanup": {
                "enabled": raw_files_only,
                "claims_touched": 0,
                "report_versions_deleted": 0,
                "claim_report_uploads_deleted": 0,
                "feedback_labels_deleted": 0,
                "decision_results_deleted": 0,
                "document_extractions_deleted": 0,
                "documents_reset": 0,
            },
            "sample_errors": [],
        },
    )

    db = SessionLocal()
    try:
        progress = dict((_get_job_snapshot(job_id) or {}).get("progress") or {})

        if include_claims:
            claims_progress = {
                "selected": 0,
                "success": 0,
                "failed": 0,
                "batches": 0,
                "last_offset": 0,
            }
            raw_cleanup_progress = {
                "enabled": raw_files_only,
                "claims_touched": 0,
                "report_versions_deleted": 0,
                "claim_report_uploads_deleted": 0,
                "feedback_labels_deleted": 0,
                "decision_results_deleted": 0,
                "document_extractions_deleted": 0,
                "documents_reset": 0,
            }
            sample_errors: list[dict[str, Any]] = []
            offset = 0

            for _ in range(max_batches):
                payload = _fetch_legacy_sync_json(
                    {
                        "mode": "bulk",
                        "status": status_filter,
                        "limit": batch_size,
                        "offset": offset,
                        "raw_files_only": 1 if raw_files_only else 0,
                    }
                )

                selected = int(payload.get("total_selected") or 0)
                success = int(payload.get("success") or 0)
                failed = int(payload.get("failed") or 0)

                claims_progress["selected"] += selected
                claims_progress["success"] += success
                claims_progress["failed"] += failed
                claims_progress["batches"] += 1
                claims_progress["last_offset"] = offset

                result_items = list(payload.get("results") or [])
                if failed > 0:
                    for item in result_items:
                        if len(sample_errors) >= 30:
                            break
                        if isinstance(item, dict) and not bool(item.get("ok", False)):
                            sample_errors.append(
                                {
                                    "claim_id": str(item.get("claim_id") or item.get("external_claim_id") or ""),
                                    "error": str(item.get("error") or "sync failed"),
                                    "http_code": int(item.get("http_code") or 0),
                                }
                            )

                if raw_files_only:
                    success_claim_ids: list[str] = []
                    for item in result_items:
                        if not isinstance(item, dict) or not bool(item.get("ok", False)):
                            continue
                        claim_ref = str(item.get("claim_id") or item.get("external_claim_id") or "").strip()
                        if claim_ref:
                            success_claim_ids.append(claim_ref)
                    cleanup_batch_stats = _reset_claims_to_raw_mode(db, success_claim_ids)
                    for key in raw_cleanup_progress.keys():
                        if key == "enabled":
                            continue
                        raw_cleanup_progress[key] += int(cleanup_batch_stats.get(key) or 0)
                    db.commit()

                progress["phase"] = "syncing_claims"
                progress["claims"] = claims_progress
                progress["raw_cleanup"] = raw_cleanup_progress
                progress["sample_errors"] = sample_errors
                _update_job(job_id, progress=progress, message=f"Claims sync batch {claims_progress['batches']} completed")

                if selected < batch_size:
                    break
                offset += batch_size

        if include_users:
            progress["phase"] = "syncing_users"
            _update_job(job_id, progress=progress, message="Creating/updating users from migrated data")
            users_result = _sync_users_from_migrated_data(db, settings.teamrightworks_sync_default_password)
            progress["users"] = users_result
            _update_job(job_id, progress=progress, message="User sync completed")

        progress["phase"] = "completed"
        _update_job(job_id, status="completed", finished_at=_utc_now_iso(), progress=progress, message="Legacy migration completed")
    except Exception as exc:
        db.rollback()
        _update_job(job_id, status="failed", finished_at=_utc_now_iso(), error=str(exc), message="Legacy migration failed")
    finally:
        db.close()
        with LEGACY_MIGRATION_LOCK:
            global LEGACY_MIGRATION_ACTIVE_JOB_ID
            if LEGACY_MIGRATION_ACTIVE_JOB_ID == job_id:
                LEGACY_MIGRATION_ACTIVE_JOB_ID = None

@router.post("/legacy-migration/start")
def start_legacy_migration(
    payload: LegacyMigrationStartRequest,
    _db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    global LEGACY_MIGRATION_ACTIVE_JOB_ID

    if not payload.include_users and not payload.include_claims:
        raise HTTPException(status_code=400, detail="Enable include_users or include_claims")

    with LEGACY_MIGRATION_LOCK:
        if LEGACY_MIGRATION_ACTIVE_JOB_ID:
            active = LEGACY_MIGRATION_JOBS.get(LEGACY_MIGRATION_ACTIVE_JOB_ID) or {}
            if str(active.get("status")) in {"queued", "running"}:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "message": "A migration is already running",
                        "job_id": LEGACY_MIGRATION_ACTIVE_JOB_ID,
                    },
                )

        job_id = str(uuid4())
        job = {
            "job_id": job_id,
            "status": "queued",
            "queued_at": _utc_now_iso(),
            "started_at": None,
            "finished_at": None,
            "started_by": current_user.username,
            "message": "Queued",
            "error": None,
            "config": {
                "include_users": bool(payload.include_users),
                "include_claims": bool(payload.include_claims),
                "raw_files_only": bool(payload.raw_files_only),
                "status_filter": payload.status_filter,
                "batch_size": int(payload.batch_size),
                "max_batches": int(payload.max_batches),
            },
            "progress": {
                "phase": "queued",
                "claims": {"selected": 0, "success": 0, "failed": 0, "batches": 0, "last_offset": 0},
                "users": {"candidates": 0, "created": 0, "updated": 0, "skipped": 0, "failed": 0},
                "raw_cleanup": {"enabled": bool(payload.raw_files_only), "claims_touched": 0, "report_versions_deleted": 0, "claim_report_uploads_deleted": 0, "feedback_labels_deleted": 0, "decision_results_deleted": 0, "document_extractions_deleted": 0, "documents_reset": 0},
                "sample_errors": [],
            },
        }
        LEGACY_MIGRATION_JOBS[job_id] = job
        LEGACY_MIGRATION_JOB_ORDER.append(job_id)
        LEGACY_MIGRATION_ACTIVE_JOB_ID = job_id

    worker = threading.Thread(target=_run_legacy_migration_job, args=(job_id,), daemon=True)
    worker.start()

    return {
        "ok": True,
        "job_id": job_id,
        "status": "queued",
        "message": "Legacy migration started",
    }


@router.get("/legacy-migration/status")
def get_legacy_migration_status(
    job_id: str | None = Query(default=None),
    _db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    with LEGACY_MIGRATION_LOCK:
        target_id = str(job_id or "").strip()
        if not target_id:
            if LEGACY_MIGRATION_ACTIVE_JOB_ID:
                target_id = LEGACY_MIGRATION_ACTIVE_JOB_ID
            elif LEGACY_MIGRATION_JOB_ORDER:
                target_id = LEGACY_MIGRATION_JOB_ORDER[-1]

        if not target_id or target_id not in LEGACY_MIGRATION_JOBS:
            return {"ok": True, "job": None}

        return {"ok": True, "job": dict(LEGACY_MIGRATION_JOBS[target_id])}

@router.get("/claim-rules")
def list_claim_rules(
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    where = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search and search.strip():
        where = "WHERE rule_id ILIKE :q OR name ILIKE :q"
        params["q"] = f"%{search.strip()}%"

    total = db.execute(text(f"SELECT COUNT(*) FROM openai_claim_rules {where}"), params).scalar_one()
    rows = db.execute(
        text(
            f"""
            SELECT id, rule_id, name, scope_json, COALESCE(conditions,'') AS conditions,
                   decision, COALESCE(remark_template,'') AS remark_template,
                   required_evidence_json, severity, priority, is_active,
                   COALESCE(version,'1.0') AS version,
                   COALESCE(source,'manual') AS source,
                   updated_at
            FROM openai_claim_rules
            {where}
            ORDER BY priority ASC, rule_id ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "rule_id": str(row["rule_id"]),
                "name": str(row["name"]),
                "scope": _normalize_json_list(row.get("scope_json")),
                "conditions": str(row.get("conditions") or ""),
                "decision": str(row.get("decision") or "QUERY"),
                "remark_template": str(row.get("remark_template") or ""),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
                "severity": str(row.get("severity") or "SOFT_QUERY"),
                "priority": int(row.get("priority") or 999),
                "is_active": bool(row.get("is_active")),
                "version": str(row.get("version") or "1.0"),
                "source": str(row.get("source") or "manual"),
                "updated_at": row.get("updated_at"),
            }
        )

    return {"total": total, "items": items}


@router.post("/claim-rules", status_code=status.HTTP_201_CREATED)
def create_claim_rule(
    payload: ClaimRuleUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    try:
        row = db.execute(
            text(
                """
                INSERT INTO openai_claim_rules (
                    rule_id, name, scope_json, conditions, decision,
                    remark_template, required_evidence_json, severity,
                    priority, is_active, version, source
                ) VALUES (
                    :rule_id, :name, CAST(:scope_json AS jsonb), :conditions, :decision,
                    :remark_template, CAST(:required_evidence_json AS jsonb), :severity,
                    :priority, :is_active, :version, :source
                )
                RETURNING id
                """
            ),
            {
                "rule_id": payload.rule_id.strip().upper(),
                "name": payload.name.strip(),
                "scope_json": json.dumps(_normalize_json_list(payload.scope)),
                "conditions": (payload.conditions or "").strip(),
                "decision": _normalize_rule_decision(payload.decision),
                "remark_template": (payload.remark_template or "").strip(),
                "required_evidence_json": json.dumps(_normalize_json_list(payload.required_evidence)),
                "severity": _normalize_severity(payload.severity),
                "priority": int(payload.priority),
                "is_active": bool(payload.is_active),
                "version": payload.version.strip() or "1.0",
                "source": f"manual:{current_user.username}",
            },
        ).mappings().one()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="rule_id already exists") from exc

    db.commit()
    return {"id": int(row["id"]), "message": "rule created"}


@router.patch("/claim-rules/{row_id}")
def update_claim_rule(
    row_id: int,
    payload: ClaimRuleUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    try:
        row = db.execute(
            text(
                """
                UPDATE openai_claim_rules
                SET rule_id = :rule_id,
                    name = :name,
                    scope_json = CAST(:scope_json AS jsonb),
                    conditions = :conditions,
                    decision = :decision,
                    remark_template = :remark_template,
                    required_evidence_json = CAST(:required_evidence_json AS jsonb),
                    severity = :severity,
                    priority = :priority,
                    is_active = :is_active,
                    version = :version,
                    source = :source
                WHERE id = :id
                RETURNING id
                """
            ),
            {
                "id": row_id,
                "rule_id": payload.rule_id.strip().upper(),
                "name": payload.name.strip(),
                "scope_json": json.dumps(_normalize_json_list(payload.scope)),
                "conditions": (payload.conditions or "").strip(),
                "decision": _normalize_rule_decision(payload.decision),
                "remark_template": (payload.remark_template or "").strip(),
                "required_evidence_json": json.dumps(_normalize_json_list(payload.required_evidence)),
                "severity": _normalize_severity(payload.severity),
                "priority": int(payload.priority),
                "is_active": bool(payload.is_active),
                "version": payload.version.strip() or "1.0",
                "source": f"manual:{current_user.username}",
            },
        ).mappings().first()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="rule_id already exists") from exc

    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="rule not found")

    db.commit()
    return {"id": int(row["id"]), "message": "rule updated"}


@router.patch("/claim-rules/{row_id}/toggle")
def toggle_claim_rule(
    row_id: int,
    is_active: bool,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    row = db.execute(
        text("UPDATE openai_claim_rules SET is_active = :is_active WHERE id = :id RETURNING id"),
        {"id": row_id, "is_active": is_active},
    ).mappings().first()
    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="rule not found")
    db.commit()
    return {"id": int(row["id"]), "is_active": is_active}


@router.delete("/claim-rules/{row_id}")
def delete_claim_rule(
    row_id: int,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    deleted = db.execute(text("DELETE FROM openai_claim_rules WHERE id = :id"), {"id": row_id}).rowcount
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="rule not found")
    return {"deleted": True}


@router.get("/diagnosis-criteria")
def list_diagnosis_criteria(
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    where = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search and search.strip():
        where = "WHERE criteria_id ILIKE :q OR diagnosis_name ILIKE :q"
        params["q"] = f"%{search.strip()}%"

    total = db.execute(text(f"SELECT COUNT(*) FROM openai_diagnosis_criteria {where}"), params).scalar_one()
    rows = db.execute(
        text(
            f"""
            SELECT id, criteria_id, COALESCE(diagnosis_key,'') AS diagnosis_key,
                   diagnosis_name, aliases_json, required_evidence_json,
                   decision, COALESCE(remark_template,'') AS remark_template,
                   severity, priority, is_active, COALESCE(version,'1.0') AS version,
                   COALESCE(source,'manual') AS source, updated_at
            FROM openai_diagnosis_criteria
            {where}
            ORDER BY priority ASC, criteria_id ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "criteria_id": str(row["criteria_id"]),
                "diagnosis_key": str(row.get("diagnosis_key") or ""),
                "diagnosis_name": str(row["diagnosis_name"]),
                "aliases": _normalize_json_list(row.get("aliases_json")),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
                "decision": str(row.get("decision") or "QUERY"),
                "remark_template": str(row.get("remark_template") or ""),
                "severity": str(row.get("severity") or "SOFT_QUERY"),
                "priority": int(row.get("priority") or 999),
                "is_active": bool(row.get("is_active")),
                "version": str(row.get("version") or "1.0"),
                "source": str(row.get("source") or "manual"),
                "updated_at": row.get("updated_at"),
            }
        )

    return {"total": total, "items": items}


@router.post("/diagnosis-criteria", status_code=status.HTTP_201_CREATED)
def create_diagnosis_criteria(
    payload: DiagnosisCriteriaUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    diagnosis_key = (payload.diagnosis_key or payload.diagnosis_name).strip().lower().replace(" ", "_")

    try:
        row = db.execute(
            text(
                """
                INSERT INTO openai_diagnosis_criteria (
                    criteria_id, diagnosis_key, diagnosis_name, aliases_json,
                    required_evidence_json, decision, remark_template, severity,
                    priority, is_active, version, source
                ) VALUES (
                    :criteria_id, :diagnosis_key, :diagnosis_name, CAST(:aliases_json AS jsonb),
                    CAST(:required_evidence_json AS jsonb), :decision, :remark_template, :severity,
                    :priority, :is_active, :version, :source
                )
                RETURNING id
                """
            ),
            {
                "criteria_id": payload.criteria_id.strip().upper(),
                "diagnosis_key": diagnosis_key,
                "diagnosis_name": payload.diagnosis_name.strip(),
                "aliases_json": json.dumps(_normalize_json_list(payload.aliases)),
                "required_evidence_json": json.dumps(_normalize_json_list(payload.required_evidence)),
                "decision": _normalize_rule_decision(payload.decision),
                "remark_template": (payload.remark_template or "").strip(),
                "severity": _normalize_severity(payload.severity),
                "priority": int(payload.priority),
                "is_active": bool(payload.is_active),
                "version": payload.version.strip() or "1.0",
                "source": f"manual:{current_user.username}",
            },
        ).mappings().one()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="criteria_id/diagnosis_key already exists") from exc

    db.commit()
    return {"id": int(row["id"]), "message": "diagnosis criteria created"}


@router.patch("/diagnosis-criteria/{row_id}")
def update_diagnosis_criteria(
    row_id: int,
    payload: DiagnosisCriteriaUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    diagnosis_key = (payload.diagnosis_key or payload.diagnosis_name).strip().lower().replace(" ", "_")
    try:
        row = db.execute(
            text(
                """
                UPDATE openai_diagnosis_criteria
                SET criteria_id = :criteria_id,
                    diagnosis_key = :diagnosis_key,
                    diagnosis_name = :diagnosis_name,
                    aliases_json = CAST(:aliases_json AS jsonb),
                    required_evidence_json = CAST(:required_evidence_json AS jsonb),
                    decision = :decision,
                    remark_template = :remark_template,
                    severity = :severity,
                    priority = :priority,
                    is_active = :is_active,
                    version = :version,
                    source = :source
                WHERE id = :id
                RETURNING id
                """
            ),
            {
                "id": row_id,
                "criteria_id": payload.criteria_id.strip().upper(),
                "diagnosis_key": diagnosis_key,
                "diagnosis_name": payload.diagnosis_name.strip(),
                "aliases_json": json.dumps(_normalize_json_list(payload.aliases)),
                "required_evidence_json": json.dumps(_normalize_json_list(payload.required_evidence)),
                "decision": _normalize_rule_decision(payload.decision),
                "remark_template": (payload.remark_template or "").strip(),
                "severity": _normalize_severity(payload.severity),
                "priority": int(payload.priority),
                "is_active": bool(payload.is_active),
                "version": payload.version.strip() or "1.0",
                "source": f"manual:{current_user.username}",
            },
        ).mappings().first()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="criteria_id/diagnosis_key already exists") from exc

    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="diagnosis criteria not found")
    db.commit()
    return {"id": int(row["id"]), "message": "diagnosis criteria updated"}


@router.patch("/diagnosis-criteria/{row_id}/toggle")
def toggle_diagnosis_criteria(
    row_id: int,
    is_active: bool,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    row = db.execute(
        text("UPDATE openai_diagnosis_criteria SET is_active = :is_active WHERE id = :id RETURNING id"),
        {"id": row_id, "is_active": is_active},
    ).mappings().first()
    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="diagnosis criteria not found")
    db.commit()
    return {"id": int(row["id"]), "is_active": is_active}


@router.delete("/diagnosis-criteria/{row_id}")
def delete_diagnosis_criteria(
    row_id: int,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    deleted = db.execute(text("DELETE FROM openai_diagnosis_criteria WHERE id = :id"), {"id": row_id}).rowcount
    db.commit()
    if not deleted:
        raise HTTPException(status_code=404, detail="diagnosis criteria not found")
    return {"deleted": True}


@router.get("/rule-suggestions")
def list_rule_suggestions(
    status_filter: str = Query(default="pending"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    allowed = {"pending", "approved", "rejected", "all"}
    status_filter = status_filter if status_filter in allowed else "pending"

    where = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if status_filter != "all":
        where = "WHERE status = :status"
        params["status"] = status_filter

    total = db.execute(text(f"SELECT COUNT(*) FROM openai_claim_rule_suggestions {where}"), params).scalar_one()
    rows = db.execute(
        text(
            f"""
            SELECT id, source_analysis_id, claim_id, suggestion_type, target_rule_id,
                   proposed_rule_id, suggested_name, suggested_decision,
                   suggested_conditions, suggested_remark_template,
                   suggested_required_evidence_json, source_context_text,
                   generator_confidence, generator_reasoning, status,
                   approved_rule_id, created_at, updated_at
            FROM openai_claim_rule_suggestions
            {where}
            ORDER BY (status = 'pending') DESC, created_at DESC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "source_analysis_id": int(row.get("source_analysis_id") or 0),
                "claim_id": str(row.get("claim_id") or ""),
                "suggestion_type": str(row.get("suggestion_type") or "new_rule"),
                "target_rule_id": str(row.get("target_rule_id") or ""),
                "proposed_rule_id": str(row.get("proposed_rule_id") or ""),
                "suggested_name": str(row.get("suggested_name") or ""),
                "suggested_decision": str(row.get("suggested_decision") or "QUERY"),
                "suggested_conditions": str(row.get("suggested_conditions") or ""),
                "suggested_remark_template": str(row.get("suggested_remark_template") or ""),
                "suggested_required_evidence": _normalize_json_list(row.get("suggested_required_evidence_json")),
                "source_context_text": str(row.get("source_context_text") or ""),
                "generator_confidence": int(row.get("generator_confidence") or 0),
                "generator_reasoning": str(row.get("generator_reasoning") or ""),
                "status": str(row.get("status") or "pending"),
                "approved_rule_id": str(row.get("approved_rule_id") or ""),
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
        )

    return {"total": total, "items": items}


def _next_claim_rule_id(db: Session) -> str:
    rows = db.execute(text("SELECT rule_id FROM openai_claim_rules WHERE rule_id ILIKE 'R%'"))
    max_no = 0
    for row in rows:
        rule_id = str(row[0] or "").strip().upper()
        if rule_id.startswith("R") and rule_id[1:].isdigit():
            max_no = max(max_no, int(rule_id[1:]))
    return f"R{max_no + 1:04d}"


def _upsert_claim_rule_from_suggestion(db: Session, suggestion: dict[str, Any], approved_rule_id: str | None) -> str:
    suggestion_type = str(suggestion.get("suggestion_type") or "new_rule").strip().lower()
    target_rule_id = str(suggestion.get("target_rule_id") or "").strip().upper()
    proposed_rule_id = str(suggestion.get("proposed_rule_id") or "").strip().upper()

    final_rule_id = (approved_rule_id or "").strip().upper()
    if suggestion_type in {"update_rule", "implied_rule"}:
        final_rule_id = target_rule_id
        existing = db.execute(
            text("SELECT id FROM openai_claim_rules WHERE rule_id = :rule_id LIMIT 1"),
            {"rule_id": final_rule_id},
        ).mappings().first()
        if existing is None:
            raise HTTPException(status_code=400, detail="target rule not found for update suggestion")

        db.execute(
            text(
                """
                UPDATE openai_claim_rules
                SET name = :name,
                    conditions = :conditions,
                    decision = :decision,
                    remark_template = :remark_template,
                    required_evidence_json = CAST(:required_evidence_json AS jsonb),
                    severity = :severity,
                    source = 'suggested_update'
                WHERE rule_id = :rule_id
                """
            ),
            {
                "rule_id": final_rule_id,
                "name": str(suggestion.get("suggested_name") or "Suggested rule"),
                "conditions": str(suggestion.get("suggested_conditions") or ""),
                "decision": _normalize_rule_decision(str(suggestion.get("suggested_decision") or "QUERY")),
                "remark_template": str(suggestion.get("suggested_remark_template") or ""),
                "required_evidence_json": json.dumps(_normalize_json_list(suggestion.get("suggested_required_evidence_json"))),
                "severity": "HARD_REJECT"
                if _normalize_rule_decision(str(suggestion.get("suggested_decision") or "QUERY")) == "REJECT"
                else "SOFT_QUERY",
            },
        )
        return final_rule_id

    if not final_rule_id:
        if proposed_rule_id:
            existing = db.execute(
                text("SELECT 1 FROM openai_claim_rules WHERE rule_id = :rule_id LIMIT 1"),
                {"rule_id": proposed_rule_id},
            ).first()
            final_rule_id = _next_claim_rule_id(db) if existing else proposed_rule_id
        else:
            final_rule_id = _next_claim_rule_id(db)

    db.execute(
        text(
            """
            INSERT INTO openai_claim_rules (
                rule_id, name, scope_json, conditions, decision, remark_template,
                required_evidence_json, severity, priority, is_active, version, source
            ) VALUES (
                :rule_id, :name, CAST(:scope_json AS jsonb), :conditions, :decision, :remark_template,
                CAST(:required_evidence_json AS jsonb), :severity, 999, TRUE, '1.0', 'suggested'
            )
            ON CONFLICT (rule_id) DO UPDATE
            SET name = EXCLUDED.name,
                conditions = EXCLUDED.conditions,
                decision = EXCLUDED.decision,
                remark_template = EXCLUDED.remark_template,
                required_evidence_json = EXCLUDED.required_evidence_json,
                severity = EXCLUDED.severity,
                source = EXCLUDED.source
            """
        ),
        {
            "rule_id": final_rule_id,
            "name": str(suggestion.get("suggested_name") or "Suggested rule"),
            "scope_json": json.dumps([]),
            "conditions": str(suggestion.get("suggested_conditions") or ""),
            "decision": _normalize_rule_decision(str(suggestion.get("suggested_decision") or "QUERY")),
            "remark_template": str(suggestion.get("suggested_remark_template") or ""),
            "required_evidence_json": json.dumps(_normalize_json_list(suggestion.get("suggested_required_evidence_json"))),
            "severity": "HARD_REJECT"
            if _normalize_rule_decision(str(suggestion.get("suggested_decision") or "QUERY")) == "REJECT"
            else "SOFT_QUERY",
        },
    )
    return final_rule_id


@router.patch("/rule-suggestions/{suggestion_id}")
def review_rule_suggestion(
    suggestion_id: int,
    payload: SuggestionReviewRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    suggestion = db.execute(
        text("SELECT * FROM openai_claim_rule_suggestions WHERE id = :id LIMIT 1"),
        {"id": suggestion_id},
    ).mappings().first()

    if suggestion is None:
        raise HTTPException(status_code=404, detail="suggestion not found")

    approved_rule_id = str(payload.approved_rule_id or "").strip().upper() or None

    if payload.status == "approved":
        approved_rule_id = _upsert_claim_rule_from_suggestion(db, dict(suggestion), approved_rule_id)

    row = db.execute(
        text(
            """
            UPDATE openai_claim_rule_suggestions
            SET status = :status,
                approved_rule_id = :approved_rule_id,
                reviewed_by_user_id = (SELECT id FROM users WHERE username = :username LIMIT 1),
                reviewed_at = NOW()
            WHERE id = :id
            RETURNING id, status, approved_rule_id
            """
        ),
        {
            "id": suggestion_id,
            "status": payload.status,
            "approved_rule_id": approved_rule_id,
            "username": current_user.username,
        },
    ).mappings().first()

    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="suggestion not found")

    db.commit()
    return {
        "id": int(row["id"]),
        "status": str(row["status"]),
        "approved_rule_id": str(row.get("approved_rule_id") or ""),
    }

@router.get("/medicines")
def list_medicines(
    search: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    where = ""
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    if search and search.strip():
        where = "WHERE medicine_name ILIKE :q OR components ILIKE :q OR subclassification ILIKE :q"
        params["q"] = f"%{search.strip()}%"

    total = db.execute(text(f"SELECT COUNT(*) FROM medicine_component_lookup {where}"), params).scalar_one()
    rows = db.execute(
        text(
            f"""
            SELECT id, medicine_key, medicine_name, components, subclassification,
                   is_high_end_antibiotic, source, updated_at
            FROM medicine_component_lookup
            {where}
            ORDER BY medicine_name ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = []
    for row in rows:
        items.append(
            {
                "id": int(row["id"]),
                "medicine_key": str(row.get("medicine_key") or ""),
                "medicine_name": str(row.get("medicine_name") or ""),
                "components": str(row.get("components") or ""),
                "subclassification": str(row.get("subclassification") or ""),
                "is_high_end_antibiotic": bool(row.get("is_high_end_antibiotic")),
                "source": str(row.get("source") or "table"),
                "updated_at": row.get("updated_at"),
            }
        )

    return {"total": total, "items": items}


@router.post("/medicines", status_code=status.HTTP_201_CREATED)
def create_medicine(
    payload: MedicineUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    key = _medicine_key(payload.medicine_name)
    if not key:
        raise HTTPException(status_code=400, detail="invalid medicine_name")

    try:
        row = db.execute(
            text(
                """
                INSERT INTO medicine_component_lookup (
                    medicine_key, medicine_name, components, subclassification,
                    is_high_end_antibiotic, source, last_checked_at
                ) VALUES (
                    :medicine_key, :medicine_name, :components, :subclassification,
                    :is_high_end_antibiotic, :source, NOW()
                )
                RETURNING id
                """
            ),
            {
                "medicine_key": key,
                "medicine_name": payload.medicine_name.strip(),
                "components": payload.components.strip(),
                "subclassification": payload.subclassification.strip() or "Supportive care",
                "is_high_end_antibiotic": bool(payload.is_high_end_antibiotic),
                "source": f"manual:{current_user.username}",
            },
        ).mappings().one()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="medicine already exists") from exc

    db.commit()
    return {"id": int(row["id"]), "message": "medicine created"}


@router.patch("/medicines/{medicine_id}")
def update_medicine(
    medicine_id: int,
    payload: MedicineUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    key = _medicine_key(payload.medicine_name)
    if not key:
        raise HTTPException(status_code=400, detail="invalid medicine_name")

    try:
        row = db.execute(
            text(
                """
                UPDATE medicine_component_lookup
                SET medicine_key = :medicine_key,
                    medicine_name = :medicine_name,
                    components = :components,
                    subclassification = :subclassification,
                    is_high_end_antibiotic = :is_high_end_antibiotic,
                    source = :source,
                    last_checked_at = NOW()
                WHERE id = :id
                RETURNING id
                """
            ),
            {
                "id": medicine_id,
                "medicine_key": key,
                "medicine_name": payload.medicine_name.strip(),
                "components": payload.components.strip(),
                "subclassification": payload.subclassification.strip() or "Supportive care",
                "is_high_end_antibiotic": bool(payload.is_high_end_antibiotic),
                "source": f"manual:{current_user.username}",
            },
        ).mappings().first()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="medicine key conflict") from exc

    if row is None:
        db.rollback()
        raise HTTPException(status_code=404, detail="medicine not found")

    db.commit()
    return {"id": int(row["id"]), "message": "medicine updated"}


@router.get("/storage-maintenance")
def storage_maintenance_summary(
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    totals = db.execute(
        text(
            """
            SELECT
                COUNT(*) AS total_documents,
                COALESCE(SUM(file_size_bytes), 0) AS total_bytes,
                COUNT(*) FILTER (WHERE parse_status = 'pending') AS pending_count,
                COUNT(*) FILTER (WHERE parse_status = 'processing') AS processing_count,
                COUNT(*) FILTER (WHERE parse_status = 'succeeded') AS succeeded_count,
                COUNT(*) FILTER (WHERE parse_status = 'failed') AS failed_count
            FROM claim_documents
            """
        )
    ).mappings().one()

    bucket_rows = db.execute(
        text(
            """
            SELECT COALESCE(metadata->>'bucket', 'unknown') AS bucket, COUNT(*) AS count
            FROM claim_documents
            GROUP BY COALESCE(metadata->>'bucket', 'unknown')
            ORDER BY count DESC
            """
        )
    ).mappings().all()

    return {
        "total_documents": int(totals["total_documents"] or 0),
        "total_bytes": int(totals["total_bytes"] or 0),
        "parse_status_counts": {
            "pending": int(totals["pending_count"] or 0),
            "processing": int(totals["processing_count"] or 0),
            "succeeded": int(totals["succeeded_count"] or 0),
            "failed": int(totals["failed_count"] or 0),
        },
        "buckets": [{"bucket": str(r["bucket"]), "count": int(r["count"])} for r in bucket_rows],
    }

@router.post("/analysis/import-sql")
async def import_analysis_sql_dump(
    file: UploadFile = File(...),
    limit: int = Query(default=0, ge=0, le=250000),
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    filename = str(file.filename or "").strip()
    if not filename.lower().endswith(".sql"):
        raise HTTPException(status_code=400, detail="Please upload a .sql dump file")

    payload = await file.read()
    if not payload:
        raise HTTPException(status_code=400, detail="empty file")

    try:
        rows_iter = iter_table_rows_from_sql_dump_bytes(payload, "openai_analysis_results")
        summary = import_analysis_results_from_rows(
            db,
            rows_iter,
            limit=int(limit or 0),
            created_by_system=f"system:legacy_sql_import:{current_user.username}",
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"analysis SQL import failed: {exc}") from exc

    return {
        "ok": True,
        "file": filename,
        "limit": int(limit or 0),
        "imported_by": current_user.username,
        **summary,
    }







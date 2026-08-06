from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.db.session import engine


class ChecklistSourceError(Exception):
    pass


_CACHE: dict[str, Any] = {"loaded_at": 0.0, "rules": [], "criteria": [], "source": "none"}
_CACHE_TTL_SECONDS = 120


FALLBACK_RULES: list[dict[str, Any]] = [
    {
        "rule_id": "R001",
        "name": "Meropenem/high-end antibiotic without sepsis markers",
        "scope": ["antibiotic", "sepsis"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 1,
        "required_evidence": [
            "Sepsis marker labs",
            "Sepsis work-up notes",
            "Objective organ dysfunction/hypotension documentation",
        ],
    },
    {
        "rule_id": "R005",
        "name": "Sepsis diagnosis must have markers and culture/work-up",
        "scope": ["sepsis"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 2,
        "required_evidence": ["Sepsis markers", "Blood culture or documented sepsis work-up rationale"],
    },
    {
        "rule_id": "R003",
        "name": "Pneumonia imaging negative + no culture + high-end antibiotic",
        "scope": ["pneumonia", "antibiotic"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 3,
        "required_evidence": ["X-ray/CT evidence", "Blood culture", "Antibiotic indication notes"],
    },
    {
        "rule_id": "R004",
        "name": "UTI without urine culture/sensitivity correlation",
        "scope": ["uti", "antibiotic"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 4,
        "required_evidence": ["Urine culture", "Sensitivity", "Antibiotic correlation with sensitivity"],
    },
    {
        "rule_id": "R002",
        "name": "ORIF billed without displaced/unstable fracture indication",
        "scope": ["fracture", "orif"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 5,
        "required_evidence": ["X-ray/CT displacement details", "Instability or neurovascular risk", "OT/operative note"],
    },
    {
        "rule_id": "R006",
        "name": "High-end antibiotic not supported by vitals/objective evidence",
        "scope": ["vitals", "antibiotic"],
        "decision": "QUERY",
        "severity": "SOFT_QUERY",
        "priority": 6,
        "required_evidence": ["TPR chart", "BP trend", "Fever/tachycardia/tachypnea evidence", "Objective infection labs/culture"],
    },
    {
        "rule_id": "R009",
        "name": "Hairline fracture in surgical fixation claim",
        "scope": ["fracture", "surgical"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 7,
        "required_evidence": ["Fracture description", "Procedure note/bill showing ORIF/K-wire", "Surgical indication"],
    },
    {
        "rule_id": "R010",
        "name": "Stable or undisplaced fracture without ORIF/K-wire indication",
        "scope": ["fracture", "orif", "kwire"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 8,
        "required_evidence": ["Imaging displacement status", "Indication for ORIF/K-wire", "Operative record"],
    },
    {
        "rule_id": "R007",
        "name": "Ayurvedic treatment hospital accreditation/registration missing",
        "scope": ["ayurvedic", "hospital_credentials"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 10,
        "required_evidence": ["Hospital government registration proof", "NABL accreditation proof"],
    },
    {
        "rule_id": "R008",
        "name": "Alcoholism history with CLD context",
        "scope": ["alcoholism", "policy_exclusion"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 11,
        "required_evidence": ["Alcohol history documentation", "CLD diagnosis evidence"],
    },
    {
        "rule_id": "R013",
        "name": "UTI + Meropenem with culture sensitivity evidence",
        "scope": ["uti", "culture", "antibiotic"],
        "decision": "APPROVE",
        "severity": "INFO",
        "priority": 13,
        "required_evidence": ["Urine culture report", "Sensitivity panel with susceptible antibiotics", "Antibiotic chart including Meropenem"],
    },
    {
        "rule_id": "R014",
        "name": "Bill amount below 20,000 with non-OPD treatment flow override",
        "scope": ["billing", "flow_override"],
        "decision": "APPROVE",
        "severity": "INFO",
        "priority": 14,
        "required_evidence": ["Claim amount", "Admission/treatment context showing non-OPD pattern"],
    },
    {
        "rule_id": "R015",
        "name": "High-bill maternity/LSCS/neonatal jaundice flow override",
        "scope": ["billing", "maternity", "neonatal", "flow_override"],
        "decision": "APPROVE",
        "severity": "INFO",
        "priority": 15,
        "required_evidence": ["Claim amount", "Maternity/LSCS/labour or neonatal jaundice context in records"],
    },
    {
        "rule_id": "R016",
        "name": "Sepsis evidence requires combination of vitals, markers, and culture",
        "scope": ["sepsis", "antibiotic"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 16,
        "required_evidence": ["Vitals trend (Temp/Pulse/BP)", "Sepsis marker panel", "Blood/Urine/Pus culture correlation"],
    },
]

FALLBACK_DIAGNOSIS: list[dict[str, Any]] = [
    {
        "criteria_id": "DX001",
        "diagnosis_name": "UTI",
        "aliases": ["UTI", "Urinary Tract Infection"],
        "decision": "QUERY",
        "severity": "SOFT_QUERY",
        "priority": 1,
        "required_evidence": [
            "Burning micturition or dysuria symptoms",
            "Urine routine report",
            "Urine microscopy report",
        ],
    },
    {
        "criteria_id": "DX002",
        "diagnosis_name": "Septicemia / Sepsis",
        "aliases": ["Septicemia", "Sepsis", "Urosepsis", "Septic Shock"],
        "decision": "REJECT",
        "severity": "HARD_REJECT",
        "priority": 2,
        "required_evidence": [
            "Sepsis marker labs",
            "Culture or sepsis work-up",
            "Objective hemodynamic instability or organ dysfunction evidence",
        ],
    },
    {
        "criteria_id": "DX003",
        "diagnosis_name": "AFI",
        "aliases": ["AFI", "Acute Febrile Illness"],
        "decision": "QUERY",
        "severity": "SOFT_QUERY",
        "priority": 3,
        "required_evidence": [
            "Fever history or temperature/vitals evidence",
            "Basic infection work-up",
            "Clinical findings supporting acute febrile illness",
        ],
    },
]


def _normalize_json_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(v).strip() for v in raw if str(v).strip()]
    if isinstance(raw, str):
        raw = raw.strip()
        if raw == "":
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except json.JSONDecodeError:
            return [raw]
    return []



def _load_from_modern_postgres() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        with engine.connect() as conn:
            raw_rules = conn.execute(
                text(
                    """
                    SELECT rule_id, name, scope_json, conditions, decision, remark_template, severity, priority, required_evidence_json
                    FROM openai_claim_rules
                    WHERE is_active = TRUE
                    ORDER BY priority ASC, rule_id ASC
                    """
                )
            ).mappings().all()

            raw_criteria = conn.execute(
                text(
                    """
                    SELECT criteria_id, diagnosis_name, aliases_json, decision, remark_template, severity, priority, required_evidence_json
                    FROM openai_diagnosis_criteria
                    WHERE is_active = TRUE
                    ORDER BY priority ASC, criteria_id ASC
                    """
                )
            ).mappings().all()
    except Exception as exc:
        raise ChecklistSourceError(f"Modern DB checklist query failed: {exc}") from exc

    rules: list[dict[str, Any]] = []
    for row in raw_rules:
        rules.append(
            {
                "rule_id": str(row.get("rule_id") or "").strip().upper(),
                "name": str(row.get("name") or "").strip(),
                "scope": _normalize_json_list(row.get("scope_json")),
                "conditions": str(row.get("conditions") or "").strip(),
                "decision": str(row.get("decision") or "QUERY").strip().upper(),
                "remark_template": str(row.get("remark_template") or "").strip(),
                "severity": str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                "priority": int(row.get("priority") or 999),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
            }
        )

    criteria: list[dict[str, Any]] = []
    for row in raw_criteria:
        aliases = _normalize_json_list(row.get("aliases_json"))
        diagnosis_name = str(row.get("diagnosis_name") or "").strip()
        if diagnosis_name and diagnosis_name not in aliases:
            aliases.insert(0, diagnosis_name)
        criteria.append(
            {
                "criteria_id": str(row.get("criteria_id") or "").strip().upper(),
                "diagnosis_name": diagnosis_name,
                "aliases": aliases,
                "decision": str(row.get("decision") or "QUERY").strip().upper(),
                "remark_template": str(row.get("remark_template") or "").strip(),
                "severity": str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                "priority": int(row.get("priority") or 999),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
            }
        )

    rules = [r for r in rules if r["rule_id"] and r["name"]]
    criteria = [c for c in criteria if c["criteria_id"] and c["diagnosis_name"]]

    if not rules and not criteria:
        raise ChecklistSourceError("Modern DB returned no active checklist records")

    return rules, criteria

def _load_from_legacy_mysql() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        import pymysql
        from pymysql.cursors import DictCursor
    except Exception as exc:
        raise ChecklistSourceError("PyMySQL is not installed") from exc

    try:
        conn = pymysql.connect(
            host=settings.legacy_db_host,
            port=settings.legacy_db_port,
            user=settings.legacy_db_user,
            password=settings.legacy_db_pass,
            database=settings.legacy_db_name,
            charset="utf8mb4",
            cursorclass=DictCursor,
            connect_timeout=5,
            read_timeout=10,
            write_timeout=10,
            autocommit=True,
        )
    except Exception as exc:
        raise ChecklistSourceError(f"Legacy DB connection failed: {exc}") from exc

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT rule_id, name, scope_json, conditions, decision, remark_template, severity, priority, required_evidence_json
                FROM openai_claim_rules
                WHERE is_active = 1
                ORDER BY priority ASC, rule_id ASC
                """
            )
            raw_rules = cur.fetchall() or []

            cur.execute(
                """
                SELECT criteria_id, diagnosis_name, aliases_json, decision, remark_template, severity, priority, required_evidence_json
                FROM openai_diagnosis_criteria
                WHERE is_active = 1
                ORDER BY priority ASC, criteria_id ASC
                """
            )
            raw_criteria = cur.fetchall() or []
    finally:
        conn.close()

    rules: list[dict[str, Any]] = []
    for row in raw_rules:
        rules.append(
            {
                "rule_id": str(row.get("rule_id") or "").strip().upper(),
                "name": str(row.get("name") or "").strip(),
                "scope": _normalize_json_list(row.get("scope_json")),
                "conditions": str(row.get("conditions") or "").strip(),
                "decision": str(row.get("decision") or "QUERY").strip().upper(),
                "remark_template": str(row.get("remark_template") or "").strip(),
                "severity": str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                "priority": int(row.get("priority") or 999),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
            }
        )

    criteria: list[dict[str, Any]] = []
    for row in raw_criteria:
        aliases = _normalize_json_list(row.get("aliases_json"))
        diagnosis_name = str(row.get("diagnosis_name") or "").strip()
        if diagnosis_name and diagnosis_name not in aliases:
            aliases.insert(0, diagnosis_name)
        criteria.append(
            {
                "criteria_id": str(row.get("criteria_id") or "").strip().upper(),
                "diagnosis_name": diagnosis_name,
                "aliases": aliases,
                "decision": str(row.get("decision") or "QUERY").strip().upper(),
                "remark_template": str(row.get("remark_template") or "").strip(),
                "severity": str(row.get("severity") or "SOFT_QUERY").strip().upper(),
                "priority": int(row.get("priority") or 999),
                "required_evidence": _normalize_json_list(row.get("required_evidence_json")),
            }
        )

    rules = [r for r in rules if r["rule_id"] and r["name"]]
    criteria = [c for c in criteria if c["criteria_id"] and c["diagnosis_name"]]

    if not rules and not criteria:
        raise ChecklistSourceError("Legacy DB returned no active checklist records")

    return rules, criteria



def _normalize_catalog_key(value: str) -> str:
    return "_".join([part for part in str(value or "").strip().lower().replace("-", " ").split() if part])


def _upsert_catalog_to_modern_postgres(
    rules: list[dict[str, Any]],
    criteria: list[dict[str, Any]],
    source_tag: str,
) -> dict[str, int]:
    rules_upserted = 0
    criteria_upserted = 0
    with engine.begin() as conn:
        for rule in rules:
            rule_id = str(rule.get("rule_id") or "").strip().upper()
            name = str(rule.get("name") or "").strip()
            if not rule_id or not name:
                continue
            conn.execute(
                text(
                    """
                    INSERT INTO openai_claim_rules (
                        rule_id, name, scope_json, conditions, decision, remark_template,
                        severity, priority, required_evidence_json, is_active, updated_at,
                        version, source
                    )
                    VALUES (
                        :rule_id, :name, CAST(:scope_json AS jsonb), :conditions, :decision, :remark_template,
                        :severity, :priority, CAST(:required_evidence_json AS jsonb), TRUE, NOW(),
                        :version, :source
                    )
                    ON CONFLICT (rule_id)
                    DO UPDATE SET
                        name = EXCLUDED.name,
                        scope_json = EXCLUDED.scope_json,
                        conditions = EXCLUDED.conditions,
                        decision = EXCLUDED.decision,
                        remark_template = EXCLUDED.remark_template,
                        severity = EXCLUDED.severity,
                        priority = EXCLUDED.priority,
                        required_evidence_json = EXCLUDED.required_evidence_json,
                        is_active = TRUE,
                        updated_at = NOW(),
                        version = EXCLUDED.version,
                        source = EXCLUDED.source
                    """
                ),
                {
                    "rule_id": rule_id,
                    "name": name,
                    "scope_json": json.dumps(rule.get("scope") or []),
                    "conditions": str(rule.get("conditions") or "").strip() or None,
                    "decision": str(rule.get("decision") or "QUERY").strip().upper() or "QUERY",
                    "remark_template": str(rule.get("remark_template") or "").strip() or None,
                    "severity": str(rule.get("severity") or "SOFT_QUERY").strip().upper() or "SOFT_QUERY",
                    "priority": int(rule.get("priority") or 999),
                    "required_evidence_json": json.dumps(rule.get("required_evidence") or []),
                    "version": "legacy-sync-v1",
                    "source": source_tag,
                },
            )
            rules_upserted += 1

        for criterion in criteria:
            criteria_id = str(criterion.get("criteria_id") or "").strip().upper()
            diagnosis_name = str(criterion.get("diagnosis_name") or "").strip()
            if not criteria_id or not diagnosis_name:
                continue
            aliases = criterion.get("aliases") or []
            if diagnosis_name and diagnosis_name not in aliases:
                aliases = [diagnosis_name] + list(aliases)
            conn.execute(
                text(
                    """
                    INSERT INTO openai_diagnosis_criteria (
                        criteria_id, diagnosis_name, diagnosis_key, aliases_json, decision,
                        remark_template, severity, priority, required_evidence_json,
                        is_active, updated_at, version, source
                    )
                    VALUES (
                        :criteria_id, :diagnosis_name, :diagnosis_key, CAST(:aliases_json AS jsonb), :decision,
                        :remark_template, :severity, :priority, CAST(:required_evidence_json AS jsonb),
                        TRUE, NOW(), :version, :source
                    )
                    ON CONFLICT (criteria_id)
                    DO UPDATE SET
                        diagnosis_name = EXCLUDED.diagnosis_name,
                        diagnosis_key = EXCLUDED.diagnosis_key,
                        aliases_json = EXCLUDED.aliases_json,
                        decision = EXCLUDED.decision,
                        remark_template = EXCLUDED.remark_template,
                        severity = EXCLUDED.severity,
                        priority = EXCLUDED.priority,
                        required_evidence_json = EXCLUDED.required_evidence_json,
                        is_active = TRUE,
                        updated_at = NOW(),
                        version = EXCLUDED.version,
                        source = EXCLUDED.source
                    """
                ),
                {
                    "criteria_id": criteria_id,
                    "diagnosis_name": diagnosis_name,
                    "diagnosis_key": _normalize_catalog_key(diagnosis_name),
                    "aliases_json": json.dumps(aliases),
                    "decision": str(criterion.get("decision") or "QUERY").strip().upper() or "QUERY",
                    "remark_template": str(criterion.get("remark_template") or "").strip() or None,
                    "severity": str(criterion.get("severity") or "SOFT_QUERY").strip().upper() or "SOFT_QUERY",
                    "priority": int(criterion.get("priority") or 999),
                    "required_evidence_json": json.dumps(criterion.get("required_evidence") or []),
                    "version": "legacy-sync-v1",
                    "source": source_tag,
                },
            )
            criteria_upserted += 1

    return {"rules_upserted": rules_upserted, "criteria_upserted": criteria_upserted}

def get_checklist_catalog(force_refresh: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    now = time.time()
    if not force_refresh and _CACHE["rules"] and (now - float(_CACHE["loaded_at"])) < _CACHE_TTL_SECONDS:
        return _CACHE["rules"], _CACHE["criteria"], {
            "catalog_source": _CACHE["source"],
            "rules_count": len(_CACHE["rules"]),
            "criteria_count": len(_CACHE["criteria"]),
            "cache": "hit",
        }

    try:
        rules, criteria = _load_from_modern_postgres()
        source = "modern_postgres"
    except ChecklistSourceError:
        try:
            rules, criteria = _load_from_legacy_mysql()
            source = "legacy_mysql"
        except ChecklistSourceError:
            rules = FALLBACK_RULES
            criteria = FALLBACK_DIAGNOSIS
            source = "seed_fallback"

    sync_counts = {"rules_upserted": 0, "criteria_upserted": 0}
    sync_error = ""
    if source in {"legacy_mysql", "seed_fallback"}:
        try:
            sync_counts = _upsert_catalog_to_modern_postgres(rules, criteria, source)
        except Exception as exc:
            sync_error = str(exc)

    _CACHE.update({"loaded_at": now, "rules": rules, "criteria": criteria, "source": source})

    summary = {
        "catalog_source": source,
        "rules_count": len(rules),
        "criteria_count": len(criteria),
        "cache": "miss",
    }
    if source in {"legacy_mysql", "seed_fallback"}:
        summary["modern_sync"] = {
            "attempted": True,
            "rules_upserted": int(sync_counts.get("rules_upserted") or 0),
            "criteria_upserted": int(sync_counts.get("criteria_upserted") or 0),
            "error": sync_error,
        }

    return rules, criteria, summary








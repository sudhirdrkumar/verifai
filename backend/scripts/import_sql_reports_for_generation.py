import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import SessionLocal
from app.services.sql_dump_parser import iter_table_rows_from_sql_dump_path


def parse_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", errors="ignore")
    if isinstance(raw, str):
        text_value = raw.strip()
        if not text_value:
            return default
        try:
            parsed = json.loads(text_value)
            return parsed
        except Exception:
            return default
    return default


def map_decision(admission_required: Any) -> tuple[str, str, bool, int]:
    ar = str(admission_required or "uncertain").strip().lower()
    if ar == "yes":
        return "approve", "auto_approve_queue", False, 4
    if ar == "no":
        return "reject", "reject_queue", True, 1
    return "manual_review", "manual_review_queue", True, 3


def to_timestamp(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except Exception:
        return s


def load_claim_map(db) -> dict[str, str]:
    rows = db.execute(text("SELECT id::text AS id, external_claim_id FROM claims")).mappings().all()
    return {str(r.get("external_claim_id") or "").strip(): str(r.get("id") or "") for r in rows if r.get("external_claim_id")}


def load_decision_map(db) -> dict[int, str]:
    rows = db.execute(
        text(
            """
            SELECT legacy_analysis_id, id::text AS id
            FROM decision_results
            WHERE legacy_analysis_id IS NOT NULL
            """
        )
    ).mappings().all()
    out: dict[int, str] = {}
    for row in rows:
        try:
            key = int(row.get("legacy_analysis_id"))
            out[key] = str(row.get("id") or "")
        except Exception:
            continue
    return out


def load_report_map(db) -> dict[str, str]:
    rows = db.execute(
        text(
            """
            SELECT decision_id::text AS decision_id, id::text AS id
            FROM report_versions
            WHERE decision_id IS NOT NULL
            """
        )
    ).mappings().all()
    return {str(r.get("decision_id") or ""): str(r.get("id") or "") for r in rows if r.get("decision_id")}


def load_next_versions(db) -> dict[str, int]:
    rows = db.execute(
        text(
            """
            SELECT claim_id::text AS claim_id, COALESCE(MAX(version_no), 0) AS max_version
            FROM report_versions
            GROUP BY claim_id
            """
        )
    ).mappings().all()
    return {str(r.get("claim_id") or ""): int(r.get("max_version") or 0) + 1 for r in rows}


def upsert_decision_result(
    db,
    *,
    decision_id: str | None,
    legacy_analysis_id: int,
    claim_id: str,
    doctor_username: str,
    model_name: str,
    admission_required: str,
    confidence_raw: Any,
    rationale: str,
    evidence: Any,
    missing_info: Any,
    disclaimer: str,
    report_html: str,
    raw_response: Any,
    created_at: Any,
) -> str:
    recommendation, route_target, manual_review_required, review_priority = map_decision(admission_required)

    qc_risk = None
    try:
        if confidence_raw is not None and str(confidence_raw).strip() != "":
            qc_risk = max(0.0, min(1.0, float(confidence_raw) / 100.0))
    except Exception:
        qc_risk = None

    consistency_checks = missing_info if isinstance(missing_info, list) else [missing_info]
    rule_hits = evidence if isinstance(evidence, list) else [evidence]

    decision_payload = {
        "legacy_analysis_id": legacy_analysis_id,
        "legacy_claim_id": claim_id,
        "admission_required": admission_required,
        "rationale": rationale,
        "disclaimer": disclaimer,
        "report_html": report_html,
        "raw_response": raw_response,
        "evidence": evidence,
        "missing_information": missing_info,
    }

    params = {
        "legacy_analysis_id": legacy_analysis_id,
        "claim_id": claim_id,
        "rule_version": "legacy-sql-openai-v1",
        "model_version": model_name or "legacy-openai",
        "qc_risk_score": qc_risk,
        "consistency_checks": json.dumps(consistency_checks, ensure_ascii=False),
        "rule_hits": json.dumps(rule_hits, ensure_ascii=False),
        "explanation_summary": (rationale or "")[:4000] or None,
        "recommendation": recommendation,
        "route_target": route_target,
        "manual_review_required": manual_review_required,
        "review_priority": review_priority,
        "decision_payload": json.dumps(decision_payload, ensure_ascii=False),
        "generated_by": doctor_username or "legacy_sql_import",
        "generated_at": to_timestamp(created_at),
    }

    if decision_id:
        row = db.execute(
            text(
                """
                UPDATE decision_results
                SET
                    claim_id = CAST(:claim_id AS uuid),
                    model_version = :model_version,
                    qc_risk_score = :qc_risk_score,
                    consistency_checks = CAST(:consistency_checks AS jsonb),
                    rule_hits = CAST(:rule_hits AS jsonb),
                    explanation_summary = :explanation_summary,
                    recommendation = :recommendation,
                    route_target = :route_target,
                    manual_review_required = :manual_review_required,
                    review_priority = :review_priority,
                    decision_payload = CAST(:decision_payload AS jsonb),
                    generated_by = :generated_by,
                    generated_at = COALESCE(:generated_at, generated_at)
                WHERE id = CAST(:id AS uuid)
                RETURNING id::text AS id
                """
            ),
            {**params, "id": decision_id},
        ).mappings().first()
        if row and row.get("id"):
            return str(row["id"])

    row = db.execute(
        text(
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
                :legacy_analysis_id,
                CAST(:claim_id AS uuid),
                :rule_version,
                :model_version,
                :qc_risk_score,
                CAST(:consistency_checks AS jsonb),
                CAST(:rule_hits AS jsonb),
                :explanation_summary,
                :recommendation,
                :route_target,
                :manual_review_required,
                :review_priority,
                CAST(:decision_payload AS jsonb),
                :generated_by,
                COALESCE(:generated_at, NOW()),
                FALSE
            )
            ON CONFLICT (legacy_analysis_id)
            DO UPDATE SET
                claim_id = EXCLUDED.claim_id,
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
            RETURNING id::text AS id
            """
        ),
        params,
    ).mappings().one()
    return str(row.get("id") or "")


def main() -> int:
    parser = argparse.ArgumentParser(description="Import report HTML from SQL dump for report generation")
    parser.add_argument("--sql-dump-path", required=True, help="Path to SQL dump")
    parser.add_argument("--limit", type=int, default=0, help="Optional max rows to process (0 = all)")
    args = parser.parse_args()

    sql_path = Path(args.sql_dump_path)
    if not sql_path.exists() or not sql_path.is_file():
        print(f"SQL dump not found: {sql_path}")
        return 1

    db = SessionLocal()
    try:
        claim_map = load_claim_map(db)
        decision_map = load_decision_map(db)
        report_map = load_report_map(db)
        next_versions = load_next_versions(db)

        processed = 0
        matched_claim = 0
        no_claim_match = 0
        no_report_html = 0
        decisions_upserted = 0
        reports_inserted = 0
        reports_updated = 0

        for row in iter_table_rows_from_sql_dump_path(sql_path, "openai_analysis_results"):
            processed += 1
            if args.limit and args.limit > 0 and processed > args.limit:
                break

            external_claim_id = str(row.get("claim_id") or "").strip()
            claim_uuid = claim_map.get(external_claim_id)
            if not claim_uuid:
                no_claim_match += 1
                continue
            matched_claim += 1

            report_html = str(row.get("report_html") or "")
            if not report_html.strip():
                no_report_html += 1
                continue

            try:
                legacy_analysis_id = int(row.get("id"))
            except Exception:
                continue

            evidence = parse_json(row.get("evidence_json"), [])
            missing_info = parse_json(row.get("missing_information_json"), [])
            raw_response = parse_json(row.get("raw_response_json"), {})

            decision_id = upsert_decision_result(
                db,
                decision_id=decision_map.get(legacy_analysis_id),
                legacy_analysis_id=legacy_analysis_id,
                claim_id=claim_uuid,
                doctor_username=str(row.get("doctor_username") or "legacy_sql_import"),
                model_name=str(row.get("model_name") or "legacy-openai"),
                admission_required=str(row.get("admission_required") or "uncertain"),
                confidence_raw=row.get("confidence"),
                rationale=str(row.get("rationale") or ""),
                evidence=evidence,
                missing_info=missing_info,
                disclaimer=str(row.get("disclaimer") or ""),
                report_html=report_html,
                raw_response=raw_response,
                created_at=row.get("created_at"),
            )
            decisions_upserted += 1
            decision_map[legacy_analysis_id] = decision_id

            existing_report_id = report_map.get(decision_id)
            if existing_report_id:
                db.execute(
                    text(
                        """
                        UPDATE report_versions
                        SET report_markdown = :report_markdown,
                            report_status = :report_status,
                            created_by = :created_by,
                            created_at = COALESCE(:created_at, created_at)
                        WHERE id = CAST(:id AS uuid)
                        """
                    ),
                    {
                        "id": existing_report_id,
                        "report_markdown": report_html,
                        "report_status": "completed",
                        "created_by": "system:legacy_sql_import",
                        "created_at": to_timestamp(row.get("created_at")),
                    },
                )
                reports_updated += 1
            else:
                version_no = int(next_versions.get(claim_uuid, 1))
                new_row = db.execute(
                    text(
                        """
                        INSERT INTO report_versions (
                            claim_id,
                            decision_id,
                            version_no,
                            report_status,
                            report_markdown,
                            export_uri,
                            created_by,
                            created_at
                        )
                        VALUES (
                            CAST(:claim_id AS uuid),
                            CAST(:decision_id AS uuid),
                            :version_no,
                            :report_status,
                            :report_markdown,
                            '',
                            :created_by,
                            COALESCE(:created_at, NOW())
                        )
                        RETURNING id::text AS id
                        """
                    ),
                    {
                        "claim_id": claim_uuid,
                        "decision_id": decision_id,
                        "version_no": version_no,
                        "report_status": "completed",
                        "report_markdown": report_html,
                        "created_by": "system:legacy_sql_import",
                        "created_at": to_timestamp(row.get("created_at")),
                    },
                ).mappings().one()
                report_map[decision_id] = str(new_row.get("id") or "")
                next_versions[claim_uuid] = version_no + 1
                reports_inserted += 1

        db.commit()

        print("SQL report import completed.")
        print(
            json.dumps(
                {
                    "processed": processed,
                    "matched_claim": matched_claim,
                    "no_claim_match": no_claim_match,
                    "no_report_html": no_report_html,
                    "decisions_upserted": decisions_upserted,
                    "reports_inserted": reports_inserted,
                    "reports_updated": reports_updated,
                },
                indent=2,
            )
        )
        return 0
    except Exception as exc:
        db.rollback()
        print(f"Failed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())


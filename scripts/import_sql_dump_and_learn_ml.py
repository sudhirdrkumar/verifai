import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db.session import SessionLocal
from app.services.ml_claim_model import ensure_model, generate_alignment_feedback_labels
from app.services.sql_dump_parser import iter_table_rows_from_sql_dump_path


def _ensure_claim_legacy_data_table(db) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS claim_legacy_data (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                legacy_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_claim_legacy_data_claim_id ON claim_legacy_data(claim_id)"))


def _fetch_claim_uuid_map(db) -> dict[str, str]:
    rows = db.execute(text("SELECT id::text AS id, external_claim_id FROM claims")).mappings().all()
    return {str(r.get("external_claim_id") or "").strip(): str(r.get("id") or "") for r in rows if r.get("external_claim_id")}


def _to_jsonable(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in row.items():
        normalized_key = str(key or "").strip()
        if not normalized_key:
            continue
        if value is None:
            out[normalized_key] = ""
        elif isinstance(value, (str, int, float, bool)):
            out[normalized_key] = value
        else:
            out[normalized_key] = str(value)
    return out


def import_sql_dump_rows(db, sql_dump_path: Path, claim_uuid_map: dict[str, str], limit: int = 0) -> dict[str, int]:
    parsed_rows = 0
    missing_claim_id = 0
    not_in_current_db = 0
    upserted = 0

    for row in iter_table_rows_from_sql_dump_path(sql_dump_path, "excel_case_uploads"):
        parsed_rows += 1
        external_claim_id = str(row.get("claim_id") or "").strip()
        if not external_claim_id:
            missing_claim_id += 1
            continue

        claim_uuid = claim_uuid_map.get(external_claim_id)
        if not claim_uuid:
            not_in_current_db += 1
            continue

        payload = _to_jsonable(row)
        payload.setdefault("claim_id", external_claim_id)
        payload.setdefault("source_file_name", sql_dump_path.name)
        payload.setdefault("uploaded_by_username", str(payload.get("uploaded_by_username") or ""))

        db.execute(
            text(
                """
                INSERT INTO claim_legacy_data (claim_id, legacy_payload, updated_at)
                VALUES (:claim_id, CAST(:legacy_payload AS jsonb), NOW())
                ON CONFLICT (claim_id)
                DO UPDATE SET
                    legacy_payload = EXCLUDED.legacy_payload,
                    updated_at = NOW()
                """
            ),
            {
                "claim_id": claim_uuid,
                "legacy_payload": json.dumps(payload, ensure_ascii=False),
            },
        )
        upserted += 1

        if limit > 0 and upserted >= limit:
            break

    return {
        "parsed_rows": parsed_rows,
        "missing_claim_id": missing_claim_id,
        "not_in_current_db": not_in_current_db,
        "upserted": upserted,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import legacy SQL dump rows into claim_legacy_data and train ML from extraction-vs-report alignment"
    )
    parser.add_argument(
        "--sql-dump-path",
        required=True,
        help="Path to legacy SQL dump (.sql)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional limit for number of matched rows to upsert (0 = all)",
    )
    parser.add_argument(
        "--skip-alignment-labels",
        action="store_true",
        help="Skip auto-generation of alignment labels",
    )
    parser.add_argument(
        "--overwrite-alignment-labels",
        action="store_true",
        help="Overwrite existing alignment labels created by system",
    )
    parser.add_argument(
        "--skip-train",
        action="store_true",
        help="Skip ML retraining",
    )
    args = parser.parse_args()

    sql_dump_path = Path(args.sql_dump_path)
    if not sql_dump_path.exists() or not sql_dump_path.is_file():
        print(f"SQL dump not found: {sql_dump_path}")
        return 1

    db = SessionLocal()
    try:
        _ensure_claim_legacy_data_table(db)
        claim_uuid_map = _fetch_claim_uuid_map(db)
        print(f"Current claims in PostgreSQL: {len(claim_uuid_map)}")

        import_summary = import_sql_dump_rows(
            db=db,
            sql_dump_path=sql_dump_path,
            claim_uuid_map=claim_uuid_map,
            limit=max(0, int(args.limit or 0)),
        )

        alignment_summary: dict[str, int] = {
            "processed": 0,
            "inserted": 0,
            "skipped_existing": 0,
            "skipped_insufficient": 0,
        }
        if not args.skip_alignment_labels:
            alignment_summary = generate_alignment_feedback_labels(
                db=db,
                created_by="system:ml_alignment:import_sql_dump",
                overwrite=bool(args.overwrite_alignment_labels),
            )

        model = None
        if not args.skip_train:
            model = ensure_model(db=db, force_retrain=True)

        db.commit()

        print("Import summary:")
        print(json.dumps(import_summary, indent=2))
        if not args.skip_alignment_labels:
            print("Alignment-label summary:")
            print(json.dumps(alignment_summary, indent=2))

        if args.skip_train:
            print("ML training skipped.")
        elif isinstance(model, dict):
            print("ML model trained.")
            print(f"Version: {model.get('version')}")
            print(f"Examples: {model.get('num_examples')}")
            print(f"Label counts: {model.get('label_counts')}")
        else:
            print("ML model could not be trained. Need more labeled data.")

        return 0
    except Exception as exc:
        db.rollback()
        print(f"Failed: {exc}")
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())

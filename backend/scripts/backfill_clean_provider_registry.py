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
from app.services.claim_structuring_service import _ensure_table, sync_clean_provider_registry_for_claim


def _safe_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill clean hospital->treating doctor->registration mappings from claim_structured_data"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of structured rows to process (0 = all)",
    )
    args = parser.parse_args()

    with SessionLocal() as db:
        _ensure_table(db)

        removed_orphans = db.execute(
            text(
                """
                DELETE FROM claim_provider_registry_clean cprc
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM claim_structured_data csd
                    WHERE csd.claim_id = cprc.claim_id
                )
                """
            )
        ).rowcount or 0

        base_sql = (
            """
            SELECT
                claim_id,
                external_claim_id,
                insured_name,
                hospital_name,
                treating_doctor,
                treating_doctor_registration_number,
                source,
                confidence,
                raw_payload
            FROM claim_structured_data
            ORDER BY updated_at DESC
            """
        )
        if args.limit and args.limit > 0:
            rows = db.execute(text(base_sql + " LIMIT :limit"), {"limit": int(args.limit)}).mappings().all()
        else:
            rows = db.execute(text(base_sql)).mappings().all()

        processed = 0
        mapped = 0
        skipped_or_removed = 0

        for row in rows:
            fields = {
                "insured_name": str(row.get("insured_name") or ""),
                "hospital_name": str(row.get("hospital_name") or ""),
                "treating_doctor": str(row.get("treating_doctor") or ""),
                "treating_doctor_registration_number": str(row.get("treating_doctor_registration_number") or ""),
            }
            synced = sync_clean_provider_registry_for_claim(
                db=db,
                claim_id=row.get("claim_id"),
                external_claim_id=str(row.get("external_claim_id") or ""),
                fields=fields,
                source=str(row.get("source") or "claim_structured_data"),
                confidence=row.get("confidence"),
                raw_payload=_safe_payload(row.get("raw_payload")),
            )
            processed += 1
            if synced:
                mapped += 1
            else:
                skipped_or_removed += 1

        db.commit()

    print(
        json.dumps(
            {
                "processed": processed,
                "mapped": mapped,
                "skipped_or_removed": skipped_or_removed,
                "removed_orphans": removed_orphans,
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

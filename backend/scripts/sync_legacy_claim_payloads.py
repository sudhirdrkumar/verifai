import argparse
import json
import math
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


LEGACY_TABLE = "excel_case_uploads"


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
        read_timeout=60,
        write_timeout=60,
        autocommit=True,
    )


def connect_modern_postgres():
    return psycopg.connect(settings.psycopg_database_uri)


def ensure_claim_legacy_data_table(pg_conn) -> None:
    with pg_conn.cursor() as cur:
        cur.execute(
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
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_claim_legacy_data_claim_id ON claim_legacy_data(claim_id)"
        )
    pg_conn.commit()


def fetch_claim_uuid_map(pg_conn) -> dict[str, str]:
    out: dict[str, str] = {}
    with pg_conn.cursor() as cur:
        cur.execute("SELECT id::text, external_claim_id FROM claims")
        for claim_id, external_claim_id in cur.fetchall():
            out[str(external_claim_id)] = str(claim_id)
    return out


def fetch_legacy_claim_rows(mysql_conn, claim_ids: list[str]) -> list[dict[str, Any]]:
    if not claim_ids:
        return []

    rows: list[dict[str, Any]] = []
    chunk_size = 500
    total_chunks = int(math.ceil(len(claim_ids) / chunk_size))

    with mysql_conn.cursor() as cur:
        for idx in range(total_chunks):
            chunk = claim_ids[idx * chunk_size : (idx + 1) * chunk_size]
            placeholders = ",".join(["%s"] * len(chunk))
            cur.execute(
                f"SELECT * FROM {LEGACY_TABLE} WHERE claim_id IN ({placeholders})",
                tuple(chunk),
            )
            rows.extend(cur.fetchall() or [])
    return rows


def to_jsonable(row: dict[str, Any]) -> dict[str, Any]:
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


def upsert_claim_legacy_rows(pg_conn, claim_uuid_map: dict[str, str], rows: list[dict[str, Any]]) -> int:
    upserted = 0
    with pg_conn.cursor() as cur:
        for row in rows:
            external_claim_id = str(row.get("claim_id") or "").strip()
            if not external_claim_id:
                continue
            claim_uuid = claim_uuid_map.get(external_claim_id)
            if not claim_uuid:
                continue

            payload = to_jsonable(row)
            payload.setdefault("claim_id", external_claim_id)
            payload.setdefault("source_file_name", "")
            payload.setdefault("uploaded_by_username", "")

            cur.execute(
                """
                INSERT INTO claim_legacy_data (claim_id, legacy_payload, updated_at)
                VALUES (%s, %s::jsonb, NOW())
                ON CONFLICT (claim_id)
                DO UPDATE SET
                    legacy_payload = EXCLUDED.legacy_payload,
                    updated_at = NOW()
                """,
                (claim_uuid, json.dumps(payload)),
            )
            upserted += 1
    pg_conn.commit()
    return upserted


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync legacy excel_case_uploads rows into claim_legacy_data for claims present in current DB"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Optional max number of current claims to sync (0 = all)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be synced without writing to PostgreSQL",
    )
    args = parser.parse_args()

    try:
        mysql_conn = connect_legacy_mysql()
    except Exception as exc:
        print("Could not connect to legacy MySQL.")
        print(f"Connection target: {settings.legacy_db_host}:{settings.legacy_db_port}/{settings.legacy_db_name}")
        print(f"Error: {exc}")
        return 1

    try:
        pg_conn = connect_modern_postgres()
    except OperationalError as exc:
        print("Could not connect to PostgreSQL.")
        print(f"Connection target: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
        print(f"Error: {exc}")
        mysql_conn.close()
        return 1

    try:
        ensure_claim_legacy_data_table(pg_conn)
        claim_uuid_map = fetch_claim_uuid_map(pg_conn)
        claim_ids = sorted(claim_uuid_map.keys())
        if args.limit and args.limit > 0:
            claim_ids = claim_ids[: args.limit]

        print(f"Current claims in PostgreSQL: {len(claim_ids)}")
        legacy_rows = fetch_legacy_claim_rows(mysql_conn, claim_ids)
        print(f"Matching legacy rows found in {LEGACY_TABLE}: {len(legacy_rows)}")

        if args.dry_run:
            print("Dry-run only. No writes performed.")
            return 0

        upserted = upsert_claim_legacy_rows(pg_conn, claim_uuid_map, legacy_rows)
        print(f"claim_legacy_data upserted rows: {upserted}")
        print("Legacy payload sync completed.")
        return 0
    finally:
        mysql_conn.close()
        pg_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

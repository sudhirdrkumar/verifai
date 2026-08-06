#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import psycopg


TARGET_EXTERNAL_IDS = ["139297434", "49006410", "139544809", "139583499"]
BACKUP_PATH = "/home/ec2-user/qc-python/artifacts/ec2_full_20260329_141544.dump"
TMP_DB = "qc_tmp_20260329_check"


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if len(v) >= 2 and ((v[0] == '"' and v[-1] == '"') or (v[0] == "'" and v[-1] == "'")):
            v = v[1:-1]
        env[k] = v
    return env


def fetch_claims(conn: psycopg.Connection) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT external_claim_id, status, assigned_doctor_id, completed_at, updated_at
            FROM claims
            WHERE external_claim_id = ANY(%s)
            ORDER BY external_claim_id
            """,
            (TARGET_EXTERNAL_IDS,),
        )
        return cur.fetchall()


def main() -> int:
    env = load_env(Path(".env"))
    host = env.get("PG_HOST", "127.0.0.1")
    port = int(env.get("PG_PORT", "5432"))
    user = env.get("PG_USER", "postgres")
    password = env.get("PG_PASSWORD", "")
    dbname = env.get("PG_DATABASE", "qc_bkp_modern")

    pg_env = os.environ.copy()
    pg_env["PGPASSWORD"] = password

    with psycopg.connect(host=host, port=port, user=user, password=password, dbname="postgres", autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TMP_DB}")
            cur.execute(f"CREATE DATABASE {TMP_DB}")

    subprocess.run(
        [
            "pg_restore",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            TMP_DB,
            BACKUP_PATH,
        ],
        check=True,
        env=pg_env,
    )

    with psycopg.connect(host=host, port=port, user=user, password=password, dbname=TMP_DB) as backup_conn:
        backup_rows = fetch_claims(backup_conn)

    with psycopg.connect(host=host, port=port, user=user, password=password, dbname=dbname) as live_conn:
        live_rows = fetch_claims(live_conn)

    with psycopg.connect(host=host, port=port, user=user, password=password, dbname="postgres", autocommit=True) as admin:
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {TMP_DB}")

    print(
        json.dumps(
            {
                "backup_path": BACKUP_PATH,
                "target_external_ids": TARGET_EXTERNAL_IDS,
                "backup_rows": backup_rows,
                "live_rows": live_rows,
            },
            default=str,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

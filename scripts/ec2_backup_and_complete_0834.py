#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg


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


def main() -> int:
    env = load_env(Path(".env"))
    host = env.get("PG_HOST", "127.0.0.1")
    port = int(env.get("PG_PORT", "5432"))
    user = env.get("PG_USER", "postgres")
    password = env.get("PG_PASSWORD", "")
    dbname = env.get("PG_DATABASE", "qc_bkp_modern")

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = Path("/home/ec2-user/db_backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_file = backup_dir / f"{dbname}_before_mark_completed_0834_{ts}.dump"

    pg_env = os.environ.copy()
    pg_env["PGPASSWORD"] = password
    subprocess.run(
        [
            "pg_dump",
            "-Fc",
            "-h",
            host,
            "-p",
            str(port),
            "-U",
            user,
            "-d",
            dbname,
            "-f",
            str(backup_file),
        ],
        check=True,
        env=pg_env,
    )

    with psycopg.connect(host=host, port=port, user=user, password=password, dbname=dbname) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                WITH last_status_evt AS (
                    SELECT DISTINCT ON (we.claim_id)
                        we.claim_id,
                        COALESCE(we.event_payload->>'status', '') AS last_status_event_status
                    FROM workflow_events we
                    WHERE we.event_type = 'claim_status_updated'
                    ORDER BY we.claim_id, we.occurred_at DESC
                ),
                intake_evt AS (
                    SELECT DISTINCT ON (we.claim_id)
                        we.claim_id
                    FROM workflow_events we
                    WHERE we.event_type = 'teamrightworks_case_intake'
                      AND we.occurred_at >= TIMESTAMPTZ '2026-04-06 08:34:00+00'
                      AND we.occurred_at <  TIMESTAMPTZ '2026-04-06 08:35:00+00'
                    ORDER BY we.claim_id, we.occurred_at DESC
                )
                SELECT c.external_claim_id
                FROM claims c
                JOIN intake_evt ie ON ie.claim_id = c.id
                LEFT JOIN last_status_evt lse ON lse.claim_id = c.id
                WHERE c.status = 'in_review'
                  AND COALESCE(lse.last_status_event_status, '') = 'completed'
                ORDER BY c.updated_at DESC
                """
            )
            target_claims = [row[0] for row in cur.fetchall()]

            updated_count = 0
            if target_claims:
                cur.execute(
                    """
                    UPDATE claims
                    SET status = 'completed',
                        updated_at = NOW()
                    WHERE external_claim_id = ANY(%s)
                    """,
                    (target_claims,),
                )
                updated_count = cur.rowcount

    print(
        json.dumps(
            {
                "backup_file": str(backup_file),
                "target_claim_count": len(target_claims),
                "updated_count": updated_count,
                "claims": target_claims,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

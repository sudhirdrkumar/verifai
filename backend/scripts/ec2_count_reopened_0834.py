#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path

import psycopg


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main() -> int:
    env = load_env(Path(".env"))
    conn = psycopg.connect(
        host=env.get("PG_HOST", "127.0.0.1"),
        port=int(env.get("PG_PORT", "5432")),
        user=env.get("PG_USER", "postgres"),
        password=env.get("PG_PASSWORD", ""),
        dbname=env.get("PG_DATABASE", "qc_bkp_modern"),
    )

    with conn, conn.cursor() as cur:
        cur.execute(
            """
            WITH last_status_evt AS (
                SELECT DISTINCT ON (we.claim_id)
                    we.claim_id,
                    COALESCE(we.event_payload->>'status', '') AS last_status_event_status,
                    we.occurred_at AS last_status_event_at
                FROM workflow_events we
                WHERE we.event_type = 'claim_status_updated'
                ORDER BY we.claim_id, we.occurred_at DESC
            ),
            intake_evt AS (
                SELECT DISTINCT ON (we.claim_id)
                    we.claim_id,
                    we.occurred_at AS intake_at
                FROM workflow_events we
                WHERE we.event_type = 'teamrightworks_case_intake'
                  AND we.occurred_at >= TIMESTAMPTZ '2026-04-06 08:34:00+00'
                  AND we.occurred_at <  TIMESTAMPTZ '2026-04-06 08:35:00+00'
                ORDER BY we.claim_id, we.occurred_at DESC
            )
            SELECT
                c.external_claim_id,
                c.status,
                c.updated_at,
                lse.last_status_event_status,
                lse.last_status_event_at,
                ie.intake_at
            FROM claims c
            JOIN intake_evt ie ON ie.claim_id = c.id
            LEFT JOIN last_status_evt lse ON lse.claim_id = c.id
            WHERE c.status = 'in_review'
              AND COALESCE(lse.last_status_event_status, '') = 'completed'
            ORDER BY c.updated_at DESC
            """
        )
        rows = cur.fetchall()

    payload = {
        "reopened_count_0834_utc": len(rows),
        "claims": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


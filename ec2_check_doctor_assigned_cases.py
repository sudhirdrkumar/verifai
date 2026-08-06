#!/usr/bin/env python3
from __future__ import annotations

import json
import re
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


def norm_token(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().lower())


def main() -> int:
    env = load_env(Path(".env"))
    conn = psycopg.connect(
        host=env.get("PG_HOST", "127.0.0.1"),
        port=int(env.get("PG_PORT", "5432")),
        user=env.get("PG_USER", "postgres"),
        password=env.get("PG_PASSWORD", ""),
        dbname=env.get("PG_DATABASE", "qc_bkp_modern"),
    )

    output: dict[str, object] = {}
    with conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, username, role
            FROM users
            WHERE LOWER(username) LIKE '%ragh%'
            ORDER BY username
            """
        )
        users = cur.fetchall()
        output["users_like_ragh"] = users

        for _id, username, role in users:
            if str(role or "").strip().lower() != "doctor":
                continue
            token = norm_token(str(username))
            cur.execute(
                """
                WITH doc_stats AS (
                    SELECT claim_id, COUNT(*) AS documents
                    FROM claim_documents
                    GROUP BY claim_id
                ),
                latest_assignment AS (
                    SELECT DISTINCT ON (claim_id)
                        claim_id,
                        occurred_at AS assigned_at,
                        DATE(occurred_at) AS allotment_date
                    FROM workflow_events
                    WHERE event_type = 'claim_assigned'
                    ORDER BY claim_id, occurred_at DESC
                )
                SELECT
                    c.external_claim_id,
                    c.status,
                    c.assigned_doctor_id,
                    COALESCE(NULLIF(TRIM(um.tagging), ''), '') AS tagging,
                    COALESCE(NULLIF(TRIM(um.subtagging), ''), '') AS subtagging,
                    CASE WHEN NULLIF(TRIM(COALESCE(um.opinion, '')), '') IS NOT NULL THEN 'yes' ELSE 'no' END AS has_opinion,
                    COALESCE(um.report_export_status, '') AS report_export_status,
                    COALESCE(ds.documents, 0) AS documents,
                    la.assigned_at,
                    la.allotment_date,
                    c.updated_at
                FROM claims c
                LEFT JOIN claim_report_uploads um ON um.claim_id = c.id
                LEFT JOIN doc_stats ds ON ds.claim_id = c.id
                LEFT JOIN latest_assignment la ON la.claim_id = c.id
                WHERE %s = ANY(string_to_array(regexp_replace(LOWER(COALESCE(c.assigned_doctor_id, '')), '[^a-z0-9,]+', '', 'g'), ','))
                  AND c.status <> 'completed'
                  AND c.status <> 'withdrawn'
                  AND NULLIF(TRIM(COALESCE(um.tagging, '')), '') IS NULL
                ORDER BY COALESCE(la.allotment_date, DATE(c.updated_at)) ASC, c.updated_at ASC
                """,
                (token,),
            )
            rows = cur.fetchall()
            output[f"doctor_{username}_token_{token}_count"] = len(rows)
            output[f"doctor_{username}_rows"] = rows

    print(json.dumps(output, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

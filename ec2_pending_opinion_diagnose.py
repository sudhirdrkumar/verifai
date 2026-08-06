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


def norm_token(value: str | None) -> str:
    raw = (value or "").strip().lower()
    return "".join(ch for ch in raw if ch.isalnum())


def main() -> int:
    env = load_env(Path(".env"))
    conn = psycopg.connect(
        host=env.get("PG_HOST", "127.0.0.1"),
        port=int(env.get("PG_PORT", "5432")),
        user=env.get("PG_USER", "postgres"),
        password=env.get("PG_PASSWORD", ""),
        dbname=env.get("PG_DATABASE", "qc_bkp_modern"),
    )

    out: dict[str, object] = {}
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
        out["users_like_ragh"] = users

        cur.execute(
            """
            SELECT
                LOWER(REGEXP_REPLACE(COALESCE(c.assigned_doctor_id,''), '[^a-z0-9]+', '', 'g')) AS doctor_key,
                COUNT(*) AS total_claims,
                SUM(CASE WHEN c.status = 'waiting_for_documents' THEN 1 ELSE 0 END) AS waiting_for_documents,
                SUM(CASE WHEN c.status = 'in_review' THEN 1 ELSE 0 END) AS in_review,
                SUM(CASE WHEN c.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN c.status = 'withdrawn' THEN 1 ELSE 0 END) AS withdrawn,
                SUM(CASE WHEN NULLIF(TRIM(COALESCE(cru.opinion,'')), '') IS NOT NULL THEN 1 ELSE 0 END) AS with_opinion,
                SUM(
                    CASE
                        WHEN NULLIF(TRIM(COALESCE(cru.opinion,'')), '') IS NOT NULL
                         AND c.status NOT IN ('completed','withdrawn')
                        THEN 1 ELSE 0
                    END
                ) AS opinion_but_open
            FROM claims c
            LEFT JOIN claim_report_uploads cru ON cru.claim_id = c.id
            WHERE NULLIF(TRIM(COALESCE(c.assigned_doctor_id,'')), '') IS NOT NULL
            GROUP BY doctor_key
            HAVING SUM(
                    CASE
                        WHEN NULLIF(TRIM(COALESCE(cru.opinion,'')), '') IS NOT NULL
                         AND c.status NOT IN ('completed','withdrawn')
                        THEN 1 ELSE 0
                    END
                ) > 0
            ORDER BY opinion_but_open DESC, doctor_key
            LIMIT 50
            """
        )
        out["top_doctors_opinion_but_open"] = cur.fetchall()

        ragh_keys = sorted(
            {
                norm_token(str(username))
                for _id, username, _role in users
                if "ragh" in norm_token(str(username))
            }
            | {"drraghvendra", "draghvendra"}
        )
        out["ragh_keys_checked"] = ragh_keys

        all_ragh_rows: list[tuple] = []
        all_ragh_sample_claim_ids: list[str] = []
        all_ragh_claim_ids: list[str] = []
        all_ragh_updated_ats: list[str] = []
        for ragh_key in ragh_keys:
            cur.execute(
                """
                SELECT
                    c.id,
                    c.external_claim_id,
                    c.status,
                    c.assigned_doctor_id,
                    COALESCE(cru.report_export_status, '') AS report_export_status,
                    COALESCE(cru.tagging, '') AS tagging,
                    LEFT(COALESCE(cru.opinion, ''), 220) AS opinion_preview,
                    (SELECT COUNT(*) FROM claim_documents d WHERE d.claim_id = c.id) AS documents,
                    (
                        SELECT COALESCE(we.event_payload->>'status', '')
                        FROM workflow_events we
                        WHERE we.claim_id = c.id
                          AND we.event_type = 'claim_status_updated'
                        ORDER BY we.occurred_at DESC
                        LIMIT 1
                    ) AS last_status_event_status,
                    (
                        SELECT we.occurred_at
                        FROM workflow_events we
                        WHERE we.claim_id = c.id
                          AND we.event_type = 'claim_status_updated'
                        ORDER BY we.occurred_at DESC
                        LIMIT 1
                    ) AS last_status_event_at,
                    c.updated_at
                FROM claims c
                LEFT JOIN claim_report_uploads cru ON cru.claim_id = c.id
                WHERE LOWER(REGEXP_REPLACE(COALESCE(c.assigned_doctor_id,''), '[^a-z0-9]+', '', 'g')) = %s
                  AND NULLIF(TRIM(COALESCE(cru.opinion,'')), '') IS NOT NULL
                  AND c.status NOT IN ('completed','withdrawn')
                ORDER BY c.updated_at DESC
                LIMIT 200
                """,
                (ragh_key,),
            )
            rows = cur.fetchall()
            out[f"opinion_but_open_claims__{ragh_key}"] = rows
            all_ragh_rows.extend(rows)
            all_ragh_sample_claim_ids.extend([r[0] for r in rows[:10]])
            all_ragh_claim_ids.extend([r[0] for r in rows])
            all_ragh_updated_ats.extend([r[10] for r in rows if len(r) > 10 and r[10] is not None])

        if all_ragh_sample_claim_ids:
            cur.execute(
                """
                SELECT
                    we.claim_id,
                    we.event_type,
                    COALESCE(we.event_payload::text, '') AS payload_text,
                    we.occurred_at
                FROM workflow_events we
                WHERE we.claim_id = ANY(%s)
                  AND we.event_type IN ('claim_status_updated', 'claim_assigned')
                ORDER BY we.occurred_at DESC
                """,
                (list(dict.fromkeys(all_ragh_sample_claim_ids)),),
            )
            out["sample_workflow_events_for_ragh"] = cur.fetchall()

        if all_ragh_claim_ids:
            unique_claim_ids = list(dict.fromkeys(all_ragh_claim_ids))
            cur.execute(
                """
                SELECT c.external_claim_id, c.status, c.updated_at,
                       COALESCE(cru.report_export_status, '') AS report_export_status,
                       COALESCE(cru.tagging, '') AS tagging,
                       COALESCE(cru.subtagging, '') AS subtagging,
                       CASE WHEN NULLIF(TRIM(COALESCE(cru.opinion, '')), '') IS NOT NULL THEN 'yes' ELSE 'no' END AS has_opinion,
                       cru.updated_at AS upload_meta_updated_at
                FROM claims c
                LEFT JOIN claim_report_uploads cru ON cru.claim_id = c.id
                WHERE c.id = ANY(%s)
                ORDER BY c.updated_at DESC
                """,
                (unique_claim_ids,),
            )
            out["ragh_claims_current_state_with_upload_meta"] = cur.fetchall()

            cur.execute(
                """
                SELECT event_type, COUNT(*) AS cnt, MIN(occurred_at), MAX(occurred_at)
                FROM workflow_events
                WHERE claim_id = ANY(%s)
                GROUP BY event_type
                ORDER BY cnt DESC, event_type
                """,
                (unique_claim_ids,),
            )
            out["workflow_event_type_counts_for_ragh_open_claims"] = cur.fetchall()

        # Query claims that UI would label "pending" (waiting_for_documents + docs>0)
        cur.execute(
            """
            WITH doc_stats AS (
                SELECT claim_id, COUNT(*) AS documents
                FROM claim_documents
                GROUP BY claim_id
            )
            SELECT
                LOWER(REGEXP_REPLACE(COALESCE(c.assigned_doctor_id,''), '[^a-z0-9]+', '', 'g')) AS doctor_key,
                COUNT(*) AS pending_display_count,
                SUM(CASE WHEN NULLIF(TRIM(COALESCE(cru.opinion,'')), '') IS NOT NULL THEN 1 ELSE 0 END) AS pending_with_opinion
            FROM claims c
            LEFT JOIN doc_stats ds ON ds.claim_id = c.id
            LEFT JOIN claim_report_uploads cru ON cru.claim_id = c.id
            WHERE c.status = 'waiting_for_documents'
              AND COALESCE(ds.documents, 0) > 0
            GROUP BY doctor_key
            HAVING COUNT(*) > 0
            ORDER BY pending_with_opinion DESC, pending_display_count DESC
            LIMIT 50
            """
        )
        out["pending_display_by_doctor"] = cur.fetchall()

    print(json.dumps(out, ensure_ascii=False, default=str, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

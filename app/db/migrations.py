import logging

from sqlalchemy import text

from app.db.session import engine

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Migration registry — append new entries to add schema changes.
# Each entry: (version: int, description: str, sql_statements: list[str])
# Versions must be sequential integers starting at 1.
# A migration only runs when its version > the highest version in the DB.
# ─────────────────────────────────────────────────────────────────────────────
_MIGRATIONS: list[tuple[int, str, list[str]]] = [
    (
        1,
        "create claim_report_uploads table and indexes",
        [
            """
            CREATE TABLE IF NOT EXISTS claim_report_uploads (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                report_export_status VARCHAR(30) NOT NULL DEFAULT 'pending',
                tagging VARCHAR(120),
                subtagging VARCHAR(120),
                opinion TEXT,
                qc_status VARCHAR(10) NOT NULL DEFAULT 'no',
                auditor_rating SMALLINT,
                auditor_rating_by VARCHAR(100),
                auditor_rating_at TIMESTAMPTZ,
                updated_by VARCHAR(100),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "ALTER TABLE claim_report_uploads ADD COLUMN IF NOT EXISTS auditor_rating SMALLINT",
            "ALTER TABLE claim_report_uploads ADD COLUMN IF NOT EXISTS auditor_rating_by VARCHAR(100)",
            "ALTER TABLE claim_report_uploads ADD COLUMN IF NOT EXISTS auditor_rating_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS idx_claim_report_uploads_claim_id ON claim_report_uploads(claim_id)",
        ],
    ),
    (
        2,
        "create claim_legacy_data table and indexes",
        [
            """
            CREATE TABLE IF NOT EXISTS claim_legacy_data (
                id BIGSERIAL PRIMARY KEY,
                claim_id UUID NOT NULL UNIQUE REFERENCES claims(id) ON DELETE CASCADE,
                legacy_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_claim_legacy_data_claim_id ON claim_legacy_data(claim_id)",
        ],
    ),
    (
        3,
        "add completed_at column to claims with backfill",
        [
            "ALTER TABLE claims ADD COLUMN IF NOT EXISTS completed_at TIMESTAMPTZ",
            "CREATE INDEX IF NOT EXISTS idx_claims_completed_at ON claims(completed_at)",
            """
            WITH first_completed AS (
                SELECT
                    we.claim_id,
                    MIN(we.occurred_at) AS first_completed_at
                FROM workflow_events we
                WHERE we.event_type = 'claim_status_updated'
                  AND COALESCE(we.event_payload->>'status', '') = 'completed'
                GROUP BY we.claim_id
            )
            UPDATE claims c
            SET completed_at = fc.first_completed_at
            FROM first_completed fc
            WHERE c.id = fc.claim_id
              AND c.status = 'completed'
              AND c.completed_at IS NULL
            """,
            """
            UPDATE claims
            SET completed_at = updated_at
            WHERE status = 'completed'
              AND completed_at IS NULL
              AND updated_at IS NOT NULL
            """,
        ],
    ),
    # ── Add new migrations below this line ───────────────────────────────────
    # Example:
    # (
    #     4,
    #     "add some_column to some_table",
    #     [
    #         "ALTER TABLE some_table ADD COLUMN IF NOT EXISTS some_column TEXT",
    #     ],
    # ),
]


_MIGRATION_ADVISORY_LOCK_ID = 20260423  # arbitrary stable integer, one per app


def run_pending_migrations() -> None:
    """
    Run all pending DB schema migrations at application startup.
    Tracks applied versions in the schema_migrations table.
    Only runs DDL for versions higher than what is already recorded.

    Uses a PostgreSQL session-level advisory lock so that when multiple
    processes start simultaneously (e.g. two uvicorn services), only one
    runs the DDL. The second acquires the lock only after the first finishes
    and has already committed all versions, so it reads the updated version
    and skips cleanly.
    """
    # Use a dedicated DB connection for the whole migration flow.
    # Advisory locks are bound to the physical PostgreSQL session; if we use
    # an ORM Session with a pool, commits can switch connections and leave a
    # lock stranded on a different pooled connection.
    with engine.connect() as conn:
        conn.execute(
            text("SELECT pg_advisory_lock(:lock_id)"),
            {"lock_id": _MIGRATION_ADVISORY_LOCK_ID},
        )
        logger.info("schema_migrations: advisory lock acquired")
        try:
            # Bootstrap: create the migrations tracking table if it doesn't exist yet.
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version     INTEGER      PRIMARY KEY,
                        description VARCHAR(255) NOT NULL,
                        applied_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
                    )
                    """
                )
            )
            conn.commit()

            current_version: int = conn.execute(
                text("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
            ).scalar_one()

            pending = [m for m in _MIGRATIONS if m[0] > current_version]

            if not pending:
                logger.info("schema_migrations: up to date at v%d", current_version)
                return

            for version, description, sql_list in pending:
                logger.info("schema_migrations: applying v%d — %s", version, description)
                for sql in sql_list:
                    conn.execute(text(sql))
                conn.execute(
                    text(
                        "INSERT INTO schema_migrations (version, description) VALUES (:v, :d)"
                    ),
                    {"v": version, "d": description},
                )
                conn.commit()
                logger.info("schema_migrations: v%d applied", version)

            latest = pending[-1][0]
            logger.info(
                "schema_migrations: applied %d migration(s), now at v%d",
                len(pending),
                latest,
            )
        finally:
            unlocked = conn.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": _MIGRATION_ADVISORY_LOCK_ID},
            ).scalar_one()
            conn.commit()
            if unlocked:
                logger.info("schema_migrations: advisory lock released")
            else:
                logger.warning("schema_migrations: advisory lock was not held on unlock")

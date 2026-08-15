import argparse
import sys
from pathlib import Path

import psycopg
from psycopg import OperationalError, sql
from psycopg.rows import tuple_row

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import settings
from app.db.session import SessionLocal
from app.services.auth_service import ensure_bootstrap_super_admin


def create_database_if_missing(db_name: str) -> None:
    with psycopg.connect(settings.psycopg_admin_uri, autocommit=True) as conn:
        with conn.cursor(row_factory=tuple_row) as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (db_name,))
            exists = cur.fetchone()
            if exists:
                print(f"Database '{db_name}' already exists.")
                return

            query = sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db_name))
            cur.execute(query)
            print(f"Created database '{db_name}'.")


def apply_sql_file(path: Path) -> None:
    sql_text = path.read_text(encoding="utf-8")
    with psycopg.connect(settings.psycopg_database_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(sql_text)
    print(f"Applied: {path}")


def bootstrap_admin_if_configured() -> None:
    username = (settings.bootstrap_admin_username or "").strip()
    password = settings.bootstrap_admin_password or ""
    if not username or not password:
        return

    db = SessionLocal()
    try:
        ensure_bootstrap_super_admin(db, username=username, password=password)
        print(f"Bootstrap super admin ensured for username '{username}'.")
    finally:
        db.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and initialize QC-BKP modern database")
    parser.add_argument(
        "--skip-seed",
        action="store_true",
        help="Create schema only and skip seed data",
    )
    args = parser.parse_args()

    schema_path = REPO_ROOT / "db" / "schema.sql"
    seed_path = REPO_ROOT / "db" / "seed.sql"

    if not schema_path.exists():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")

    try:
        create_database_if_missing(settings.pg_database)
        apply_sql_file(schema_path)

        if not args.skip_seed:
            if not seed_path.exists():
                raise FileNotFoundError(f"Seed file not found: {seed_path}")
            apply_sql_file(seed_path)

        bootstrap_admin_if_configured()
    except OperationalError as exc:
        print("Could not connect to PostgreSQL. Start PostgreSQL and verify .env credentials.")
        print(f"Connection target: {settings.pg_host}:{settings.pg_port}/{settings.pg_database}")
        raise SystemExit(1) from exc

    print("Database bootstrap completed.")


if __name__ == "__main__":
    main()


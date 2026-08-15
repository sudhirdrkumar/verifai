#!/usr/bin/env bash
set -euo pipefail

: "${NEW_PG_PASSWORD:?NEW_PG_PASSWORD is required}"
: "${PG_ROLE_NAME:=admin}"

sudo -u postgres psql -d postgres -c "ALTER USER ${PG_ROLE_NAME} WITH PASSWORD '${NEW_PG_PASSWORD}';"

cd /opt/verifai/backend
if grep -q '^PG_USER=' .env; then
  sed -i "s/^PG_USER=.*/PG_USER=${PG_ROLE_NAME}/" .env
else
  printf '\nPG_USER=%s\n' "$PG_ROLE_NAME" >> .env
fi

if grep -q '^PG_PASSWORD=' .env; then
  sed -i "s/^PG_PASSWORD=.*/PG_PASSWORD=${NEW_PG_PASSWORD}/" .env
else
  printf '\nPG_PASSWORD=%s\n' "$NEW_PG_PASSWORD" >> .env
fi

sudo systemctl restart verifai-backend
sleep 8
systemctl is-active verifai-backend

./.venv/bin/python - <<'PY'
from app.db.session import engine
from sqlalchemy import text

try:
    with engine.connect() as conn:
        conn.execute(text('SELECT 1'))
    print('DB_CONNECT_OK')
except Exception as exc:
    print(f'DB_CONNECT_FAIL: {type(exc).__name__}: {exc}')
PY

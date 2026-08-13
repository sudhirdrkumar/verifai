#!/usr/bin/env bash
set -euo pipefail

if command -v service >/dev/null 2>&1; then
  sudo service postgresql start 2>/dev/null || service postgresql start 2>/dev/null || true
fi

for _ in $(seq 1 30); do
  if pg_isready -h 127.0.0.1 -p 5432 >/dev/null 2>&1; then
    exit 0
  fi
  sleep 1
done

echo "PostgreSQL did not become ready within 30 seconds." >&2
exit 1

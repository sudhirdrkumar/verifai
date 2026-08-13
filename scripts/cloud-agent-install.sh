#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

bash scripts/cloud-agent-start.sh

python3 -m pip install --user -r requirements.txt

if [[ ! -f .env ]]; then
  cp .env.example .env
  # Local dev defaults for Cloud Agent; override via environment secrets if needed.
  {
    echo ""
    echo "PG_PASSWORD=postgres"
    echo "BOOTSTRAP_ADMIN_USERNAME=admin"
    echo "BOOTSTRAP_ADMIN_PASSWORD=ChangeMeDev123!"
    echo "MEDICINE_RECTIFY_SCHEDULER_ENABLED=false"
    echo "FOLDER_SYNC_ENABLED=false"
  } >> .env
fi

python3 scripts/create_database.py

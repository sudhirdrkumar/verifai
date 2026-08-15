#!/bin/bash
set -euo pipefail

APP_DIR="/opt/verifai/backend"
STAGE_DIR="/tmp/qcpython_changes_stage"

mkdir -p "$APP_DIR/app/services"
mkdir -p "$APP_DIR/app/core"

rm -rf "$STAGE_DIR"
mkdir -p "$STAGE_DIR"
tar -xzf /tmp/qcpython_changes.tgz -C "$STAGE_DIR"

install -m 0644 "$STAGE_DIR/app/core/config.py" "$APP_DIR/app/core/config.py"
install -m 0644 "$STAGE_DIR/app/main.py" "$APP_DIR/app/main.py"
install -m 0644 "$STAGE_DIR/app/services/extractions_service.py" "$APP_DIR/app/services/extractions_service.py"
install -m 0644 "$STAGE_DIR/app/services/claim_structuring_service.py" "$APP_DIR/app/services/claim_structuring_service.py"
install -m 0644 "$STAGE_DIR/app/services/claim_reduction_service.py" "$APP_DIR/app/services/claim_reduction_service.py"
install -m 0644 "$STAGE_DIR/app/services/redis_service.py" "$APP_DIR/app/services/redis_service.py"
install -m 0644 "$STAGE_DIR/requirements.txt" "$APP_DIR/requirements.txt"

if [ -f "$STAGE_DIR/.env.example" ]; then
  install -m 0644 "$STAGE_DIR/.env.example" "$APP_DIR/.env.example"
fi

python3.11 -m venv "$APP_DIR/.venv" 2>/dev/null || true
. "$APP_DIR/.venv/bin/activate"
python -m pip install --upgrade pip >/dev/null
python -m pip install -r "$APP_DIR/requirements.txt" >/dev/null
python -m compileall "$APP_DIR/app" >/dev/null

ENV_FILE="$APP_DIR/.env"
GEMINI_VALUE="${GEMINI_API_KEY:-}"
REDIS_VALUE="${REDIS_URL:-redis://127.0.0.1:6379/0}"

python3 - <<PY
from pathlib import Path

env_path = Path("$ENV_FILE")
lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
updates = {
    "REDIS_URL": "$REDIS_VALUE",
    "GEMINI_API_KEY": "$GEMINI_VALUE",
    "GEMINI_FLASH_MODEL": "gemini-2.5-flash",
}
existing = {}
order = []
for line in lines:
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        key = line.split("=", 1)[0]
        existing[key] = line
        order.append(key)
for key, value in updates.items():
    existing[key] = f"{key}={value}"
    if key not in order:
        order.append(key)
out = []
seen = set()
for line in lines:
    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
        key = line.split("=", 1)[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(existing[key])
    else:
        out.append(line)
for key in order:
    if key not in seen:
        out.append(existing[key])
        seen.add(key)
env_path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
PY

sudo systemctl restart verifai-backend
for attempt in $(seq 1 18); do
  if curl -fsS --max-time 10 http://127.0.0.1:8000/health; then
    echo
    sudo systemctl is-active verifai-backend
    exit 0
  fi
  sleep 5
done

sudo journalctl -u verifai-backend -n 80 --no-pager
exit 1

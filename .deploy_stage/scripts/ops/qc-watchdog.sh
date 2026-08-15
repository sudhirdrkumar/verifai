#!/usr/bin/env bash
set -euo pipefail

FAIL_COUNT_FILE="/var/lib/qc-watchdog/fail-count"
FAIL_THRESHOLD=3
HEALTH_URLS=(
  "http://127.0.0.1:8001/api/v1/health"
  "http://127.0.0.1:8002/api/v1/health"
)

mkdir -p "$(dirname "$FAIL_COUNT_FILE")"

if [[ ! -f "$FAIL_COUNT_FILE" ]]; then
  echo "0" > "$FAIL_COUNT_FILE"
fi

read -r fail_count < "$FAIL_COUNT_FILE"
fail_count="${fail_count:-0}"

healthy=1
for url in "${HEALTH_URLS[@]}"; do
  if ! curl -fsS --max-time 8 "$url" > /dev/null; then
    healthy=0
    logger -t qc-watchdog "Health check failed for $url"
  fi
done

if [[ "$healthy" -eq 1 ]]; then
  if [[ "$fail_count" -ne 0 ]]; then
    logger -t qc-watchdog "Health recovered; resetting failure counter from $fail_count to 0"
  fi
  echo "0" > "$FAIL_COUNT_FILE"
  exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count" > "$FAIL_COUNT_FILE"
logger -t qc-watchdog "Consecutive health-check failures: $fail_count/$FAIL_THRESHOLD"

if [[ "$fail_count" -ge "$FAIL_THRESHOLD" ]]; then
  logger -t qc-watchdog "Threshold reached; restarting qc-python, qc-python-2, and nginx"
  systemctl restart qc-python qc-python-2 nginx
  echo "0" > "$FAIL_COUNT_FILE"
fi


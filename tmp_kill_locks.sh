#!/bin/bash
export PGPASSWORD="Dhoom*2690"
echo "=== Killing blocked connections ==="
psql -h 127.0.0.1 -U postgres -d qc_bkp_modern -c "SELECT pg_terminate_backend(pid), pid, left(query,60) AS q FROM pg_stat_activity WHERE wait_event = 'relation' AND state <> 'idle' AND pid <> pg_backend_pid();"
echo "=== Done. Remaining active ==="
psql -h 127.0.0.1 -U postgres -d qc_bkp_modern -c "SELECT count(*) FROM pg_stat_activity WHERE datname='qc_bkp_modern' AND state <> 'idle';"

# Complete Implementation Guide - Transaction Management Fix

**Status:** Ready for Deployment  
**Effort:** 2-4 hours for Phase 1  
**Impact:** HIGH - Will eliminate idle in transaction issues

---

## What We've Done

### ✅ Completed
1. **Identified root cause:** Transactions never committed/closed
2. **Added monitoring middleware:** Detects slow requests and stuck transactions
3. **Created utility functions:** Easy-to-use transaction management helpers
4. **Audited all endpoints:** 30+ endpoints identified needing fixes
5. **Documented all fixes:** Templates and examples provided

### 🔄 Ready to Deploy
- Monitoring middleware integrated into FastAPI app
- Database utilities ready to use
- Endpoint audit report with specific line numbers

### ⏳ Next Steps
- Deploy monitoring to EC2 (immediate visibility)
- Fix critical endpoints (Phase 1)
- Fix remaining endpoints (Phase 2)

---

## Deployment Steps

### Step 1: Deploy Monitoring & Utilities to EC2

```bash
# Copy new files to EC2
scp -i key.pem app/middleware/request_monitoring.py ec2-user@15.207.135.22:~/qc-python/app/middleware/
scp -i key.pem app/utils/db_utils.py ec2-user@15.207.135.22:~/qc-python/app/utils/
scp -i key.pem app/main.py ec2-user@15.207.135.22:~/qc-python/app/

# Restart Uvicorn
ssh ec2-user@15.207.135.22 "pkill -9 uvicorn; sleep 2; cd ~/qc-python && \
  nohup .venv/bin/uvicorn app.main:app --port 8001 > /tmp/uvicorn_8001.log 2>&1 & \
  nohup .venv/bin/uvicorn app.main:app --port 8002 > /tmp/uvicorn_8002.log 2>&1 &"

# Verify
sleep 3
curl http://15.207.135.22:8001/api/v1/health
```

### Step 2: Monitor the Monitoring

Watch for alerts in logs:
```bash
ssh ec2-user@15.207.135.22 "tail -f /tmp/uvicorn_8001.log | grep -E 'SLOW|CRITICAL|POOL WARNING'"
```

Expected output will show:
- Request durations
- Pool usage trends
- Any slow/stuck transactions

### Step 3: Fix Critical Endpoints (Phase 1)

Priority order:
1. `app/api/v1/endpoints/auth.py` - Authentication (BLOCKS users)
2. `app/api/v1/endpoints/user_tools.py:get_claim_document_status` (MOST USED)
3. `app/api/v1/endpoints/user_tools.py:get_dashboard_overview`
4. `app/api/v1/endpoints/user_tools.py:get_completed_reports`

For each endpoint:

```python
# BEFORE
@router.get("/endpoint")
def get_endpoint(db: Session = Depends(get_db)):
    result = db.query(Model).first()
    return result

# AFTER
from app.utils.db_utils import get_db_context

@router.get("/endpoint")
def get_endpoint():
    with get_db_context() as db:
        result = db.query(Model).first()
        return result
```

### Step 4: Test Phase 1 Fixes

```bash
# Check idle transactions
ssh ec2-user@15.207.135.22 'psql -h 127.0.0.1 -U postgres -d qc_bkp_modern -c \
  "SELECT COUNT(*) FROM pg_stat_activity WHERE state = '"'"'idle in transaction'"'"';"'

# Should show: 0

# Test health
curl http://15.207.135.22:8001/api/v1/health
# Should show: status = "ok", pool.warning = false

# Run concurrent requests
for i in {1..10}; do curl http://15.207.135.22:8001/api/v1/auth/me & done; wait

# Check pool again - should still be healthy
curl http://15.207.135.22:8001/api/v1/health/pool
```

### Step 5: Deploy Phase 1 to EC2

After local testing:

```bash
# Commit changes
git add app/api/v1/endpoints/auth.py app/api/v1/endpoints/user_tools.py
git commit -m "Fix critical endpoints - explicit transaction management

Phase 1 fixes:
- auth.py: Authentication endpoint
- user_tools.py: claim-document-status, dashboard-overview, completed-reports

All endpoints now use get_db_context() for automatic commit/rollback.

Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"

# Deploy
scp -i key.pem app/api/v1/endpoints/auth.py ec2-user@15.207.135.22:~/qc-python/app/api/v1/endpoints/
scp -i key.pem app/api/v1/endpoints/user_tools.py ec2-user@15.207.135.22:~/qc-python/app/api/v1/endpoints/

# Restart
ssh ec2-user@15.207.135.22 "pkill -9 uvicorn; sleep 2; cd ~/qc-python && \
  nohup .venv/bin/uvicorn app.main:app --port 8001 > /tmp/uvicorn_8001.log 2>&1 & \
  nohup .venv/bin/uvicorn app.main:app --port 8002 > /tmp/uvicorn_8002.log 2>&1 &"
```

### Step 6: Monitor 24 Hours

Set up monitoring script:

```bash
#!/bin/bash
while true; do
  echo "=== $(date) ==="
  
  # Check idle connections
  IDLE=$(ssh ec2-user@15.207.135.22 'psql -h 127.0.0.1 -U postgres -d qc_bkp_modern -t -c \
    "SELECT COUNT(*) FROM pg_stat_activity WHERE state = '"'"'idle in transaction'"'"';"')
  echo "Idle in transaction: $IDLE"
  
  # Check health
  curl -s http://15.207.135.22:8001/api/v1/health | jq '.status, .pool.warning'
  
  # Check load
  ssh ec2-user@15.207.135.22 uptime | awk -F'load average:' '{print "Load: " $2}'
  
  sleep 60
done
```

Run it:
```bash
chmod +x monitor.sh
./monitor.sh
```

Watch for:
- Idle in transaction staying at 0
- Status remaining "ok"
- Pool.warning staying false
- Load decreasing over time

### Step 7: Fix Remaining Endpoints (Phase 2)

After Phase 1 is stable (24 hours), fix remaining endpoints following same pattern:

**admin_tools.py endpoints:** 20 endpoints need fixes
**user_tools.py endpoints:** Remaining functions
**claims.py endpoints:** 2+ endpoints

Use this batch fix command:
```bash
# Find all Depends(get_db) in admin_tools
grep -n "Depends(get_db)" app/api/v1/endpoints/admin_tools.py

# For each line, apply the context manager pattern
```

---

## Key Files to Reference

| File | Purpose |
|------|---------|
| `ENDPOINT_AUDIT_REPORT.md` | Complete list of endpoints needing fixes |
| `app/utils/db_utils.py` | Reusable utilities - USE THESE! |
| `app/middleware/request_monitoring.py` | Monitoring logic |
| `SERVER_DIAGNOSIS_REPORT.md` | Root cause analysis |
| `EC2_OPTIMIZATION_SUMMARY.md` | All optimizations summary |

---

## Patterns to Follow

### Pattern 1: Simple Read Endpoint

```python
from app.utils.db_utils import get_db_context

@router.get("/api/users")
def get_users():
    with get_db_context() as db:
        users = db.query(User).all()
        return users
```

### Pattern 2: Write Endpoint

```python
from app.utils.db_utils import get_db_context

@router.post("/api/users")
def create_user(user_data: UserCreate):
    with get_db_context() as db:
        user = User(**user_data.dict())
        db.add(user)
        db.flush()  # Get the ID before commit
        return user
```

### Pattern 3: Complex Query

```python
from app.utils.db_utils import get_db_context

@router.get("/api/report")
def get_report(filters: ReportFilters):
    with get_db_context() as db:
        query = db.query(Claim).join(...)
        if filters.status:
            query = query.filter(Claim.status == filters.status)
        results = query.all()
        return results
```

### Pattern 4: Can't use context manager (rare)

```python
from app.utils.db_utils import ensure_commit

@router.post("/endpoint")
def endpoint(db: Session = Depends(get_db)):
    # ... do stuff ...
    ensure_commit(db)  # Explicit commit
    return result
```

---

## What to Expect

### After Phase 1 (4-8 hours)
- ✅ No more "idle in transaction" errors
- ✅ Health endpoint shows status = "ok"
- ✅ Connection pool healthy
- ✅ System load dropping
- ✅ Authentication working reliably
- ✅ Most-used endpoints fast

### After Phase 2 (1-2 days)
- ✅ All endpoints using proper transaction management
- ✅ Zero idle connection issues
- ✅ System completely stable
- ✅ Monitoring showing all green
- ✅ Ready for Temporal.io if needed

---

## Rollback Plan (if needed)

If something breaks:

```bash
# Revert last commit
git revert HEAD

# OR restore old version
git checkout HEAD~1 -- app/api/v1/endpoints/auth.py app/api/v1/endpoints/user_tools.py

# Restart
pkill -9 uvicorn && sleep 2 && nohup .venv/bin/uvicorn app.main:app --port 8001 &
```

---

## Success Metrics

### Baseline (Current)
- Idle in transaction: 8
- Pool checked out: 6/5 (OVERFLOW!)
- Status: DEGRADED
- Load: 6.7+
- CPU idle: 0%

### Target (After fixes)
- Idle in transaction: 0
- Pool checked out: 1-2/5
- Status: OK
- Load: < 2.0
- CPU idle: > 50%

---

## Questions?

Reference these docs:
1. `ENDPOINT_AUDIT_REPORT.md` - Which endpoints need fixing
2. `app/utils/db_utils.py` - How to use utilities
3. `app/middleware/request_monitoring.py` - What monitoring is tracking
4. `SERVER_DIAGNOSIS_REPORT.md` - Why this is happening

---

## Next Action

**Ready to proceed with:**
1. Deploy monitoring to EC2 now?
2. Fix Phase 1 endpoints?
3. Both?

Let me know and I'll deploy and implement the fixes!

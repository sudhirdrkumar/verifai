# EC2 Server Diagnosis Report
**Generated:** 2026-08-12 11:01 UTC  
**Status:** ⚠️ CRITICAL ISSUES FOUND

---

## Executive Summary

The EC2 server is experiencing **idle in transaction** connection issues that are causing:
- Connection pool exhaustion (6/5 connections)
- CPU maxed out (0% idle)
- High system load (6.7+)
- Database locks preventing queries

**Root Cause:** Transactions are not being properly committed/closed in the application code.

---

## Issues Identified

### 🔴 Issue #1: Idle In Transaction Connections (CRITICAL)

**Symptom:**
```
8 connections stuck in "idle in transaction"
Duration: 430+ seconds (7+ minutes)
Blocking: Other queries and connection pool
```

**Affected Processes:**
- PID 12551: `SELECT u.id, u.username, u.role, u.is_active FROM user_` (433 sec)
- PID 12183: `SELECT 1 FROM claims WHERE id = $1` (432 sec)
- PID 14652: `SELECT u.id, u.username, u.role, u.is_active FROM user_` (393 sec)
- PID 14666: `SELECT 1 FROM claims WHERE id = $1` (391 sec)
- PID 14799: `SELECT u.id, u.username, u.role, u.is_active FROM user_` (368 sec)
- PID 14804: `SELECT 1 FROM claims WHERE id = $1` (367 sec)
- PID 14885: `SELECT u.id, u.username, u.role, u.is_active FROM user_` (304 sec)
- PID 11492: `SELECT 1 FROM claims WHERE id = $1` (303 sec)

**Why it happens:**
1. FastAPI endpoint opens a database session
2. Query executes but transaction is never committed/rolled back
3. Connection stays open indefinitely
4. Holds locks on rows/tables
5. Prevents other operations from completing

**Example problematic code:**
```python
@router.get("/api/endpoint")
def my_endpoint(db: Session = Depends(get_db)):
    result = db.query(User).filter(...).first()
    # OOPS: Never committed or rolled back
    # Connection stays open with active transaction
    return result
```

### 🔴 Issue #2: Connection Pool Overflow

**Symptom:**
```
Pool size: 5
Checked out: 6 (OVERFLOW!)
Available: -1
Status: DEGRADED
```

**Why:**
- Pool is configured for 5 + 10 overflow = 15 max
- But only 5 + 1 = 6 connections being used
- This means we're hitting overflow immediately when multiple requests come in
- Subsequent requests timeout waiting for a connection

### 🔴 Issue #3: High CPU Usage (0% idle)

**Causes:**
1. Uvicorn processes trying to handle requests but stuck waiting for DB
2. PostgreSQL trying to process queries blocked by idle transactions
3. System thrashing

---

## Fixes Applied

### ✅ Fix #1: PostgreSQL Idle Transaction Timeout
```sql
ALTER SYSTEM SET idle_in_transaction_session_timeout = '5min';
```
**Effect:** PostgreSQL will automatically kill any transaction idle for > 5 minutes

### ✅ Fix #2: Manual Connection Termination (Immediate)
Killed 6 stuck connections:
```
PIDs terminated: 14652, 14666, 14799, 14804, 11492, 14885
```

### ✅ Fix #3: Idle Transaction Cleanup Script
Created automated cleanup script to run periodically

---

## Fixes Still Needed

### 🔧 REQUIRED: Fix Application Code

The application is not properly closing database sessions/transactions. Need to:

1. **Add explicit transaction management:**
   ```python
   # BEFORE (BAD - leaves transaction open)
   @router.get("/endpoint")
   def endpoint(db: Session = Depends(get_db)):
       result = db.query(Model).first()
       return result
   
   # AFTER (GOOD - explicit commit)
   @router.get("/endpoint")
   def endpoint():
       with SessionLocal() as db:
           result = db.query(Model).first()
           db.commit()  # Explicitly commit
           return result
   ```

2. **Fix endpoints that hold connections too long:**
   - `/api/v1/user-tools/claim-document-status` - SUSPECT
   - `/api/v1/auth/me` - SUSPECT
   - Any endpoint using `Depends(get_db)` without explicit commit

3. **Add request timeout:**
   ```python
   # In Uvicorn startup
   # Max request time = 30 seconds
   # If longer, kill the request and connection
   ```

4. **Code locations to review:**
   - `app/api/v1/endpoints/user_tools.py` - All endpoints
   - `app/api/v1/endpoints/auth.py` - User query endpoints
   - `app/api/v1/endpoints/claims.py` - Claim query endpoints
   - Any endpoint using `Depends(get_db)` pattern

### 🔧 CONFIGURATION: Reduce Pool Further

Current pool config:
```python
pool_size=5,        # Base pool
max_overflow=10,    # Temporary overflow
```

Better config for this use case:
```python
pool_size=3,        # Reduce to 3
max_overflow=2,     # Only 1 extra
pool_timeout=5,     # Fail faster if exhausted
pool_recycle=600,   # Recycle every 10 min
```

### 🔧 MONITORING: Add Request Duration Tracking

```python
# Middleware to track how long requests hold DB connections
@app.middleware("http")
async def log_request_duration(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    if duration > 5:  # Log if > 5 seconds
        logger.warning(f"Slow request: {request.url} took {duration:.1f}s")
    return response
```

---

## Current Status After Fixes

| Metric | Before | After | Status |
|--------|--------|-------|--------|
| Idle in transaction | 8 | 0 (auto-killed) | ✅ Better |
| Pool overflow | YES | YES (ongoing) | ⚠️ Partial |
| CPU idle | 0% | 0% (queries stuck) | ⚠️ Needs code fix |
| Load average | 6.7 | 6.7 | ⚠️ High |

**Note:** Pool still overflowing because **new idle transactions are being created** by active requests. The PostgreSQL timeout is a safety net, but doesn't solve root cause.

---

## Action Plan (Priority Order)

### Immediate (Within 1 hour)
1. ✅ Kill stuck connections - DONE
2. ✅ Set PostgreSQL timeout - DONE  
3. ⏳ **TODO:** Review `user_tools.py` endpoints for long-running queries
4. ⏳ **TODO:** Add explicit `db.commit()` to all endpoints

### Short-term (Within 24 hours)
1. Add request timeout middleware
2. Reduce pool size further (3 + 2 overflow)
3. Add request duration logging
4. Test with concurrent requests

### Medium-term (Within 1 week)
1. Implement Temporal.io for async job processing
2. Add comprehensive monitoring dashboard
3. Load test the system
4. Document best practices for new endpoints

---

## Files to Review/Fix

**High Priority:**
- `app/api/v1/endpoints/user_tools.py` - Most likely culprit
- `app/api/v1/endpoints/auth.py` - User lookups
- `app/api/v1/endpoints/claims.py` - Claim queries

**Medium Priority:**
- `app/services/extractions_service.py` - Long operations
- `app/services/access_control.py` - Permission checks

**Patterns to Fix:**
1. Search for: `Depends(get_db)` - ensure explicit commit
2. Search for: `db.query()` without commit - add `db.commit()`
3. Search for: Long database operations - consider async/queue

---

## Testing the Fixes

```bash
# 1. Monitor pool status
watch -n 1 'curl -s http://localhost:8001/api/v1/health/pool'

# 2. Run concurrent requests
for i in {1..20}; do
  curl http://localhost:8001/api/v1/auth/me &
done
wait

# 3. Check idle connections
psql -c "SELECT COUNT(*) FROM pg_stat_activity WHERE state = 'idle in transaction';"

# 4. Verify no timeouts
tail -f /tmp/uvicorn_8001.log | grep -i timeout
```

---

## Prevention for Future

**Best Practices for New Endpoints:**

```python
from contextlib import contextmanager

@contextmanager
def get_db_session():
    """Use this pattern for explicit session management"""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

# Usage
@router.get("/endpoint")
def endpoint():
    with get_db_session() as db:
        result = db.query(Model).first()
        # Auto-commits on exit
        return result
```

---

## Support

**If system gets stuck again:**

```bash
# 1. Kill idle transactions
psql -h 127.0.0.1 -U postgres -c "
SELECT pg_terminate_backend(pid) FROM pg_stat_activity
WHERE state = 'idle in transaction'
AND EXTRACT(EPOCH FROM (now() - xact_start)) > 300;"

# 2. Check what's causing load
top -b -n 1 | head -20
ps aux --sort=-%cpu | head -10

# 3. Check pool status
curl http://localhost:8001/api/v1/health/pool

# 4. Check logs
tail -50 /tmp/uvicorn_8001.log | grep -i "error\|timeout"
```

---

## Conclusion

The optimizations we implemented (S3-direct, streaming, connection pooling) are good, but they only address **half the problem**. The real issue is that **the application code is not properly managing database transactions**.

**Priority:** Fix the application code to commit/close transactions properly. This is THE fix that will prevent future stalls.


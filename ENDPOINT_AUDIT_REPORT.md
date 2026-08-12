# Endpoint Audit Report - Transaction Management Issues

**Report Date:** 2026-08-12  
**Status:** CRITICAL - 30+ endpoints need fixes

---

## Summary

- **Total endpoints using `Depends(get_db)`:** 30+
- **Endpoints missing explicit `db.commit()`:** 25+
- **Highest risk files:**
  1. `admin_tools.py` - 20+ endpoints
  2. `user_tools.py` - 8+ endpoints  
  3. `auth.py` - 3+ endpoints
  4. `claims.py` - 2+ endpoints

---

## Critical Endpoints (MUST FIX FIRST)

### 1. `app/api/v1/endpoints/auth.py` - Line 22

**Current Code:**
```python
def authenticate(
    email_or_username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
) -> TokenResponse:
    user = db.query(User).filter(...).first()
    # NO COMMIT! Connection stays open
    return TokenResponse(...)
```

**Fixed Code:**
```python
def authenticate(
    email_or_username: str = Form(...),
    password: str = Form(...),
) -> TokenResponse:
    with get_db_context() as db:
        user = db.query(User).filter(...).first()
        # Auto-commits on exit
        return TokenResponse(...)
```

**Impact:** HIGH - User authentication failing due to stuck connection

---

### 2. `app/api/v1/endpoints/user_tools.py` - claim-document-status (LIKELY CULPRIT)

**Issue:** Long-running query that doesn't commit

**Fix Pattern:**
```python
# BEFORE
@router.get("/user-tools/claim-document-status")
def get_claim_status(
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(...)),
):
    # Multiple queries without commit

# AFTER
@router.get("/user-tools/claim-document-status")
def get_claim_status(
    current_user: AuthenticatedUser = Depends(require_roles(...)),
):
    with get_db_context() as db:
        # All queries here, auto-commit on exit
```

**Impact:** CRITICAL - This endpoint is called frequently and holds connections

---

## All Endpoints Needing Fixes

### admin_tools.py (20 endpoints)

| Line | Function | Issue |
|------|----------|-------|
| 490 | `upsert_claim_rule` | No commit after INSERT |
| 554 | `upsert_claim_rule` | No commit after INSERT |
| 575 | `toggle_claim_rule_active` | No commit after UPDATE |
| 630 | `delete_claim_rule` | No commit after DELETE |
| 676 | `get_claim_rules` | No commit on SELECT (but holds connection) |
| 732 | `get_diagnosis_criteria` | No commit |
| 749 | `upsert_diagnosis_criteria` | No commit after INSERT |
| 764 | `delete_diagnosis_criteria` | No commit after DELETE |
| 818 | `get_openai_suggestions` | No commit |
| 866 | `process_rule_suggestion` | No commit after INSERT |
| 922 | `get_suggestion_feedback` | No commit |
| 939 | `upsert_feedback_label` | No commit after INSERT |
| 954 | `get_feedback_labels` | No commit |
| 1116 | `validate_medicine_name` | No commit |
| 1168 | `get_medicine_components` | No commit |
| 1213 | `upsert_medicine_component` | No commit after INSERT |
| 1255 | `delete_medicine_component` | No commit after DELETE |
| 1303 | `get_all_components` | No commit |
| 1327 | `check_medicine_component` | No commit |

**Fix Pattern for All:** Replace `Depends(get_db)` with context manager

---

### user_tools.py (8 endpoints)

**HIGHEST PRIORITY** - These are called by frontend frequently

| Function | Issue |
|----------|-------|
| `claim-document-status` | Long query, no commit |
| `completed-reports` | Multiple queries, no commit |
| `dashboard-overview` | Complex query, no commit |
| `allotment-date-wise` | Multiple joins, no commit |
| `claim-search` | Search query, no commit |
| `user-assignments` | Group query, no commit |

---

### auth.py (3 endpoints)

| Line | Function | Issue |
|------|----------|-------|
| 22 | `authenticate` | User lookup, no commit |
| (other auth endpoints) | User lookups | No commit |

---

### claims.py (2+ endpoints)

| Function | Issue |
|----------|-------|
| `get_claims` | List query, no commit |
| `create_claim` | Insert, no commit |

---

## Fix Template

### For READ-ONLY endpoints (SELECT only):

```python
# BEFORE
@router.get("/api/endpoint")
def get_data(db: Session = Depends(get_db), ...):
    result = db.query(Model).first()
    return result

# AFTER
@router.get("/api/endpoint")
def get_data(...):
    with get_db_context() as db:
        result = db.query(Model).first()
        return result
```

### For WRITE endpoints (INSERT/UPDATE/DELETE):

```python
# BEFORE
@router.post("/api/endpoint")
def create_data(payload: MyModel, db: Session = Depends(get_db)):
    db.add(payload)
    db.execute(...)
    # NO COMMIT!
    return result

# AFTER
@router.post("/api/endpoint")
def create_data(payload: MyModel):
    with get_db_context() as db:
        db.add(payload)
        db.execute(...)
        # Auto-commits on successful exit
        return result
```

### For endpoints that can't use context manager:

```python
from app.utils.db_utils import ensure_commit, get_db_context

@router.post("/endpoint")
def my_endpoint(db: Session = Depends(get_db)):
    # ... do stuff ...
    ensure_commit(db)  # Explicit commit
    return result
```

---

## Implementation Order

### Phase 1 (TODAY - 1-2 hours) - FIX CRITICAL
1. `auth.py` - Authentication endpoint (BLOCKS all users)
2. `user_tools.py` - claim-document-status (MOST USED endpoint)
3. `user_tools.py` - dashboard-overview
4. `user_tools.py` - completed-reports

**Result:** System should stabilize immediately

### Phase 2 (TODAY - 2-3 hours) - FIX HIGH PRIORITY
1. All write endpoints in `admin_tools.py` (INSERT/UPDATE/DELETE)
2. All read endpoints in `user_tools.py`
3. All endpoints in `claims.py`

**Result:** Zero idle in transaction issues

### Phase 3 (TOMORROW - 1-2 hours) - OPTIONAL CLEANUP
1. Add `@safe_db_operation` decorator to remaining endpoints
2. Add integration tests to ensure commits happen
3. Add automated checks to CI/CD

---

## Automated Helper Available

**Location:** `app/utils/db_utils.py`

**Available utilities:**
- `get_db_context()` - Context manager for automatic commit/rollback
- `safe_db_operation()` - Decorator (optional, for decorator-style endpoints)
- `ensure_commit(db)` - Explicit commit when needed
- `ensure_rollback(db)` - Explicit rollback on error

**Example Usage:**
```python
from app.utils.db_utils import get_db_context

@router.get("/endpoint")
def my_endpoint():
    with get_db_context() as db:
        result = db.query(Model).first()
        return result
```

---

## Testing the Fixes

After fixing each endpoint:

```bash
# 1. Check for idle transactions
watch -n 1 'psql -c "SELECT COUNT(*) FROM pg_stat_activity WHERE state = '"'"'idle in transaction'"'"';"'

# 2. Test the endpoint
curl http://localhost:8001/api/v1/endpoint

# 3. Verify connection released
# Count should return to 0 or < 1

# 4. Monitor pool status
curl http://localhost:8001/api/v1/health/pool

# Should show: checked_out = 1 (only the monitoring request)
```

---

## Success Criteria

- ✅ No connections in "idle in transaction" state
- ✅ Connection pool has free connections available  
- ✅ Load average drops below 2.0
- ✅ No SLOW/CRITICAL warnings in logs
- ✅ Health endpoint shows status: "ok"
- ✅ Concurrent requests complete without timeout

---

## Deployment Checklist

- [ ] Create new file: `app/utils/db_utils.py` - DONE
- [ ] Add monitoring middleware to `app/main.py` - DONE
- [ ] Fix `auth.py` endpoints
- [ ] Fix `user_tools.py` endpoints
- [ ] Fix `admin_tools.py` endpoints  
- [ ] Fix `claims.py` endpoints
- [ ] Test with concurrent requests
- [ ] Deploy to EC2
- [ ] Monitor for 24 hours
- [ ] Remove temporary kill script (pool cleanup can retire)

---

## Prevention for Future

**Best Practice for New Endpoints:**

```python
from app.utils.db_utils import get_db_context

@router.get("/new/endpoint")
def new_endpoint():
    # ALWAYS use context manager
    with get_db_context() as db:
        result = db.query(Model).first()
        return result

    # Never use Depends(get_db) pattern
    # Never forget to commit
```

---

## Support

**If questions during implementation:**
1. Check the pattern templates above
2. Use `get_db_context()` - safest option
3. Run test suite to verify
4. Check logs for "TRANSACTION GUARD" warnings


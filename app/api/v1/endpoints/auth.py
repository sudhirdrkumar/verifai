import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.deps.auth import get_bearer_token, get_current_user, require_roles
from app.core.config import settings
from app.db.session import get_db
from app.schemas.auth import (
    AuthUserResponse,
    CreateUserRequest,
    GstVerificationResponse,
    IfscVerificationResponse,
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    UpdatePasswordRequest,
    UserBankDetailsItem,
    UserBankDetailsListResponse,
    UserBankDetailsUpsertRequest,
    UserListResponse,
    UserRole,
)
from app.schemas.qc_tools import ResetUserPasswordRequest
from app.services.auth_service import (
    AuthenticationError,
    AuthenticatedUser,
    UserAlreadyExistsError,
    UserNotFoundError,
    admin_reset_user_password,
    authenticate_and_create_session,
    change_own_password,
    create_user_account,
    list_users,
    revoke_session,
)
from app.utils.db_utils import get_db_context
router = APIRouter(prefix="/auth", tags=["auth"])
_GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][A-Z0-9]Z[A-Z0-9]$")
_PHARMACY_HINTS = (
    "pharma",
    "pharmacy",
    "chemist",
    "medicos",
    "medical",
    "drug",
    "medicine",
)


def _ensure_user_bank_details_table(db: Session) -> None:
    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS user_bank_details (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                account_holder_name VARCHAR(255) NOT NULL DEFAULT '',
                bank_name VARCHAR(255) NOT NULL DEFAULT '',
                branch_name VARCHAR(255) NOT NULL DEFAULT '',
                account_number VARCHAR(64) NOT NULL DEFAULT '',
                payment_rate VARCHAR(64) NOT NULL DEFAULT '',
                ifsc_code VARCHAR(32) NOT NULL DEFAULT '',
                upi_id VARCHAR(255) NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by VARCHAR(100) NOT NULL DEFAULT '',
                updated_by VARCHAR(100) NOT NULL DEFAULT '',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    )
    db.execute(text("ALTER TABLE user_bank_details ADD COLUMN IF NOT EXISTS payment_rate VARCHAR(64) NOT NULL DEFAULT ''"))
    db.execute(text("CREATE INDEX IF NOT EXISTS idx_user_bank_details_user_id ON user_bank_details(user_id)"))


def _bank_text(value: str | None, max_len: int) -> str:
    return str(value or "").strip()[:max_len]


def _normalize_ifsc_code(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _normalize_gstin(value: str | None) -> str:
    return re.sub(r"\s+", "", str(value or "").strip().upper())


def _safe_text(value: object) -> str:
    return str(value or "").strip()


def _compose_address(addr: object) -> tuple[str, str, str]:
    if not isinstance(addr, dict):
        return "", "", ""
    ordered_keys = ["bno", "flno", "bnm", "st", "loc", "city", "dst", "stcd", "pncd"]
    parts: list[str] = []
    for key in ordered_keys:
        item = _safe_text(addr.get(key))
        if item:
            parts.append(item)
    deduped: list[str] = []
    seen: set[str] = set()
    for item in parts:
        low = item.lower()
        if low in seen:
            continue
        seen.add(low)
        deduped.append(item)
    return ", ".join(deduped), _safe_text(addr.get("pncd")), _safe_text(addr.get("stcd"))


def _detect_pharmacy(legal_name: str, trade_name: str, nature_of_business: list[str]) -> tuple[bool, str]:
    blobs = [legal_name, trade_name, " ".join(nature_of_business)]
    full_text = " ".join([b for b in blobs if b]).lower()
    for hint in _PHARMACY_HINTS:
        if hint in full_text:
            return True, f"matched '{hint}' in GST legal/trade/business text"
    return False, ""


def _is_active_gst_status(status_text: str) -> bool | None:
    normalized = re.sub(r"[^a-z]", "", _safe_text(status_text).lower())
    if not normalized:
        return None
    if normalized in {"active", "act"}:
        return True
    if normalized in {"inactive", "inact", "cancelled", "canceled", "suspended", "cancel", "provisional", "invalid"}:
        return False
    if "inactive" in normalized or "cancel" in normalized or "suspend" in normalized:
        return False
    if "active" in normalized:
        return True
    return None


def _coerce_optional_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text_value = str(value).strip().lower()
    if text_value in {"1", "true", "yes", "y"}:
        return True
    if text_value in {"0", "false", "no", "n"}:
        return False
    return None


def _verify_ifsc_with_razorpay(ifsc_code: str) -> IfscVerificationResponse:
    normalized_ifsc = _normalize_ifsc_code(ifsc_code)
    if not normalized_ifsc:
        raise HTTPException(status_code=400, detail="IFSC code is required.")
    if not re.fullmatch(r"^[A-Z]{4}0[A-Z0-9]{6}$", normalized_ifsc):
        raise HTTPException(status_code=400, detail="Invalid IFSC format.")

    if not settings.razorpay_ifsc_verify_enabled:
        raise HTTPException(status_code=503, detail="IFSC verification is disabled.")

    base_url = str(settings.razorpay_ifsc_api_base_url or "https://ifsc.razorpay.com").strip().rstrip("/")
    target_url = f"{base_url}/{normalized_ifsc}"
    timeout_sec = float(settings.razorpay_ifsc_timeout_seconds or 8.0)

    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            response = client.get(target_url, headers={"Accept": "application/json"})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Unable to reach IFSC verification service.") from exc

    if response.status_code == 404:
        raise HTTPException(status_code=404, detail="IFSC not found.")
    if response.status_code >= 400:
        raise HTTPException(status_code=502, detail="IFSC verification service error.")

    try:
        payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Invalid response from IFSC verification service.") from exc

    bank_name = str(payload.get("BANK") or "").strip()
    branch_name = str(payload.get("BRANCH") or "").strip()
    return IfscVerificationResponse(
        ifsc_code=normalized_ifsc,
        valid=bool(bank_name or branch_name),
        bank_name=bank_name,
        branch_name=branch_name,
        address=str(payload.get("ADDRESS") or "").strip(),
        city=str(payload.get("CITY") or "").strip(),
        district=str(payload.get("DISTRICT") or "").strip(),
        state=str(payload.get("STATE") or "").strip(),
        contact=str(payload.get("CONTACT") or "").strip(),
        micr=str(payload.get("MICR") or "").strip(),
        bank_code=str(payload.get("BANKCODE") or "").strip(),
        upi=_coerce_optional_bool(payload.get("UPI")),
        neft=_coerce_optional_bool(payload.get("NEFT")),
        rtgs=_coerce_optional_bool(payload.get("RTGS")),
        imps=_coerce_optional_bool(payload.get("IMPS")),
        source="razorpay_ifsc",
        raw=payload if isinstance(payload, dict) else {},
    )


def _verify_gstin_with_cleartax(gstin: str, captcha_token: str | None = None) -> GstVerificationResponse:
    normalized_gstin = _normalize_gstin(gstin)
    if not normalized_gstin:
        raise HTTPException(status_code=400, detail="GSTIN is required.")
    if not _GSTIN_RE.fullmatch(normalized_gstin):
        raise HTTPException(status_code=400, detail="Invalid GSTIN format.")
    if not settings.cleartax_gst_verify_enabled:
        raise HTTPException(status_code=503, detail="GST verification is disabled.")

    base_url = str(settings.cleartax_gst_base_url or "https://cleartax.in").strip().rstrip("/")
    timeout_sec = float(settings.cleartax_gst_timeout_seconds or 10.0)
    target_url = f"{base_url}/f/compliance-report/{normalized_gstin}/"
    params: dict[str, str] = {}
    if _safe_text(captcha_token):
        params["captcha_token"] = _safe_text(captcha_token)

    try:
        with httpx.Client(timeout=timeout_sec, follow_redirects=True) as client:
            response = client.get(target_url, params=params, headers={"Accept": "application/json"})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail="Unable to reach GST verification service.") from exc

    if response.status_code >= 500:
        raise HTTPException(status_code=502, detail="GST verification service error.")

    try:
        payload = response.json()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Invalid response from GST verification service.") from exc

    if not isinstance(payload, dict):
        payload = {}
    taxpayer_info = payload.get("taxpayerInfo")
    if not isinstance(taxpayer_info, dict):
        taxpayer_info = {}
    error_response = payload.get("errorResponse")
    if not isinstance(error_response, dict):
        error_response = {}

    legal_name = _safe_text(taxpayer_info.get("lgnm"))
    trade_name = _safe_text(taxpayer_info.get("tradeNam"))
    status_text = _safe_text(taxpayer_info.get("sts"))
    is_active = _is_active_gst_status(status_text)
    taxpayer_type = _safe_text(taxpayer_info.get("dty"))
    constitution_of_business = _safe_text(taxpayer_info.get("ctb"))
    registration_date = _safe_text(taxpayer_info.get("rgdt"))
    cancellation_date = _safe_text(taxpayer_info.get("cxdt"))
    state_jurisdiction = _safe_text(taxpayer_info.get("stj"))
    center_jurisdiction = _safe_text(taxpayer_info.get("ctj"))
    nature_of_business = [str(v).strip() for v in (taxpayer_info.get("nba") or []) if str(v).strip()]
    primary_addr = taxpayer_info.get("pradr")
    if isinstance(primary_addr, dict):
        primary_addr = primary_addr.get("addr")
    principal_address, pincode, state = _compose_address(primary_addr)

    valid = bool(_safe_text(taxpayer_info.get("gstin"))) and not bool(error_response)
    message = (
        _safe_text(error_response.get("message"))
        or _safe_text(taxpayer_info.get("errorMsg"))
        or ("GSTIN verified successfully." if valid else "")
    )
    is_pharmacy, pharmacy_reason = _detect_pharmacy(
        legal_name=legal_name,
        trade_name=trade_name,
        nature_of_business=nature_of_business,
    )

    return GstVerificationResponse(
        gstin=normalized_gstin,
        valid=valid,
        legal_name=legal_name,
        trade_name=trade_name,
        gst_status=status_text,
        is_active=is_active,
        taxpayer_type=taxpayer_type,
        constitution_of_business=constitution_of_business,
        registration_date=registration_date,
        cancellation_date=cancellation_date,
        state_jurisdiction=state_jurisdiction,
        center_jurisdiction=center_jurisdiction,
        principal_address=principal_address,
        pincode=pincode,
        state=state,
        nature_of_business=nature_of_business,
        is_pharmacy=is_pharmacy,
        pharmacy_match_reason=pharmacy_reason,
        message=message,
        source="cleartax_gst_search",
        upstream_status_code=int(response.status_code),
        raw=payload,
    )

@router.post("/login", response_model=LoginResponse)
def login_endpoint(
    payload: LoginRequest,
    request: Request,
) -> LoginResponse:
    with get_db_context() as db:
        ip_address = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")

        try:
            user, token, expires_at = authenticate_and_create_session(
                db=db,
                username=payload.username,
                password=payload.password,
                ip_address=ip_address,
                user_agent=user_agent,
            )
        except AuthenticationError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc

        return LoginResponse(
            access_token=token,
            expires_at=expires_at,
            user=user.as_response(),
        )


@router.post("/logout", response_model=LogoutResponse)
def logout_endpoint(
    token: str = Depends(get_bearer_token),
    _current_user: AuthenticatedUser = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> LogoutResponse:
    logged_out = revoke_session(db, token)
    return LogoutResponse(logged_out=logged_out)


@router.get("/me", response_model=AuthUserResponse)
def me_endpoint(current_user: AuthenticatedUser = Depends(get_current_user)) -> AuthUserResponse:
    return current_user.as_response()


@router.post("/users", response_model=AuthUserResponse, status_code=status.HTTP_201_CREATED)
def create_user_endpoint(
    payload: CreateUserRequest,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> AuthUserResponse:
    try:
        return create_user_account(db, payload)
    except UserAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail="username already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/users", response_model=UserListResponse)
def list_users_endpoint(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> UserListResponse:
    return list_users(db, limit=limit, offset=offset)


@router.get("/doctor-usernames")
def list_doctor_usernames_endpoint(
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(
        require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)
    ),
) -> dict:
    rows = db.execute(
        text(
            """
            WITH user_doctors AS (
                SELECT LOWER(TRIM(username)) AS username
                FROM users
                WHERE is_active = TRUE
                  AND role IN ('doctor', 'super_admin')
                  AND NULLIF(TRIM(username), '') IS NOT NULL
            ),
            assigned_doctors AS (
                SELECT LOWER(TRIM(doctor_token)) AS username
                FROM claims c
                CROSS JOIN LATERAL UNNEST(
                    string_to_array(REPLACE(COALESCE(c.assigned_doctor_id, ''), ' ', ''), ',')
                ) AS doctor_token
                WHERE NULLIF(TRIM(doctor_token), '') IS NOT NULL
            )
            SELECT DISTINCT username
            FROM (
                SELECT username FROM user_doctors
                UNION ALL
                SELECT username FROM assigned_doctors
            ) merged
            WHERE NULLIF(TRIM(username), '') IS NOT NULL
            ORDER BY username ASC
            """
        )
    ).mappings().all()
    items = [str(row.get("username") or "").strip() for row in rows if str(row.get("username") or "").strip()]
    return {"total": len(items), "items": items}




@router.get("/ifsc/verify/{ifsc_code}", response_model=IfscVerificationResponse)
def verify_ifsc_code_endpoint(
    ifsc_code: str,
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> IfscVerificationResponse:
    return _verify_ifsc_with_razorpay(ifsc_code)


@router.get("/gst/verify/{gstin}", response_model=GstVerificationResponse)
def verify_gstin_endpoint(
    gstin: str,
    captcha_token: str | None = Query(default=None, max_length=4096),
    _current_user: AuthenticatedUser = Depends(
        require_roles(UserRole.super_admin, UserRole.user, UserRole.doctor, UserRole.auditor)
    ),
) -> GstVerificationResponse:
    return _verify_gstin_with_cleartax(gstin=gstin, captcha_token=captcha_token)

@router.get("/user-bank-details", response_model=UserBankDetailsListResponse)
def list_user_bank_details_endpoint(
    search: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> UserBankDetailsListResponse:
    _ensure_user_bank_details_table(db)

    search_text = str(search or "").strip().lower()
    params = {
        "search": f"%{search_text}%" if search_text else "",
        "limit": int(limit),
        "offset": int(offset),
    }

    total = int(
        db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM users u
                LEFT JOIN user_bank_details ubd ON ubd.user_id = u.id
                WHERE CAST(u.role AS TEXT) IN ('super_admin', 'doctor')
                  AND (
                       :search = ''
                    OR LOWER(u.username) LIKE :search
                    OR LOWER(CAST(u.role AS TEXT)) LIKE :search
                    OR LOWER(COALESCE(ubd.account_holder_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.bank_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.branch_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.account_number, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.payment_rate, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.ifsc_code, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.upi_id, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.notes, '')) LIKE :search
                  )
                """
            ),
            params,
        ).scalar_one()
    )

    rows = db.execute(
        text(
            """
            SELECT
                u.id AS user_id,
                u.username,
                CAST(u.role AS TEXT) AS role,
                u.is_active AS user_is_active,
                COALESCE(ubd.account_holder_name, '') AS account_holder_name,
                COALESCE(ubd.bank_name, '') AS bank_name,
                COALESCE(ubd.branch_name, '') AS branch_name,
                COALESCE(ubd.account_number, '') AS account_number,
                COALESCE(ubd.payment_rate, '') AS payment_rate,
                COALESCE(ubd.ifsc_code, '') AS ifsc_code,
                COALESCE(ubd.upi_id, '') AS upi_id,
                COALESCE(ubd.notes, '') AS notes,
                COALESCE(ubd.is_active, TRUE) AS bank_is_active,
                COALESCE(ubd.updated_by, '') AS updated_by,
                ubd.updated_at AS updated_at
            FROM users u
                LEFT JOIN user_bank_details ubd ON ubd.user_id = u.id
                WHERE CAST(u.role AS TEXT) IN ('super_admin', 'doctor')
                  AND (
                       :search = ''
                    OR LOWER(u.username) LIKE :search
                    OR LOWER(CAST(u.role AS TEXT)) LIKE :search
                    OR LOWER(COALESCE(ubd.account_holder_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.bank_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.branch_name, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.account_number, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.payment_rate, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.ifsc_code, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.upi_id, '')) LIKE :search
                    OR LOWER(COALESCE(ubd.notes, '')) LIKE :search
                  )
            ORDER BY LOWER(u.username) ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        params,
    ).mappings().all()

    items = [
        UserBankDetailsItem(
            user_id=int(r.get("user_id") or 0),
            username=str(r.get("username") or ""),
            role=UserRole(str(r.get("role") or "user")),
            user_is_active=bool(r.get("user_is_active")),
            account_holder_name=str(r.get("account_holder_name") or ""),
            bank_name=str(r.get("bank_name") or ""),
            branch_name=str(r.get("branch_name") or ""),
            account_number=str(r.get("account_number") or ""),
            payment_rate=str(r.get("payment_rate") or ""),
            ifsc_code=str(r.get("ifsc_code") or ""),
            upi_id=str(r.get("upi_id") or ""),
            notes=str(r.get("notes") or ""),
            bank_is_active=bool(r.get("bank_is_active")),
            updated_by=str(r.get("updated_by") or ""),
            updated_at=r.get("updated_at"),
        )
        for r in rows
    ]
    return UserBankDetailsListResponse(total=total, limit=int(limit), offset=int(offset), items=items)


@router.put("/user-bank-details/{user_id}", response_model=UserBankDetailsItem)
def upsert_user_bank_details_endpoint(
    user_id: int,
    payload: UserBankDetailsUpsertRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> UserBankDetailsItem:
    _ensure_user_bank_details_table(db)

    user_row = db.execute(
        text("SELECT id, username, CAST(role AS TEXT) AS role, is_active FROM users WHERE id = :user_id LIMIT 1"),
        {"user_id": int(user_id)},
    ).mappings().first()
    if user_row is None:
        raise HTTPException(status_code=404, detail="user not found")

    target_role = str(user_row.get("role") or "").strip().lower()
    if target_role not in {"super_admin", "doctor"}:
        raise HTTPException(status_code=400, detail="bank details can only be updated for super_admin or doctor users")

    account_holder_name = _bank_text(payload.account_holder_name, 255)
    bank_name = _bank_text(payload.bank_name, 255)
    branch_name = _bank_text(payload.branch_name, 255)
    account_number = _bank_text(payload.account_number, 64)
    payment_rate = _bank_text(payload.payment_rate, 64)
    ifsc_code = _bank_text(payload.ifsc_code, 32).upper()
    upi_id = _bank_text(payload.upi_id, 255)
    notes = _bank_text(payload.notes, 2000)
    actor = str(current_user.username or "").strip()[:100]

    db.execute(
        text(
            """
            INSERT INTO user_bank_details (
                user_id,
                account_holder_name,
                bank_name,
                branch_name,
                account_number,
                payment_rate,
                ifsc_code,
                upi_id,
                notes,
                is_active,
                created_by,
                updated_by
            )
            VALUES (
                :user_id,
                :account_holder_name,
                :bank_name,
                :branch_name,
                :account_number,
                :payment_rate,
                :ifsc_code,
                :upi_id,
                :notes,
                :is_active,
                :actor,
                :actor
            )
            ON CONFLICT (user_id)
            DO UPDATE SET
                account_holder_name = EXCLUDED.account_holder_name,
                bank_name = EXCLUDED.bank_name,
                branch_name = EXCLUDED.branch_name,
                account_number = EXCLUDED.account_number,
                payment_rate = EXCLUDED.payment_rate,
                ifsc_code = EXCLUDED.ifsc_code,
                upi_id = EXCLUDED.upi_id,
                notes = EXCLUDED.notes,
                is_active = EXCLUDED.is_active,
                updated_by = EXCLUDED.updated_by,
                updated_at = NOW()
            """
        ),
        {
            "user_id": int(user_id),
            "account_holder_name": account_holder_name,
            "bank_name": bank_name,
            "branch_name": branch_name,
            "account_number": account_number,
            "payment_rate": payment_rate,
            "ifsc_code": ifsc_code,
            "upi_id": upi_id,
            "notes": notes,
            "is_active": bool(payload.is_active),
            "actor": actor,
        },
    )
    db.commit()

    row = db.execute(
        text(
            """
            SELECT
                u.id AS user_id,
                u.username,
                CAST(u.role AS TEXT) AS role,
                u.is_active AS user_is_active,
                COALESCE(ubd.account_holder_name, '') AS account_holder_name,
                COALESCE(ubd.bank_name, '') AS bank_name,
                COALESCE(ubd.branch_name, '') AS branch_name,
                COALESCE(ubd.account_number, '') AS account_number,
                COALESCE(ubd.payment_rate, '') AS payment_rate,
                COALESCE(ubd.ifsc_code, '') AS ifsc_code,
                COALESCE(ubd.upi_id, '') AS upi_id,
                COALESCE(ubd.notes, '') AS notes,
                COALESCE(ubd.is_active, TRUE) AS bank_is_active,
                COALESCE(ubd.updated_by, '') AS updated_by,
                ubd.updated_at AS updated_at
            FROM users u
            LEFT JOIN user_bank_details ubd ON ubd.user_id = u.id
            WHERE u.id = :user_id
            LIMIT 1
            """
        ),
        {"user_id": int(user_id)},
    ).mappings().first()

    if row is None:
        raise HTTPException(status_code=404, detail="user not found")

    return UserBankDetailsItem(
        user_id=int(row.get("user_id") or 0),
        username=str(row.get("username") or ""),
        role=UserRole(str(row.get("role") or "user")),
        user_is_active=bool(row.get("user_is_active")),
        account_holder_name=str(row.get("account_holder_name") or ""),
        bank_name=str(row.get("bank_name") or ""),
        branch_name=str(row.get("branch_name") or ""),
        account_number=str(row.get("account_number") or ""),
        payment_rate=str(row.get("payment_rate") or ""),
        ifsc_code=str(row.get("ifsc_code") or ""),
        upi_id=str(row.get("upi_id") or ""),
        notes=str(row.get("notes") or ""),
        bank_is_active=bool(row.get("bank_is_active")),
        updated_by=str(row.get("updated_by") or ""),
        updated_at=row.get("updated_at"),
    )
@router.post("/users/reset-password")
def reset_user_password_endpoint(
    payload: ResetUserPasswordRequest,
    db: Session = Depends(get_db),
    _current_user: AuthenticatedUser = Depends(require_roles(UserRole.super_admin)),
) -> dict:
    try:
        role = UserRole(payload.role) if payload.role else None
        admin_reset_user_password(db, payload.username, role, payload.new_password)
        return {"message": "password reset"}
    except UserNotFoundError as exc:
        raise HTTPException(status_code=404, detail="user not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
def change_password_endpoint(
    payload: UpdatePasswordRequest,
    db: Session = Depends(get_db),
    current_user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    try:
        change_own_password(db, current_user.id, payload.current_password, payload.new_password)
    except AuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc








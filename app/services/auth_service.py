from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from passlib.context import CryptContext
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.schemas.auth import AuthUserResponse, CreateUserRequest, UserListResponse, UserRole

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


class AuthenticationError(Exception):
    pass


class AuthorizationError(Exception):
    pass


class UserAlreadyExistsError(Exception):
    pass


class UserNotFoundError(Exception):
    pass


@dataclass
class AuthenticatedUser:
    id: int
    username: str
    role: UserRole
    is_active: bool

    def as_response(self) -> AuthUserResponse:
        return AuthUserResponse(id=self.id, username=self.username, role=self.role, is_active=self.is_active)


def _password_policy_error(password: str) -> str | None:
    if len(password) < 8:
        return "password must be at least 8 characters"
    if not re.search(r"[A-Z]", password):
        return "password must include an uppercase letter"
    if not re.search(r"[a-z]", password):
        return "password must include a lowercase letter"
    if not re.search(r"[0-9]", password):
        return "password must include a number"
    return None


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return pwd_context.verify(password, password_hash)
    except Exception:
        return False


def _log_auth_attempt(
    db: Session,
    user_id: int | None,
    username: str,
    role: UserRole,
    success: bool,
    ip_address: str,
    user_agent: str,
    legacy_auth_log_id: int | None = None,
) -> None:
    db.execute(
        text(
            """
            INSERT INTO auth_logs (legacy_auth_log_id, user_id, username, role, ip_address, user_agent, success)
            VALUES (:legacy_auth_log_id, :user_id, :username, :role, :ip_address, :user_agent, :success)
            ON CONFLICT (legacy_auth_log_id) DO NOTHING
            """
        ),
        {
            "legacy_auth_log_id": legacy_auth_log_id,
            "user_id": user_id,
            "username": username,
            "role": role.value,
            "ip_address": ip_address[:45] if ip_address else "unknown",
            "user_agent": (user_agent or "unknown")[:255],
            "success": success,
        },
    )


def authenticate_and_create_session(
    db: Session,
    username: str,
    password: str,
    ip_address: str,
    user_agent: str,
) -> tuple[AuthenticatedUser, str, datetime]:
    username_raw = str(username or "").strip()
    username_norm = re.sub(r"\s+", "", username_raw).lower()

    user_row = db.execute(
        text(
            """
            SELECT id, username, password_hash, role, is_active
            FROM users
            WHERE REPLACE(LOWER(username), ' ', '') = :username_norm
            LIMIT 1
            """
        ),
        {"username_norm": username_norm},
    ).mappings().first()

    role_for_log = UserRole.user
    if user_row is not None:
        try:
            role_for_log = UserRole(str(user_row["role"]))
        except Exception:
            role_for_log = UserRole.user

    if user_row is None:
        _log_auth_attempt(db, None, username.strip(), role_for_log, False, ip_address, user_agent)
        db.commit()
        raise AuthenticationError("invalid credentials")

    if not bool(user_row["is_active"]):
        _log_auth_attempt(db, int(user_row["id"]), username.strip(), role_for_log, False, ip_address, user_agent)
        db.commit()
        raise AuthenticationError("user is inactive")

    if not verify_password(password, str(user_row["password_hash"])):
        _log_auth_attempt(db, int(user_row["id"]), username.strip(), role_for_log, False, ip_address, user_agent)
        db.commit()
        raise AuthenticationError("invalid credentials")

    user = AuthenticatedUser(
        id=int(user_row["id"]),
        username=str(user_row["username"]),
        role=UserRole(str(user_row["role"])),
        is_active=bool(user_row["is_active"]),
    )

    raw_token = secrets.token_urlsafe(48)
    token_hash = _hash_token(raw_token)
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.auth_session_hours)

    db.execute(
        text(
            """
            INSERT INTO user_sessions (user_id, token_hash, role_snapshot, expires_at)
            VALUES (:user_id, :token_hash, :role_snapshot, :expires_at)
            """
        ),
        {
            "user_id": user.id,
            "token_hash": token_hash,
            "role_snapshot": user.role.value,
            "expires_at": expires_at,
        },
    )
    _log_auth_attempt(db, user.id, user.username, user.role, True, ip_address, user_agent)
    db.commit()

    return user, raw_token, expires_at


def get_user_by_token(db: Session, raw_token: str) -> AuthenticatedUser | None:
    if not raw_token:
        return None

    token_hash = _hash_token(raw_token)
    row = db.execute(
        text(
            """
            SELECT u.id, u.username, u.role, u.is_active
            FROM user_sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = :token_hash
              AND s.revoked_at IS NULL
              AND s.expires_at > NOW()
            LIMIT 1
            """
        ),
        {"token_hash": token_hash},
    ).mappings().first()

    if row is None:
        return None

    return AuthenticatedUser(
        id=int(row["id"]),
        username=str(row["username"]),
        role=UserRole(str(row["role"])),
        is_active=bool(row["is_active"]),
    )


def revoke_session(db: Session, raw_token: str) -> bool:
    token_hash = _hash_token(raw_token)
    updated = db.execute(
        text(
            """
            UPDATE user_sessions
            SET revoked_at = NOW()
            WHERE token_hash = :token_hash
              AND revoked_at IS NULL
            """
        ),
        {"token_hash": token_hash},
    ).rowcount
    db.commit()
    return bool(updated)


def create_user_account(db: Session, payload: CreateUserRequest) -> AuthUserResponse:
    policy_error = _password_policy_error(payload.password)
    if policy_error is not None:
        raise ValueError(policy_error)

    try:
        row = db.execute(
            text(
                """
                INSERT INTO users (username, password_hash, role, is_active)
                VALUES (:username, :password_hash, :role, TRUE)
                RETURNING id, username, role, is_active
                """
            ),
            {
                "username": payload.username.strip(),
                "password_hash": hash_password(payload.password),
                "role": payload.role.value,
            },
        ).mappings().one()
        db.commit()
        return AuthUserResponse.model_validate(dict(row))
    except IntegrityError as exc:
        db.rollback()
        if getattr(exc.orig, "sqlstate", None) == "23505":
            raise UserAlreadyExistsError from exc
        raise


def list_users(db: Session, limit: int = 100, offset: int = 0) -> UserListResponse:
    total = db.execute(text("SELECT COUNT(*) FROM users")).scalar_one()
    rows = db.execute(
        text(
            """
            SELECT id, username, role, is_active
            FROM users
            ORDER BY username ASC
            LIMIT :limit OFFSET :offset
            """
        ),
        {"limit": limit, "offset": offset},
    ).mappings().all()
    items = [AuthUserResponse.model_validate(dict(row)) for row in rows]
    return UserListResponse(total=total, items=items)


def change_own_password(db: Session, user_id: int, current_password: str, new_password: str) -> None:
    row = db.execute(
        text("SELECT id, password_hash FROM users WHERE id = :user_id LIMIT 1"),
        {"user_id": user_id},
    ).mappings().first()

    if row is None:
        raise UserNotFoundError

    if not verify_password(current_password, str(row["password_hash"])):
        raise AuthenticationError("current password is incorrect")

    policy_error = _password_policy_error(new_password)
    if policy_error is not None:
        raise ValueError(policy_error)

    db.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :user_id"),
        {"user_id": user_id, "password_hash": hash_password(new_password)},
    )
    db.commit()



def admin_reset_user_password(db: Session, username: str, role: UserRole | None, new_password: str) -> None:
    policy_error = _password_policy_error(new_password)
    if policy_error is not None:
        raise ValueError(policy_error)

    row = db.execute(
        text("SELECT id, role FROM users WHERE username = :username LIMIT 1"),
        {"username": username.strip()},
    ).mappings().first()

    if row is None:
        raise UserNotFoundError

    if role is not None and str(row.get("role")) != role.value:
        raise ValueError("username exists but role does not match")

    db.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
        {"id": int(row["id"]), "password_hash": hash_password(new_password)},
    )
    db.commit()

def ensure_bootstrap_super_admin(db: Session, username: str, password: str) -> None:
    if not username or not password:
        return

    existing = db.execute(
        text("SELECT id FROM users WHERE username = :username LIMIT 1"),
        {"username": username},
    ).mappings().first()

    if existing is not None:
        return

    payload = CreateUserRequest(username=username, password=password, role=UserRole.super_admin)
    create_user_account(db, payload)


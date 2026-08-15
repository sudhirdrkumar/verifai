"""
Database utilities for proper transaction management.
Use these patterns in all endpoints to ensure transactions are committed/closed.
"""
from contextlib import contextmanager
from typing import Any, Generator

from sqlalchemy.orm import Session

from app.db.session import SessionLocal


@contextmanager
def get_db_context() -> Generator[Session, None, None]:
    """
    Context manager for automatic transaction management.

    Usage:
        with get_db_context() as db:
            result = db.query(Model).first()
            # Auto-commits on success, rolls back on error
            return result
    """
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def safe_db_operation(func) -> Any:
    """
    Decorator for automatic transaction management in endpoint functions.

    Usage:
        @router.get("/endpoint")
        @safe_db_operation
        def endpoint(db: Session = None):
            # db is automatically created and committed
            result = db.query(Model).first()
            return result
    """
    def wrapper(*args, db: Session = None, **kwargs):
        if db is None:
            with get_db_context() as db:
                return func(*args, db=db, **kwargs)
        else:
            # If db is provided (via Depends), just call the function
            # NOTE: Caller responsible for commit
            return func(*args, db=db, **kwargs)

    return wrapper


def ensure_commit(db: Session) -> None:
    """
    Explicitly commit a database transaction.
    Use at the end of endpoint functions that perform writes.
    """
    db.commit()


def ensure_rollback(db: Session) -> None:
    """Explicitly rollback a database transaction."""
    db.rollback()

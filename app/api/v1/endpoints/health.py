from fastapi import APIRouter

from app.db.session import db_ping

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    is_db_reachable = db_ping()
    return {
        "status": "ok" if is_db_reachable else "degraded",
        "database": "reachable" if is_db_reachable else "unreachable",
    }

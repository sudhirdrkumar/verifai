from fastapi import APIRouter

from app.db.session import db_ping, get_connection_pool_status

router = APIRouter(tags=["health"])


@router.get("/health")
def health_check() -> dict:
    is_db_reachable = db_ping()
    pool_status = get_connection_pool_status()

    connection_warning = pool_status["checked_out"] > pool_status["pool_size"]

    return {
        "status": "ok" if is_db_reachable and not connection_warning else "degraded",
        "database": "reachable" if is_db_reachable else "unreachable",
        "pool": {
            "size": pool_status["pool_size"],
            "checked_out": pool_status["checked_out"],
            "available": pool_status["available"],
            "warning": connection_warning,
        },
    }


@router.get("/health/pool")
def pool_status() -> dict:
    """Get detailed connection pool status (for monitoring/debugging)."""
    return get_connection_pool_status()

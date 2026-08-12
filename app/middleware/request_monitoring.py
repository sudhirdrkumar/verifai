"""
Request monitoring middleware - detect slow/stuck transactions and connection issues.
"""
import logging
import time
from typing import Callable

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from app.db.session import get_connection_pool_status

logger = logging.getLogger(__name__)

# Thresholds for warnings/errors
SLOW_REQUEST_THRESHOLD = 10.0  # seconds
VERY_SLOW_REQUEST_THRESHOLD = 30.0  # seconds
POOL_WARNING_THRESHOLD = 0.8  # 80% of pool


class RequestMonitoringMiddleware(BaseHTTPMiddleware):
    """Monitor request duration and database connection pool usage."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        start_time = time.time()
        pool_before = get_connection_pool_status()

        try:
            response = await call_next(request)
        except Exception as exc:
            duration = time.time() - start_time
            logger.error(
                f"Request error: {request.method} {request.url.path} "
                f"failed after {duration:.2f}s: {exc}"
            )
            raise

        duration = time.time() - start_time
        pool_after = get_connection_pool_status()

        # Log metrics
        _log_request_metrics(
            request=request,
            duration=duration,
            pool_before=pool_before,
            pool_after=pool_after,
            status_code=response.status_code,
        )

        # Add custom headers for debugging
        response.headers["X-Request-Duration"] = f"{duration:.3f}"
        response.headers["X-Pool-Usage"] = f"{pool_after['checked_out']}/{pool_after['pool_size']}"

        return response


def _log_request_metrics(
    request: Request,
    duration: float,
    pool_before: dict,
    pool_after: dict,
    status_code: int,
) -> None:
    """Log request metrics and alert on issues."""
    method = request.method
    path = request.url.path
    query = request.url.query

    # Detect slow requests
    if duration > VERY_SLOW_REQUEST_THRESHOLD:
        logger.critical(
            f"CRITICAL: Very slow request: {method} {path} took {duration:.2f}s "
            f"(pool: {pool_after['checked_out']}/{pool_after['pool_size']})"
        )
    elif duration > SLOW_REQUEST_THRESHOLD:
        logger.warning(
            f"SLOW: {method} {path} took {duration:.2f}s "
            f"(pool: {pool_after['checked_out']}/{pool_after['pool_size']})"
        )

    # Detect pool exhaustion
    pool_usage = pool_after["checked_out"] / pool_after["pool_size"] if pool_after["pool_size"] > 0 else 0
    if pool_usage > POOL_WARNING_THRESHOLD:
        logger.warning(
            f"POOL WARNING: {method} {path} - Pool at {pool_usage*100:.1f}% "
            f"({pool_after['checked_out']}/{pool_after['pool_size']} connections)"
        )

    if pool_after.get("warning"):
        logger.error(
            f"POOL CRITICAL: {method} {path} - Pool overflow! "
            f"Checked out: {pool_after['checked_out']} "
            f"(max: {pool_after['pool_size'] + 10})"
        )

    # Log info for debugging
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            f"Request: {method} {path}{'?' + query if query else ''} "
            f"Status: {status_code} Duration: {duration:.3f}s "
            f"Pool: {pool_before['checked_out']}->{pool_after['checked_out']}/{pool_after['pool_size']}"
        )


class TransactionGuardMiddleware(BaseHTTPMiddleware):
    """Detect requests that might be leaving transactions open."""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # Don't monitor health checks
        if request.url.path in ["/health", "/api/v1/health", "/api/v1/health/pool"]:
            return await call_next(request)

        start_time = time.time()
        pool_before = get_connection_pool_status()

        try:
            response = await call_next(request)
        except Exception:
            raise

        duration = time.time() - start_time
        pool_after = get_connection_pool_status()

        # Check if pool connections increased (might indicate unclosed transaction)
        connections_held = pool_after["checked_out"] - pool_before["checked_out"]

        if connections_held > 0 and duration > SLOW_REQUEST_THRESHOLD:
            logger.warning(
                f"TRANSACTION GUARD: {request.method} {request.url.path} "
                f"held {connections_held} connection(s) for {duration:.2f}s - "
                f"possible unclosed transaction!"
            )

        return response

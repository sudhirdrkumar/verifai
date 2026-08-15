from __future__ import annotations

from typing import Any

from app.core.config import settings


def get_redis_client() -> Any | None:
    redis_url = str(settings.redis_url or "").strip()
    if not redis_url:
        return None

    try:
        import redis  # type: ignore
    except Exception:
        return None

    try:
        client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=3, socket_connect_timeout=3)
        client.ping()
        return client
    except Exception:
        return None

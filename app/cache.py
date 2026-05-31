"""
Redis cache layer — real-time metric caching & pub/sub for dashboard.
"""
import os
import json
from typing import Any, Optional

import structlog
import redis.asyncio as aioredis

log = structlog.get_logger()

REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

_redis: Optional[aioredis.Redis] = None


async def init_cache() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
    try:
        await _redis.ping()
        log.info("redis_connected", url=REDIS_URL)
    except Exception as exc:
        log.error("redis_connection_failed", error=str(exc))
        raise


async def close_cache() -> None:
    global _redis
    if _redis:
        await _redis.aclose()
        log.info("redis_closed")


def get_cache() -> aioredis.Redis:
    if _redis is None:
        raise RuntimeError("Redis not initialised")
    return _redis


async def check_cache_health() -> bool:
    try:
        r = get_cache()
        await r.ping()
        return True
    except Exception:
        return False


# ── Metric helpers ────────────────────────────────────────────────────────────

METRICS_TTL = 30  # seconds


async def set_metrics(store_id: str, data: dict) -> None:
    r = get_cache()
    key = f"metrics:{store_id}"
    await r.setex(key, METRICS_TTL, json.dumps(data, default=str))


async def get_metrics(store_id: str) -> Optional[dict]:
    r = get_cache()
    raw = await r.get(f"metrics:{store_id}")
    return json.loads(raw) if raw else None


async def invalidate_metrics(store_id: str) -> None:
    r = get_cache()
    await r.delete(f"metrics:{store_id}")


# ── Last event timestamp tracker ─────────────────────────────────────────────

async def update_last_event_time(store_id: str, ts: str) -> None:
    r = get_cache()
    await r.set(f"last_event:{store_id}", ts)


async def get_last_event_time(store_id: str) -> Optional[str]:
    r = get_cache()
    return await r.get(f"last_event:{store_id}")


async def get_all_store_ids() -> list[str]:
    r = get_cache()
    keys = await r.keys("last_event:*")
    return [k.split(":", 1)[1] for k in keys]


# ── Pub/Sub for live dashboard ────────────────────────────────────────────────

async def publish_event(store_id: str, payload: dict) -> None:
    r = get_cache()
    channel = f"store_events:{store_id}"
    await r.publish(channel, json.dumps(payload, default=str))


async def get_pubsub() -> aioredis.client.PubSub:
    r = get_cache()
    return r.pubsub()

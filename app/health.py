"""
Health endpoint — service status, DB/cache connectivity, per-store feed lag.
"""
from datetime import datetime, timezone, timedelta
from typing import List

import structlog

from app.models import HealthResponse, StoreFeedStatus
import app.db as db
import app.cache as cache

log = structlog.get_logger()

STALE_FEED_MINUTES = 10
VERSION = "1.0.0"


async def get_all_store_ids():
    return await cache.get_all_store_ids()


async def get_last_event_time(store_id: str):
    return await cache.get_last_event_time(store_id)


async def get_health() -> HealthResponse:
    now = datetime.now(timezone.utc)

    db_ok    = await db.check_db_health()
    cache_ok = await cache.check_cache_health()

    try:
        store_ids = await get_all_store_ids()
    except Exception:
        # Redis not initialised or error reading keys — treat as no stores and degraded cache
        store_ids = []
        cache_ok = False
    feeds: List[StoreFeedStatus] = []

    for sid in store_ids:
        try:
            raw = await get_last_event_time(sid)
        except Exception:
            raw = None
        if raw is None:
            feeds.append(StoreFeedStatus(
                store_id=sid,
                last_event_at=None,
                lag_minutes=None,
                status="NO_DATA",
            ))
            continue

        last_ts = datetime.fromisoformat(raw)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)

        lag_min = (now - last_ts).total_seconds() / 60
        status = "STALE_FEED" if lag_min > STALE_FEED_MINUTES else "OK"

        feeds.append(StoreFeedStatus(
            store_id=sid,
            last_event_at=last_ts,
            lag_minutes=round(lag_min, 2),
            status=status,
        ))

    overall = "healthy"
    if not db_ok or not cache_ok:
        overall = "degraded"

    return HealthResponse(
        status=overall,
        version=VERSION,
        db_connected=db_ok,
        cache_connected=cache_ok,
        store_feeds=feeds,
        checked_at=now,
    )

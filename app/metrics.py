"""
Real-time metric computation for /stores/{id}/metrics
All queries exclude is_staff=true events.
"""
from datetime import datetime, timezone, timedelta
from typing import Optional

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreMetrics, ZoneDwell

log = structlog.get_logger()

# Default window: last 24 hours (today's metrics)
DEFAULT_WINDOW_HOURS = 24


async def compute_store_metrics(
    store_id: str,
    db: AsyncSession,
    window_hours: int = DEFAULT_WINDOW_HOURS,
) -> StoreMetrics:
    """
    Compute live store metrics for `window_hours`.
    Returns zero-value metrics safely if no data exists.
    """
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(hours=window_hours)

    unique_visitors    = await _unique_visitors(store_id, window_start, db)
    converted_visitors = await _converted_visitors(store_id, window_start, db)
    avg_dwell          = await _avg_dwell(store_id, window_start, db)
    zone_dwells        = await _zone_dwell_breakdown(store_id, window_start, db)
    queue_depth        = await _current_queue_depth(store_id, db)
    abandonment_rate   = await _abandonment_rate(store_id, window_start, db)

    conversion_rate = (
        round(converted_visitors / unique_visitors, 4) if unique_visitors > 0 else 0.0
    )

    confidence = _data_confidence(unique_visitors)

    return StoreMetrics(
        store_id=store_id,
        window_start=window_start,
        window_end=now,
        unique_visitors=unique_visitors,
        converted_visitors=converted_visitors,
        conversion_rate=conversion_rate,
        avg_dwell_ms=avg_dwell,
        zone_dwell=zone_dwells,
        queue_depth=queue_depth,
        abandonment_rate=abandonment_rate,
        data_confidence=confidence,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Metric sub-queries
# ─────────────────────────────────────────────────────────────────────────────

async def _unique_visitors(store_id: str, since: datetime, db: AsyncSession) -> int:
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ENTRY'
          AND is_staff = FALSE
          AND timestamp >= :since
    """)
    result = await db.execute(sql, {"store_id": store_id, "since": since})
    row = result.scalar()
    return int(row or 0)


async def _converted_visitors(store_id: str, since: datetime, db: AsyncSession) -> int:
    """
    Visitors whose visitor_id had a BILLING_QUEUE_JOIN and a POS transaction
    within 5 minutes of their billing zone activity.
    """
    sql = text("""
        SELECT COUNT(DISTINCT vs.visitor_id)
        FROM visitor_sessions vs
        WHERE vs.store_id = :store_id
          AND vs.was_in_billing = TRUE
          AND vs.is_converted = TRUE
          AND vs.entry_time >= :since
    """)
    result = await db.execute(sql, {"store_id": store_id, "since": since})
    row = result.scalar()
    return int(row or 0)


async def _avg_dwell(store_id: str, since: datetime, db: AsyncSession) -> float:
    """Average dwell time across all ZONE_DWELL events (customer only)."""
    sql = text("""
        SELECT COALESCE(AVG(dwell_ms), 0)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'ZONE_DWELL'
          AND is_staff = FALSE
          AND timestamp >= :since
    """)
    result = await db.execute(sql, {"store_id": store_id, "since": since})
    row = result.scalar()
    return round(float(row or 0), 2)


async def _zone_dwell_breakdown(
    store_id: str, since: datetime, db: AsyncSession
) -> list[ZoneDwell]:
    sql = text("""
        SELECT zone_id,
               AVG(dwell_ms)   AS avg_dwell,
               COUNT(*)        AS visit_count
        FROM events
        WHERE store_id = :store_id
          AND event_type IN ('ZONE_DWELL', 'ZONE_ENTER')
          AND zone_id IS NOT NULL
          AND is_staff = FALSE
          AND timestamp >= :since
        GROUP BY zone_id
        ORDER BY avg_dwell DESC
    """)
    result = await db.execute(sql, {"store_id": store_id, "since": since})
    rows = result.fetchall()
    return [
        ZoneDwell(
            zone_id=r.zone_id,
            avg_dwell_ms=round(float(r.avg_dwell), 2),
            visit_count=int(r.visit_count),
        )
        for r in rows
    ]


async def _current_queue_depth(store_id: str, db: AsyncSession) -> int:
    """Latest queue_depth value from the most recent BILLING_QUEUE_JOIN."""
    sql = text("""
        SELECT queue_depth
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    """)
    result = await db.execute(sql, {"store_id": store_id})
    row = result.scalar()
    return int(row or 0)


async def _abandonment_rate(store_id: str, since: datetime, db: AsyncSession) -> float:
    """Fraction of BILLING_QUEUE_JOIN sessions that ended in BILLING_QUEUE_ABANDON."""
    sql_joins = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND is_staff = FALSE
          AND timestamp >= :since
    """)
    sql_abandons = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id = :store_id
          AND event_type = 'BILLING_QUEUE_ABANDON'
          AND is_staff = FALSE
          AND timestamp >= :since
    """)
    joins    = (await db.execute(sql_joins,    {"store_id": store_id, "since": since})).scalar() or 0
    abandons = (await db.execute(sql_abandons, {"store_id": store_id, "since": since})).scalar() or 0
    return round(abandons / joins, 4) if joins > 0 else 0.0


def _data_confidence(session_count: int) -> str:
    if session_count >= 50:  return "HIGH"
    if session_count >= 20:  return "MEDIUM"
    return "LOW"

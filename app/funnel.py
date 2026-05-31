"""
Conversion funnel: Entry → Zone Visit → Billing Queue → Purchase
Session is the unit — re-entries do NOT double-count a visitor.
"""
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreFunnel, FunnelStage

log = structlog.get_logger()


async def compute_funnel(
    store_id: str,
    db: AsyncSession,
    window_hours: int = 24,
) -> StoreFunnel:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    # Stage 1 — unique visitors (ENTRY events, customer only)
    entered = await _count_distinct_visitors(store_id, "ENTRY", since, db)

    # Stage 2 — visitors who visited at least one product zone
    visited_zone = await _count_zone_visitors(store_id, since, db)

    # Stage 3 — visitors who joined billing queue
    reached_billing = await _count_billing_visitors(store_id, since, db)

    # Stage 4 — visitors who completed a purchase (session is_converted=TRUE)
    purchased = await _count_converted(store_id, since, db)

    # Build funnel stages with drop-off %
    stages = _build_stages([
        ("Entry",          entered),
        ("Zone Visit",     visited_zone),
        ("Billing Queue",  reached_billing),
        ("Purchase",       purchased),
    ])

    return StoreFunnel(
        store_id=store_id,
        stages=stages,
        sessions=entered,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Sub-queries
# ─────────────────────────────────────────────────────────────────────────────

async def _count_distinct_visitors(
    store_id: str, event_type: str, since: datetime, db: AsyncSession
) -> int:
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id   = :store_id
          AND event_type = :event_type
          AND is_staff   = FALSE
          AND timestamp  >= :since
    """)
    r = await db.execute(sql, {"store_id": store_id, "event_type": event_type, "since": since})
    return int(r.scalar() or 0)


async def _count_zone_visitors(store_id: str, since: datetime, db: AsyncSession) -> int:
    """Distinct visitors who entered at least one non-billing zone."""
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id   = :store_id
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
          AND is_staff   = FALSE
          AND timestamp  >= :since
    """)
    r = await db.execute(sql, {"store_id": store_id, "since": since})
    return int(r.scalar() or 0)


async def _count_billing_visitors(store_id: str, since: datetime, db: AsyncSession) -> int:
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id   = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND is_staff   = FALSE
          AND timestamp  >= :since
    """)
    r = await db.execute(sql, {"store_id": store_id, "since": since})
    return int(r.scalar() or 0)


async def _count_converted(store_id: str, since: datetime, db: AsyncSession) -> int:
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM visitor_sessions
        WHERE store_id       = :store_id
          AND is_converted   = TRUE
          AND entry_time     >= :since
    """)
    r = await db.execute(sql, {"store_id": store_id, "since": since})
    return int(r.scalar() or 0)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_stages(named_counts: list[tuple[str, int]]) -> list[FunnelStage]:
    stages = []
    for i, (name, count) in enumerate(named_counts):
        prev_count = named_counts[i - 1][1] if i > 0 else count
        drop_off = (
            round((1 - count / prev_count) * 100, 2) if prev_count > 0 else 0.0
        )
        stages.append(FunnelStage(stage=name, count=count, drop_off_pct=drop_off))
    return stages

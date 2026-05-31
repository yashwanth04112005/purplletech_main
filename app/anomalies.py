"""
Anomaly detection engine.
Detects: BILLING_QUEUE_SPIKE, CONVERSION_DROP, DEAD_ZONE, STALE_FEED, EMPTY_STORE
Severity scale: INFO → WARN → CRITICAL
"""
import os
import uuid
from datetime import datetime, timezone, timedelta
from typing import List

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Anomaly, AnomalyList, AnomalyType, Severity
from app.cache import get_last_event_time

log = structlog.get_logger()

# Thresholds (overridable via env)
QUEUE_SPIKE_THRESHOLD    = int(os.getenv("ANOMALY_QUEUE_SPIKE_THRESHOLD", 5))
CONVERSION_DROP_PCT      = float(os.getenv("ANOMALY_CONVERSION_DROP_PCT", 20))
DEAD_ZONE_MINUTES        = int(os.getenv("ANOMALY_DEAD_ZONE_MINUTES", 30))
STALE_FEED_MINUTES       = int(os.getenv("STALE_FEED_MINUTES", 10))


async def detect_anomalies(store_id: str, db: AsyncSession) -> AnomalyList:
    now = datetime.now(timezone.utc)
    anomalies: List[Anomaly] = []

    # Run all detectors concurrently (sequential here for clarity — add asyncio.gather if needed)
    q = await _check_queue_spike(store_id, now, db)
    if q:
        anomalies.append(q)

    c = await _check_conversion_drop(store_id, now, db)
    if c:
        anomalies.append(c)

    dz = await _check_dead_zones(store_id, now, db)
    anomalies.extend(dz)

    sf = await _check_stale_feed(store_id, now)
    if sf:
        anomalies.append(sf)

    es = await _check_empty_store(store_id, now, db)
    if es:
        anomalies.append(es)

    return AnomalyList(store_id=store_id, anomalies=anomalies)


# ─────────────────────────────────────────────────────────────────────────────
# Detectors
# ─────────────────────────────────────────────────────────────────────────────

async def _check_queue_spike(
    store_id: str, now: datetime, db: AsyncSession
) -> Anomaly | None:
    """CRITICAL if current queue depth exceeds threshold."""
    sql = text("""
        SELECT queue_depth
        FROM events
        WHERE store_id   = :store_id
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
        ORDER BY timestamp DESC
        LIMIT 1
    """)
    r = await db.execute(sql, {"store_id": store_id})
    depth = r.scalar()
    if depth is None or depth < QUEUE_SPIKE_THRESHOLD:
        return None

    severity = Severity.CRITICAL if depth >= QUEUE_SPIKE_THRESHOLD * 2 else Severity.WARN
    return Anomaly(
        anomaly_id=str(uuid.uuid4()),
        store_id=store_id,
        anomaly_type=AnomalyType.BILLING_QUEUE_SPIKE,
        severity=severity,
        detected_at=now,
        description=f"Billing queue depth is {depth} (threshold: {QUEUE_SPIKE_THRESHOLD})",
        suggested_action="Open an additional billing counter immediately. Alert floor manager.",
        metadata={"queue_depth": depth, "threshold": QUEUE_SPIKE_THRESHOLD},
    )


async def _check_conversion_drop(
    store_id: str, now: datetime, db: AsyncSession
) -> Anomaly | None:
    """WARN if today's conversion rate is >20% below 7-day average."""
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = now - timedelta(days=7)

    sql_today = text("""
        SELECT
            COUNT(DISTINCT CASE WHEN is_converted THEN visitor_id END) AS converted,
            COUNT(DISTINCT visitor_id) AS total
        FROM visitor_sessions
        WHERE store_id   = :store_id
          AND entry_time >= :today_start
    """)
    sql_week = text("""
        SELECT
            COUNT(DISTINCT CASE WHEN is_converted THEN visitor_id END) AS converted,
            COUNT(DISTINCT visitor_id) AS total
        FROM visitor_sessions
        WHERE store_id   = :store_id
          AND entry_time >= :week_start
          AND entry_time  < :today_start
    """)

    rt = await db.execute(sql_today, {"store_id": store_id, "today_start": today_start})
    rw = await db.execute(sql_week, {
        "store_id": store_id, "week_start": week_start, "today_start": today_start
    })

    rt_row = rt.fetchone()
    rw_row = rw.fetchone()

    today_rate = (rt_row.converted / rt_row.total) if rt_row and rt_row.total else None
    week_rate  = (rw_row.converted / rw_row.total) if rw_row and rw_row.total else None

    if today_rate is None or week_rate is None or week_rate == 0:
        return None

    drop_pct = ((week_rate - today_rate) / week_rate) * 100
    if drop_pct < CONVERSION_DROP_PCT:
        return None

    severity = Severity.CRITICAL if drop_pct > CONVERSION_DROP_PCT * 1.5 else Severity.WARN
    return Anomaly(
        anomaly_id=str(uuid.uuid4()),
        store_id=store_id,
        anomaly_type=AnomalyType.CONVERSION_DROP,
        severity=severity,
        detected_at=now,
        description=(
            f"Conversion rate dropped {drop_pct:.1f}% vs 7-day avg "
            f"(today: {today_rate:.1%}, avg: {week_rate:.1%})"
        ),
        suggested_action=(
            "Review billing queue abandonment. Check if promotion is running. "
            "Verify POS system is operational."
        ),
        metadata={
            "today_rate": round(today_rate, 4),
            "week_avg_rate": round(week_rate, 4),
            "drop_pct": round(drop_pct, 2),
        },
    )


async def _check_dead_zones(
    store_id: str, now: datetime, db: AsyncSession
) -> list[Anomaly]:
    """INFO for every zone with zero visits in the last DEAD_ZONE_MINUTES."""
    cutoff = now - timedelta(minutes=DEAD_ZONE_MINUTES)

    # All known zones for this store (from events history)
    sql_all = text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id = :store_id AND zone_id IS NOT NULL
    """)
    sql_active = text("""
        SELECT DISTINCT zone_id FROM events
        WHERE store_id  = :store_id
          AND zone_id   IS NOT NULL
          AND timestamp >= :cutoff
          AND is_staff  = FALSE
    """)

    all_zones    = {r[0] for r in (await db.execute(sql_all,    {"store_id": store_id})).fetchall()}
    active_zones = {r[0] for r in (await db.execute(sql_active, {"store_id": store_id, "cutoff": cutoff})).fetchall()}
    dead_zones   = all_zones - active_zones

    return [
        Anomaly(
            anomaly_id=str(uuid.uuid4()),
            store_id=store_id,
            anomaly_type=AnomalyType.DEAD_ZONE,
            severity=Severity.INFO,
            detected_at=now,
            description=f"Zone '{z}' has had no customer visits in {DEAD_ZONE_MINUTES} minutes.",
            suggested_action=(
                f"Check if zone '{z}' display is correctly stocked and accessible. "
                "Consider a staff walkthrough."
            ),
            metadata={"zone_id": z, "dead_minutes": DEAD_ZONE_MINUTES},
        )
        for z in sorted(dead_zones)
    ]


async def _check_stale_feed(store_id: str, now: datetime) -> Anomaly | None:
    """WARN if the last event for this store is older than STALE_FEED_MINUTES."""
    raw = await get_last_event_time(store_id)
    if raw is None:
        return None  # No data yet — handled by EMPTY_STORE check

    from datetime import datetime
    last_ts = datetime.fromisoformat(raw)
    if last_ts.tzinfo is None:
        last_ts = last_ts.replace(tzinfo=timezone.utc)

    lag_min = (now - last_ts).total_seconds() / 60
    if lag_min < STALE_FEED_MINUTES:
        return None

    severity = Severity.CRITICAL if lag_min > STALE_FEED_MINUTES * 3 else Severity.WARN
    return Anomaly(
        anomaly_id=str(uuid.uuid4()),
        store_id=store_id,
        anomaly_type=AnomalyType.STALE_FEED,
        severity=severity,
        detected_at=now,
        description=f"No events received from {store_id} for {lag_min:.1f} minutes.",
        suggested_action="Check camera connectivity and detection pipeline process health.",
        metadata={"lag_minutes": round(lag_min, 1), "last_event_at": raw},
    )


async def _check_empty_store(
    store_id: str, now: datetime, db: AsyncSession
) -> Anomaly | None:
    """INFO if store has zero active visitors right now."""
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM visitor_sessions
        WHERE store_id  = :store_id
          AND entry_time IS NOT NULL
          AND exit_time  IS NULL
    """)
    r = await db.execute(sql, {"store_id": store_id})
    active = int(r.scalar() or 0)

    if active > 0:
        return None

    return Anomaly(
        anomaly_id=str(uuid.uuid4()),
        store_id=store_id,
        anomaly_type=AnomalyType.EMPTY_STORE,
        severity=Severity.INFO,
        detected_at=now,
        description="No active visitors detected in store right now.",
        suggested_action="Normal during off-peak periods. Verify camera feed if unexpected.",
        metadata={"active_visitors": 0},
    )

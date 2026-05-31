"""
Zone heatmap: visit frequency + avg dwell, normalised 0–100.
"""
from datetime import datetime, timezone, timedelta

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import StoreHeatmap, HeatmapCell

log = structlog.get_logger()

LOW_SESSION_THRESHOLD = 20


async def compute_heatmap(
    store_id: str,
    db: AsyncSession,
    window_hours: int = 24,
) -> StoreHeatmap:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=window_hours)

    rows = await _zone_stats(store_id, since, db)
    total_sessions = await _total_sessions(store_id, since, db)

    # Normalise visit_frequency to 0–100
    max_freq = max((r.visit_count for r in rows), default=1) or 1
    max_dwell = max((float(r.avg_dwell) for r in rows), default=1) or 1

    confidence_global = "LOW" if total_sessions < LOW_SESSION_THRESHOLD else "HIGH"

    cells = []
    for r in rows:
        freq_score  = (r.visit_count / max_freq)   * 50
        dwell_score = (float(r.avg_dwell) / max_dwell) * 50
        normalised  = round(freq_score + dwell_score, 1)
        cells.append(HeatmapCell(
            zone_id=r.zone_id,
            visit_frequency=int(r.visit_count),
            avg_dwell_ms=round(float(r.avg_dwell), 2),
            normalised_score=min(normalised, 100.0),
            data_confidence="LOW" if int(r.visit_count) < LOW_SESSION_THRESHOLD else "HIGH",
        ))

    # Sort descending by score
    cells.sort(key=lambda c: c.normalised_score, reverse=True)

    return StoreHeatmap(
        store_id=store_id,
        generated_at=now,
        total_sessions=total_sessions,
        cells=cells,
        data_confidence=confidence_global,
    )


async def _zone_stats(store_id: str, since: datetime, db: AsyncSession):
    sql = text("""
        SELECT zone_id,
               COUNT(DISTINCT visitor_id) AS visit_count,
               COALESCE(AVG(dwell_ms), 0) AS avg_dwell
        FROM events
        WHERE store_id   = :store_id
          AND zone_id    IS NOT NULL
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
          AND is_staff   = FALSE
          AND timestamp  >= :since
        GROUP BY zone_id
        ORDER BY visit_count DESC
    """)
    r = await db.execute(sql, {"store_id": store_id, "since": since})
    return r.fetchall()


async def _total_sessions(store_id: str, since: datetime, db: AsyncSession) -> int:
    sql = text("""
        SELECT COUNT(DISTINCT visitor_id)
        FROM events
        WHERE store_id   = :store_id
          AND event_type = 'ENTRY'
          AND is_staff   = FALSE
          AND timestamp  >= :since
    """)
    r = await db.execute(sql, {"store_id": store_id, "since": since})
    return int(r.scalar() or 0)

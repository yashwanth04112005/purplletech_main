"""
Event ingestion: validate → deduplicate → store → update sessions → publish.
"""
import json
import uuid
from datetime import datetime, timezone
from typing import List, Tuple

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EventType, IngestResponse, EventError, StoreEvent
from app.cache import invalidate_metrics, update_last_event_time, publish_event

log = structlog.get_logger()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

async def ingest_events(
    events: List[StoreEvent],
    db: AsyncSession,
    trace_id: str = "",
) -> IngestResponse:
    """
    Idempotent batch ingest of up to 500 events.
    - Deduplicates by event_id (UUID)
    - Partial success: malformed events are rejected with reason
    - Updates visitor sessions after insertion
    - Publishes to Redis pub/sub for live dashboard
    """
    accepted = 0
    rejected = 0
    duplicates = 0
    errors: List[EventError] = []

    for idx, event in enumerate(events):
        try:
            inserted = await _insert_event(event, db)
            if inserted == "duplicate":
                duplicates += 1
            else:
                accepted += 1
                await _update_session(event, db)
                await update_last_event_time(event.store_id, event.timestamp.isoformat())
                await invalidate_metrics(event.store_id)
                await publish_event(event.store_id, _event_to_dict(event))
        except Exception as exc:
            log.warning(
                "event_ingest_failed",
                idx=idx,
                event_id=str(event.event_id),
                error=str(exc),
                trace_id=trace_id,
            )
            errors.append(EventError(
                index=idx,
                event_id=str(event.event_id),
                error=str(exc),
            ))
            rejected += 1

    return IngestResponse(
        accepted=accepted,
        rejected=rejected,
        duplicates=duplicates,
        errors=errors,
        trace_id=trace_id,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

async def _insert_event(event: StoreEvent, db: AsyncSession) -> str:
    """Insert one event — returns 'inserted' or 'duplicate'."""
    sql = text("""
        INSERT INTO events (
            event_id, store_id, camera_id, visitor_id, event_type,
            timestamp, zone_id, dwell_ms, is_staff, confidence,
            queue_depth, sku_zone, session_seq, raw_payload
        )
        VALUES (
            :event_id, :store_id, :camera_id, :visitor_id, :event_type,
            :timestamp, :zone_id, :dwell_ms, :is_staff, :confidence,
            :queue_depth, :sku_zone, :session_seq, :raw_payload
        )
        ON CONFLICT (event_id) DO NOTHING
        RETURNING event_id
    """)

    result = await db.execute(sql, {
        "event_id":    str(event.event_id),
        "store_id":    event.store_id,
        "camera_id":   event.camera_id,
        "visitor_id":  event.visitor_id,
        "event_type":  event.event_type.value,
        "timestamp":   event.timestamp,
        "zone_id":     event.zone_id,
        "dwell_ms":    event.dwell_ms,
        "is_staff":    event.is_staff,
        "confidence":  event.confidence,
        "queue_depth": event.metadata.queue_depth,
        "sku_zone":    event.metadata.sku_zone,
        "session_seq": event.metadata.session_seq,
        "raw_payload": json.dumps(_event_to_dict(event), default=str),
    })

    row = result.fetchone()
    return "inserted" if row else "duplicate"


async def _update_session(event: StoreEvent, db: AsyncSession) -> None:
    """Maintain the visitor_sessions table based on incoming event type."""
    if event.is_staff:
        return  # Staff events never appear in session tracking

    etype = event.event_type

    if etype in (EventType.ENTRY, EventType.REENTRY):
        await _upsert_session_entry(event, db)

    elif etype == EventType.EXIT:
        await _close_session(event, db)

    elif etype in (EventType.ZONE_ENTER, EventType.ZONE_EXIT, EventType.ZONE_DWELL):
        await _record_zone_visit(event, db)

    elif etype == EventType.BILLING_QUEUE_JOIN:
        await _mark_billing_zone(event, db)

    elif etype == EventType.BILLING_QUEUE_ABANDON:
        await _record_abandon(event, db)


async def _upsert_session_entry(event: StoreEvent, db: AsyncSession) -> None:
    # PostgreSQL expects text[] for zones_visited; SQLite tests store it as text.
    dialect = ""
    try:
        if db.bind is not None and db.bind.dialect is not None:
            dialect = db.bind.dialect.name or ""
    except Exception:
        dialect = ""

    zones_value = [] if dialect == "postgresql" else "{}"

    sql = text("""
        INSERT INTO visitor_sessions (store_id, visitor_id, entry_time, zones_visited)
        VALUES (:store_id, :visitor_id, :entry_time, :zones_visited)
        ON CONFLICT DO NOTHING
    """)
    await db.execute(sql, {
        "store_id":      event.store_id,
        "visitor_id":    event.visitor_id,
        "entry_time":    event.timestamp,
        "zones_visited": zones_value,
    })


async def _close_session(event: StoreEvent, db: AsyncSession) -> None:
    sql = text("""
        UPDATE visitor_sessions
        SET exit_time = :exit_time, updated_at = CURRENT_TIMESTAMP
        WHERE visitor_id = :visitor_id AND store_id = :store_id
          AND exit_time IS NULL
    """)
    await db.execute(sql, {
        "exit_time":  event.timestamp,
        "visitor_id": event.visitor_id,
        "store_id":   event.store_id,
    })


async def _record_zone_visit(event: StoreEvent, db: AsyncSession) -> None:
    if not event.zone_id:
        return
    # Check if zone already recorded (portable: no array_append / ANY)
    check = text("""
        SELECT zones_visited FROM visitor_sessions
        WHERE visitor_id = :visitor_id AND store_id = :store_id
          AND exit_time IS NULL
    """)
    row = (await db.execute(check, {
        "visitor_id": event.visitor_id, "store_id": event.store_id
    })).fetchone()
    if row is None:
        return
    zones_raw = row.zones_visited or ""
    # PostgreSQL stores as text[], SQLite as string — handle both
    if isinstance(zones_raw, list):
        zones = zones_raw
    else:
        zones = [z for z in str(zones_raw).strip("{}").split(",") if z]
    if event.zone_id in zones:
        return
    zones.append(event.zone_id)
    new_val = "{" + ",".join(zones) + "}"
    sql = text("""
        UPDATE visitor_sessions
        SET zones_visited = :zones, updated_at = CURRENT_TIMESTAMP
        WHERE visitor_id = :visitor_id AND store_id = :store_id
          AND exit_time IS NULL
    """)
    await db.execute(sql, {
        "zones":      new_val,
        "visitor_id": event.visitor_id,
        "store_id":   event.store_id,
    })


async def _mark_billing_zone(event: StoreEvent, db: AsyncSession) -> None:
    sql = text("""
        UPDATE visitor_sessions
        SET was_in_billing = TRUE, updated_at = CURRENT_TIMESTAMP
        WHERE visitor_id = :visitor_id AND store_id = :store_id
          AND exit_time IS NULL
    """)
    await db.execute(sql, {
        "visitor_id": event.visitor_id,
        "store_id":   event.store_id,
    })


async def _record_abandon(event: StoreEvent, db: AsyncSession) -> None:
    # Queue abandon is tracked via events table — no session field needed
    pass


def _event_to_dict(event: StoreEvent) -> dict:
    return {
        "event_id":   str(event.event_id),
        "store_id":   event.store_id,
        "camera_id":  event.camera_id,
        "visitor_id": event.visitor_id,
        "event_type": event.event_type.value,
        "timestamp":  event.timestamp.isoformat(),
        "zone_id":    event.zone_id,
        "dwell_ms":   event.dwell_ms,
        "is_staff":   event.is_staff,
        "confidence": event.confidence,
        "metadata":   event.metadata.model_dump(),
    }

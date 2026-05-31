"""
POST /events/ingest  — batch event ingestion endpoint
"""
from typing import Any

from fastapi import APIRouter, Depends, Request, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import IngestResponse, StoreEvent
from app.ingestion import ingest_events

router = APIRouter()


@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_207_MULTI_STATUS,
    summary="Batch ingest store events (idempotent by event_id)",
)
async def ingest(
    body: dict[str, Any],
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Accepts a batch of up to 500 events.
    - Idempotent: duplicate event_ids are silently skipped (counted as `duplicates`)
    - Partial success: invalid events are rejected per-item, valid ones are accepted
    - Returns 503 if database is unavailable
    """
    trace_id = getattr(request.state, "trace_id", "")

    raw_events = body.get("events", [])
    if not isinstance(raw_events, list) or len(raw_events) == 0:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="events must be a non-empty list")
    if len(raw_events) > 500:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="batch size exceeds 500")

    # Per-event validation — invalid items become rejected entries
    from app.models import EventError
    valid_events: list[StoreEvent] = []
    pre_errors: list[EventError] = []
    for idx, raw in enumerate(raw_events):
        try:
            valid_events.append(StoreEvent.model_validate(raw))
        except Exception as exc:
            pre_errors.append(EventError(
                index=idx,
                event_id=str(raw.get("event_id", "")) if isinstance(raw, dict) else None,
                error=str(exc),
            ))

    if not valid_events and pre_errors:
        from app.models import IngestResponse
        return IngestResponse(
            accepted=0, rejected=len(pre_errors), duplicates=0,
            errors=pre_errors, trace_id=trace_id,
        )

    try:
        result = await ingest_events(valid_events, db, trace_id=trace_id)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "database_unavailable", "trace_id": trace_id},
        )

    # Merge pre-validation errors into the result
    result.rejected += len(pre_errors)
    result.errors = pre_errors + result.errors
    return result

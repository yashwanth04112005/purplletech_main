"""
Store analytics routers:
  GET /stores/{id}/metrics
  GET /stores/{id}/funnel
  GET /stores/{id}/heatmap
  GET /stores/{id}/anomalies
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import StoreMetrics, StoreFunnel, StoreHeatmap, AnomalyList
from app.metrics import compute_store_metrics
from app.funnel import compute_funnel
from app.heatmap import compute_heatmap
from app.anomalies import detect_anomalies
from app.cache import get_metrics, set_metrics

router = APIRouter()


@router.get(
    "/{store_id}/metrics",
    response_model=StoreMetrics,
    summary="Real-time store metrics (unique visitors, conversion rate, dwell, queue)",
)
async def get_metrics_endpoint(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168, description="Lookback window in hours"),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns live metrics — never served from stale cache.
    Excludes is_staff=true events from all counts.
    Returns zero-value response (not null, not 5xx) if store has no data.
    """
    try:
        # Check short-lived cache (30s TTL) for non-default windows
        if window_hours == 24:
            cached = await get_metrics(store_id)
            if cached:
                return StoreMetrics(**cached)

        result = await compute_store_metrics(store_id, db, window_hours)

        if window_hours == 24:
            await set_metrics(store_id, result.model_dump())

        return result
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "metrics_unavailable", "store_id": store_id, "message": str(exc)},
        )


@router.get(
    "/{store_id}/funnel",
    response_model=StoreFunnel,
    summary="Conversion funnel: Entry → Zone → Billing → Purchase with drop-off %",
)
async def get_funnel_endpoint(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    """
    Session-level funnel. Re-entries are deduplicated — one visitor = one funnel unit.
    """
    try:
        return await compute_funnel(store_id, db, window_hours)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "funnel_unavailable", "store_id": store_id, "message": str(exc)},
        )


@router.get(
    "/{store_id}/heatmap",
    response_model=StoreHeatmap,
    summary="Zone heatmap: visit frequency + avg dwell normalised 0–100",
)
async def get_heatmap_endpoint(
    store_id: str,
    window_hours: int = Query(default=24, ge=1, le=168),
    db: AsyncSession = Depends(get_db),
):
    """
    Returns a normalised heatmap per zone. Includes data_confidence flag
    if fewer than 20 sessions exist in the window.
    """
    try:
        return await compute_heatmap(store_id, db, window_hours)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "heatmap_unavailable", "store_id": store_id, "message": str(exc)},
        )


@router.get(
    "/{store_id}/anomalies",
    response_model=AnomalyList,
    summary="Active anomalies: queue spike, conversion drop, dead zone, stale feed",
)
async def get_anomalies_endpoint(
    store_id: str,
    db: AsyncSession = Depends(get_db),
):
    """
    Runs all anomaly detectors synchronously and returns current active anomalies.
    Severity: INFO | WARN | CRITICAL. Each includes suggested_action.
    """
    try:
        return await detect_anomalies(store_id, db)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"error": "anomaly_detection_unavailable", "store_id": store_id, "message": str(exc)},
        )

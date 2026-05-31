"""
GET /health — service health + feed staleness.
"""
from fastapi import APIRouter
from app.models import HealthResponse
from app.health import get_health

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Service health: DB, cache, per-store feed lag",
    tags=["Health"],
)
async def health_endpoint():
    """
    Returns:
    - DB and Redis connectivity
    - Last event timestamp per store
    - STALE_FEED warning if any store feed lag > 10 minutes
    This is what an on-call engineer checks first.
    """
    return await get_health()

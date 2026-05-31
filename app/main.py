"""
Store Intelligence API — FastAPI entrypoint
"""
import os
import uuid
import time
import structlog
from contextlib import asynccontextmanager

from app.logging_config import configure_logging
configure_logging(log_level=os.getenv("LOG_LEVEL", "INFO"))

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.db import init_db, close_db
from app.cache import init_cache, close_cache
from app.routers import events, stores, health, ws

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown lifecycle."""
    # Allow skipping startup connectivity checks for local validation (set SKIP_STARTUP=true)
    if os.getenv("SKIP_STARTUP", "false").lower() != "true":
        await init_db()
        await init_cache()
    else:
        log.info("startup_skipped", reason="SKIP_STARTUP=true")
    log.info("store_intelligence_api_started")
    yield
    if os.getenv("SKIP_STARTUP", "false").lower() != "true":
        await close_db()
        await close_cache()
    else:
        log.info("shutdown_skipped", reason="SKIP_STARTUP=true")
    log.info("store_intelligence_api_stopped")


app = FastAPI(
    title="Store Intelligence API",
    description="Real-time retail analytics from CCTV event streams",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def structured_logging_middleware(request: Request, call_next):
    """Log every request with trace_id, latency, store_id, status."""
    trace_id = str(uuid.uuid4())
    store_id = request.path_params.get("store_id", "N/A")
    start = time.perf_counter()
    event_count = None

    if request.method == "POST" and request.url.path == "/events/ingest":
        try:
            payload = await request.json()
            events = payload.get("events", []) if isinstance(payload, dict) else []
            event_count = len(events) if isinstance(events, list) else None
        except Exception:
            event_count = None

    request.state.trace_id = trace_id

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        log.error(
            "unhandled_exception",
            trace_id=trace_id,
            path=request.url.path,
            error=str(exc),
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "trace_id": trace_id},
        )

    latency_ms = round((time.perf_counter() - start) * 1000, 2)
    log.info(
        "http_request",
        trace_id=trace_id,
        store_id=store_id,
        endpoint=request.url.path,
        method=request.method,
        status_code=response.status_code,
        latency_ms=latency_ms,
        event_count=event_count,
    )
    response.headers["X-Trace-Id"] = trace_id
    return response


# ── Routers ──────────────────────────────────────────────────────────────────
app.include_router(events.router, prefix="/events", tags=["Events"])
app.include_router(stores.router, prefix="/stores", tags=["Stores"])
app.include_router(health.router, tags=["Health"])
app.include_router(ws.router, tags=["WebSocket"])

"""
WebSocket router — live dashboard push for Part E.
Subscribes to Redis pub/sub channel per store and streams events to browser.
"""
import asyncio
import json

import structlog
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.cache import get_pubsub, get_metrics, get_cache

log = structlog.get_logger()

router = APIRouter()


@router.websocket("/ws/{store_id}")
async def websocket_feed(websocket: WebSocket, store_id: str):
    """
    WebSocket endpoint — connects client to live event stream for a store.
    Pushes:
      - Raw events as they are ingested (via Redis pub/sub)
      - A metrics snapshot every 5 seconds
    """
    await websocket.accept()
    log.info("ws_client_connected", store_id=store_id)

    pubsub = await get_pubsub()
    await pubsub.subscribe(f"store_events:{store_id}")

    try:
        # Push current metrics immediately on connect
        cached = await get_metrics(store_id)
        if cached:
            await websocket.send_json({"type": "metrics_snapshot", "data": cached})

        async def _listen_pubsub():
            async for message in pubsub.listen():
                if message["type"] == "message":
                    payload = json.loads(message["data"])
                    await websocket.send_json({"type": "event", "data": payload})

        async def _heartbeat():
            """Push a metrics snapshot every 5 seconds."""
            while True:
                await asyncio.sleep(5)
                metrics = await get_metrics(store_id)
                if metrics:
                    await websocket.send_json({"type": "metrics_snapshot", "data": metrics})

        # Run both concurrently
        await asyncio.gather(_listen_pubsub(), _heartbeat())

    except WebSocketDisconnect:
        log.info("ws_client_disconnected", store_id=store_id)
    except Exception as exc:
        log.error("ws_error", store_id=store_id, error=str(exc))
    finally:
        await pubsub.unsubscribe(f"store_events:{store_id}")
        await pubsub.aclose()

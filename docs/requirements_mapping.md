Requirements → Code mapping

This file maps each high-level requirement from the PDF to where it is implemented in the repo.

1) Detection layer (emit structured events)
- Files: pipeline/detect.py, pipeline/tracker.py, pipeline/emit.py, pipeline/staff_detector.py, pipeline/zone_mapper.py
- Notes: `detect.py` uses YOLOv8 + `PersonTracker` to emit events via `EventEmitter` (see pipeline/emit.py).

2) Event schema
- Files: app/models.py, pipeline/emit.py
- Notes: Pydantic models in `app/models.py` define the required fields (event_id, store_id, camera_id, visitor_id, event_type, timestamp, zone_id, dwell_ms, is_staff, confidence, metadata).

3) Ingest endpoint, idempotency, partial success
- Files: app/routers/events.py, app/ingestion.py
- Notes: `POST /events/ingest` validates batches (<=500), uses DB `ON CONFLICT` for idempotency, returns 207 Multi-Status for partial success.

4) Metrics, funnel, heatmap, anomalies
- Files: app/metrics.py, app/funnel.py, app/heatmap.py, app/anomalies.py
- Notes: Metrics endpoint computes unique visitors, conversion rate, avg dwell; funnel endpoint computes Entry→Zone→Billing→Purchase; anomalies detect queue spikes and conversion drops.

5) POS correlation (5-minute window)
- Files: app/pos_correlation.py
- Notes: `load_pos_transactions` and `run_conversion_matching` implement time-window correlation and mark sessions converted.

6) Health + structured logging
- Files: app/health.py, app/logging_config.py, app/main.py
- Notes: `/health` endpoint returns DB/cache status and per-store last event time; middleware logs trace_id, store_id, endpoint, latency_ms.

7) Production readiness (docker compose, README)
- Files: docker-compose.yml, app/Dockerfile, dashboard/Dockerfile, README.md
- Notes: `docker-compose.yml` defines `postgres`, `redis`, `api`, `dashboard`. README includes 5-command quickstart.

8) Tests and AI documentation
- Files: tests/* (prompt blocks in tests), docs/DESIGN.md, docs/CHOICES.md
- Notes: Prompt blocks are present in test files; DESIGN.md and CHOICES.md include AI-assisted decisions and reasoning.

9) Live dashboard
- Files: dashboard/server.js, dashboard/public/index.html, app/routers/ws.py
- Notes: `server.js` proxies FastAPI WebSocket to browsers; `app/routers/ws.py` subscribes to Redis pub/sub to push events.

Status summary:
- Implemented: schema, ingest, metrics, funnel, POS correlation, health, logging, dashboard code.
- Partially implemented / needs runtime: detection model weights and GPU setup for the pipeline; Docker/run-time validation (requires Docker or external Postgres+Redis).

How to validate locally (commands):

1) Start with Docker (recommended):

```bash
cp .env.example .env
# edit .env if you need custom credentials
docker compose up -d
```

2) Validate API + health:

```bash
curl http://localhost:8000/health
# should return JSON with db_connected=true and cache_connected=true when DB/Redis are up
```

3) Validate dashboard:

Open: http://localhost:3000?store=STORE_BLR_002

4) Replay events (simulated real-time):

```bash
python pipeline/replay.py --events-dir data/events --api-url http://localhost:8000 --speed 10
```

If you prefer, I can produce automated local-check scripts or add a `make` target to run acceptance checks.

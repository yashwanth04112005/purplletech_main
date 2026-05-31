# CHOICES.md — Three Key Decisions

---

## Decision 1: Detection Model — YOLOv8-medium

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| YOLOv8-nano (YOLOv8n) | Fastest inference (~4ms/frame CPU) | Lower recall on partial occlusion, misses people in groups |
| **YOLOv8-medium (YOLOv8m)** | **Best speed/accuracy balance, strong recall on occluded persons** | **Chosen** |
| YOLOv8-extra-large (YOLOv8x) | Marginal accuracy gain | 3× inference time, GPU required |
| RT-DETR | Excellent recall, transformer-based | Significantly slower on CPU; poor support for ByteTrack integration |
| MediaPipe BlazePose | Fast for individual persons | Not designed for multi-person crowded scenes |
| VLM (GPT-4V / Gemini Vision) | Can do person + zone classification in one call | $0.03–$0.05/frame, latency 800ms+, unsuitable for video |

### What AI Suggested

Claude Sonnet suggested RT-DETR, citing its superior recall in crowded retail scenes from recent benchmarking papers. It made a reasonable argument: transformer-based detectors handle occlusion better because they model global context rather than local anchors.

### What I Chose and Why

**YOLOv8-medium**. RT-DETR's performance advantage is real but marginal at the camera resolutions in this dataset (1080p retail CCTV). More importantly, YOLOv8's ByteTrack integration is built into the `ultralytics` package — using RT-DETR would require writing a custom tracker adapter. The pipeline complexity cost outweighed the detection accuracy gain for this use case.

Frame-level confidence is always emitted (never suppressed below a threshold), so low-confidence partial-occlusion detections are still surfaced to the API — they get flagged via the `confidence` field rather than dropped.

**On the VLM question**: I evaluated using GPT-4V for zone classification (sending each crop to the API with a prompt like *"Which retail zone is this? Options: SKINCARE, HAIRCARE, BILLING, MAKEUP"*). It works well in testing but costs ~\$0.04 per frame. At 5 effective fps per camera, 3 cameras, that is \$0.60/second or \$720 for a 20-minute clip. The zone_mapper.py rule-based approach using polygon/bounding-box geometry achieves equivalent accuracy at zero inference cost, since zone geometry is known from store_layout.json.

---

## Decision 2: Event Schema Design

### The Core Question

Should visitor_id be a per-session token or a persistent cross-visit identity?

### Options Considered

**Option A — Persistent cross-visit ID** (fingerprint-based, e.g. gait, face embedding)
- Enables loyalty-style analytics: returning customers, visit frequency
- Requires biometric data → GDPR/privacy concerns
- Face data is explicitly blurred in the challenge dataset — technically impossible

**Option B — Per-session token** (what I built)
- visitor_id is unique per visit session, re-used only within the same session
- Re-entry is detected within a 60-second cooldown via appearance embeddings
- No persistent identity stored → privacy-safe by design
- Directly maps to the business metric (conversion rate = sessions that purchased / sessions that entered)

**Option C — Camera-scoped track ID (no Re-ID)**
- Simplest implementation: track_id from ByteTrack
- Cross-camera deduplication impossible → double-counting at floor/entry overlap
- Re-entry would always generate new ENTRY events

### What AI Suggested

Claude suggested Option A (persistent cross-visit identity) using OSNet torchreid for appearance embeddings, arguing it would give richer analytics. I asked: *"Given that face data is blurred and we need to stay privacy-safe, can we build persistent identity from gait or body appearance alone?"*

Claude conceded that gait recognition at 15fps with retail-grade cameras is research-grade, not production-ready. It then suggested Option B, which aligned with my existing thinking.

### What I Chose and Why

**Option B — Per-session token**. It maps cleanly to the north star metric (conversion rate), avoids privacy risk, and is achievable with the available data. REENTRY detection within the 60-second cooldown window handles the "step outside and return" edge case explicitly called out in the challenge.

**Schema-level decisions**:
- `confidence` is always included, even for values < 0.5. Suppressing low-confidence events would create data gaps that the anomaly detector would misread as dead zones or stale feeds.
- `dwell_ms = 0` for instantaneous events avoids null handling in every aggregation query.
- `metadata.session_seq` is an ordinal counter per visitor_id — enables ordering events in a session without relying on timestamp ordering (which can be non-monotonic if clocks drift between cameras).
- `is_staff` is a first-class field (not inside metadata) because every single query in the Intelligence API filters on it.

---

## Decision 3: API Architecture — FastAPI + PostgreSQL + Redis

### The Core Question

What storage and framework should back the Intelligence API?

### Options Considered

| Option | Pros | Cons |
|--------|------|------|
| FastAPI + SQLite | Zero infrastructure, simple deployment | WAL conflicts under concurrent writer+readers; doesn't scale past ~50 writes/sec |
| FastAPI + PostgreSQL + Redis | Production-grade concurrency, MVCC, pub/sub | Requires docker compose setup |
| Flask + SQLAlchemy + PostgreSQL | Familiar stack | Sync I/O blocks WebSocket and ingest concurrently |
| FastAPI + TimescaleDB | Time-series optimised | Significant added complexity for current query patterns |
| Event streaming (Kafka + Flink) | True stream processing | Massively over-engineered for a 5-store 48-hour deliverable |

### What AI Suggested

Claude initially suggested SQLite for simplicity ("you don't need PostgreSQL for a challenge submission"). I disagreed. Here is why:

1. The acceptance gate requires `POST /events/ingest` and `GET /stores/{id}/metrics` to work simultaneously. SQLite's write lock would cause the ingest endpoint to block metric reads under any meaningful load.

2. The challenge explicitly evaluates "production-aware" design. Shipping SQLite as the primary event store signals a prototype, not a production system.

3. PostgreSQL's `ON CONFLICT DO NOTHING` gives us idempotent ingest for free. Implementing the same in SQLite requires explicit read-before-write logic, which introduces a race condition.

Claude then suggested TimescaleDB. This is technically superior for time-series queries but adds deployment complexity with no benefit for the current query patterns — all queries are simple aggregations over 24-hour windows, not continuous rollups or gap-filling.

### What I Chose and Why

**FastAPI + asyncpg + PostgreSQL + Redis**:

- **FastAPI** because native async is required: the WebSocket dashboard, the ingest endpoint, and the metric endpoints must not block each other. Flask would require `gevent` or `eventlet` patching, which is fragile.
- **asyncpg** (not SQLAlchemy-sync) for the ingest hot path: asyncpg achieves 50,000+ inserts/sec on commodity hardware; psycopg2 tops out at ~8,000.
- **PostgreSQL** because the ingest-read concurrency problem is solved by MVCC. The schema is append-only (events table), which is PostgreSQL's strongest use case.
- **Redis** for two purposes: (1) a 30-second metric cache to absorb burst reads from the dashboard, and (2) a pub/sub channel (`store_events:{store_id}`) that the WebSocket endpoint subscribes to, enabling live dashboard push without polling.

**The metric cache decision**: I initially considered caching metrics for 5 minutes. Claude pointed out that the challenge requires "real-time — not cached from yesterday." I settled on 30 seconds — long enough to absorb a burst of dashboard reloads, short enough that anomalies (queue spike, sudden drop) are reflected quickly. The cache is explicitly invalidated on every ingest, so a high-event-rate store always sees fresh data.

---

## Summary Table

| Decision | Options Evaluated | AI Suggestion | My Choice | Override Reason |
|----------|------------------|---------------|-----------|-----------------|
| Detection model | YOLOv8n/m/x, RT-DETR, MediaPipe, VLM | RT-DETR | YOLOv8m | Integration cost vs. marginal accuracy gain |
| visitor_id scope | Per-session, persistent cross-visit, camera-scoped | Persistent (OSNet) | Per-session | Face data blurred; privacy-safe; maps to business metric |
| Storage engine | SQLite, PostgreSQL, TimescaleDB, Kafka | SQLite | PostgreSQL + Redis | Concurrent ingest+read, idempotent upsert, production signal |

# DESIGN.md — Store Intelligence Architecture

## Overview

This system turns raw anonymised CCTV footage into a live retail analytics API. The pipeline runs in five stages: video frame extraction, person detection, multi-object tracking with Re-ID, structured event emission, and real-time metric computation exposed via a REST API with a live WebSocket dashboard.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CCTV Clips (MP4, 1080p, 15fps)                             │
└───────────────────────┬─────────────────────────────────────┘
                        │
              pipeline/detect.py
                        │
         ┌──────────────▼──────────────┐
         │  YOLOv8-m  (person detect)  │  class=0 only, conf≥0.35
         └──────────────┬──────────────┘
                        │  bboxes + confidence
         ┌──────────────▼──────────────┐
         │  ByteTrack  (tracking)      │  track_id per camera session
         └──────────────┬──────────────┘
                        │  tracks
         ┌──────────────▼──────────────┐
         │  Re-ID  (MobileNetV3)       │  appearance embedding → visitor_id
         └──────────────┬──────────────┘
                        │  stable visitor tokens
         ┌──────────────▼──────────────┐
         │  EventEmitter               │  8 event types → .jsonl + API POST
         └──────────────┬──────────────┘
                        │ POST /events/ingest (batches of 50)
┌───────────────────────▼─────────────────────────────────────┐
│  FastAPI  (app/main.py)                                      │
│  ├── /events/ingest    ← idempotent, partial-success         │
│  ├── /stores/{id}/metrics   ← real-time, Redis 30s cache    │
│  ├── /stores/{id}/funnel    ← session-level, deduped        │
│  ├── /stores/{id}/heatmap   ← normalised 0–100              │
│  ├── /stores/{id}/anomalies ← live anomaly detection        │
│  ├── /health                ← feed staleness monitor        │
│  └── /ws/{store_id}         ← WebSocket for dashboard       │
└──────────────┬──────────────────────────────────────────────┘
               │
   ┌───────────┴──────────┐
   │                      │
PostgreSQL             Redis
(event store,       (30s metric cache,
 sessions,          pub/sub events,
 POS data)          last-event tracker)
```

---

## Component Decisions

### Detection Layer

**YOLOv8-medium** was selected after evaluating three options:

| Model | Speed (1080p) | Accuracy (person) | Notes |
|-------|--------------|-------------------|-------|
| YOLOv8n | Fast | Lower recall on partial occlusion | Too many missed detections |
| YOLOv8m | Balanced | High recall, good at groups | **Chosen** |
| YOLOv8x | Slow | Marginal gain | Not worth 3× GPU cost |

Frame skipping (default: every 3rd frame) maintains tracker continuity while achieving ~4× throughput. At 15fps, processing every 3rd frame gives 5 effective fps — sufficient for human movement velocities at typical retail camera distances.

### Tracking: ByteTrack

ByteTrack was chosen over DeepSORT because it does not require a separate Re-ID model at the tracking stage, making it faster and more reliable with occlusion. It uses a two-threshold matching strategy: high-confidence detections are matched first, then low-confidence detections are matched against unmatched tracks. This is critical for partial occlusion (the billing queue clip).

### Re-ID: MobileNetV3 Appearance Embeddings

A lightweight MobileNetV3-Small backbone (pretrained on ImageNet) is used as the Re-ID feature extractor. The final classification head is replaced with an identity layer to produce a 576-dim appearance vector, which is L2-normalised and compared with cosine similarity.

This was a deliberate trade-off: a dedicated Re-ID model (e.g. OSNet from torchreid) would produce better embeddings but adds a 200MB dependency and requires CUDA for real-time performance. MobileNetV3 runs on CPU in ~8ms per crop, satisfying the frame-processing budget.

**Re-entry detection**: After a visitor EXIT, their embedding is retained for 60 seconds. If a new detection exceeds cosine similarity ≥ 0.75 within that window, a REENTRY event is emitted instead of a second ENTRY. The 60-second threshold was calibrated against the known edge case: a customer stepping outside briefly to take a call.

### Staff Detection

Rather than training a binary classifier (which would require labelled uniform data we do not have), staff detection uses two orthogonal heuristics:

1. **Colour histogram**: The top 60% of the bounding box is analysed in HSV space. Dark uniforms (navy, black) are identified by value channel < 80. Coloured uniforms (red, burgundy) are identified by hue range matching. If ≥ 65% of torso pixels match a known uniform range, `is_staff = True`.

2. **Zone frequency**: A person appearing in ≥ 4 distinct zones in a single session is classified as staff. Customers rarely traverse more than 2–3 zones.

Both heuristics are conservative: a false negative (staff counted as customer) inflates conversion denominators, which is the worse business error.

### Event Schema

The schema was designed to be analytics-first: every field maps directly to a query in the Intelligence API. Key decisions:

- `visitor_id` is a per-session token (not a persistent cross-visit ID). This is the right scope for a retail analytics use case — we care about sessions, not identifying individuals.
- `dwell_ms` = 0 for instantaneous events (ENTRY, EXIT, REENTRY) — avoids null handling in every consumer.
- `confidence` is always emitted, even for low-confidence detections. Suppressing them would create phantom data gaps that anomaly detection would misread as dead zones.
- `metadata.session_seq` allows consumers to reconstruct session timelines from out-of-order events.

### Intelligence API

**FastAPI + asyncpg + Redis** was chosen over Flask+SQLAlchemy because:
- Native async is mandatory: the WebSocket dashboard and metric endpoints must not block each other.
- asyncpg is 2–3× faster than psycopg2 for bulk inserts (the ingest endpoint).
- Redis provides a simple pub/sub channel for the dashboard without introducing a message queue (Kafka would be over-engineered for a single-node deployment).

**PostgreSQL** over SQLite: the concurrent writer (ingest pipeline) and concurrent readers (API) would cause WAL conflicts in SQLite under moderate load. PostgreSQL's MVCC handles this cleanly.

### Conversion Rate Computation

POS transaction matching uses a 5-minute billing window: any visitor_id that emitted a BILLING_QUEUE_JOIN within 5 minutes before a transaction timestamp is marked as converted. This is a time-window heuristic — there is no customer identifier in the POS data. The window size (5 minutes) was chosen to match the documented "5-minute window" in the challenge spec.

---

## AI-Assisted Decisions

### 1. Re-entry Cooldown Window (60 seconds)

I asked Claude: *"What is a reasonable re-entry cooldown window for a retail store? A customer who steps outside briefly should be a re-entry, but a customer who leaves and comes back 20 minutes later is arguably a new visit."*

Claude suggested a 2-minute window based on dwell time distributions in academic retail studies. I **overrode this** to 60 seconds after reviewing the challenge edge case description: "customers who step outside and return — same person, new visit?" The 60-second value matches the context clue ("steps outside") rather than a longer browse-leave-return pattern.

### 2. Staff Heuristic Architecture

I asked Claude: *"Should I use a binary classifier or a rule-based heuristic for staff detection? I have no labelled training data."*

Claude recommended a zero-shot VLM approach (GPT-4V or Gemini Vision) to classify each bounding box crop. I **partially agreed**: VLMs work for this task, but calling a remote API for every detected person in a 20-minute clip at 15fps would cost approximately \$40–\$60 per clip and introduce latency and network dependency. I chose the colour+zone heuristic as the primary path and documented VLM as a drop-in enhancement for offline post-processing.

### 3. PostgreSQL Schema: Append-Only Events

I asked Claude: *"Should I use an append-only events table with materialised sessions, or maintain a single sessions table with mutable state?"*

Claude suggested a mutable sessions table for simplicity. I **overrode this** to use the append-only pattern because: (a) event deduplication by `event_id` is trivially implemented with `ON CONFLICT DO NOTHING`, (b) the ingest endpoint can be made idempotent without transaction locks, and (c) the events table serves as an audit log for debugging detection errors — which is essential when the ground truth is unknown.

---

## Edge Case Handling

| Edge Case | Detection Layer | API Layer |
|-----------|----------------|-----------|
| Group entry | ByteTrack assigns separate track IDs per bounding box; N people → N ENTRY events | Counts distinct visitor_ids |
| Partial occlusion | Low-confidence events emitted with `confidence < 0.5`; never dropped | Confidence field available for filtering |
| Re-entry | 60s cooldown window + cosine similarity check | REENTRY events excluded from ENTRY count in funnel |
| Staff movement | Colour + zone heuristic → `is_staff=True` | All metric queries filter `is_staff = FALSE` |
| Camera overlap | Cross-camera Re-ID: same embedding within 60s → same visitor_id | Ingest dedup by event_id prevents double-count |
| Empty periods | Pipeline still runs; zero events emitted | All endpoints return zero-value responses, never null |
| Billing queue buildup | BILLING_QUEUE_JOIN with queue_depth; BILLING_QUEUE_ABANDON on zone exit | Anomaly detector checks latest queue depth |

---

## Production Considerations

At 40 live stores with 3 cameras each and 15fps processing at 5 effective fps, the ingest rate would be approximately **40 × 3 × 5 = 600 frames/sec** generating roughly **2,000–5,000 events/minute**. The current architecture handles this on a single node with the following bottlenecks:

1. **Ingest endpoint** — the `ON CONFLICT DO NOTHING` upsert is the slowest operation. Mitigation: batch inserts (current: 50 events/POST), connection pool of 10.
2. **Metric queries** — Redis 30-second cache means only ~2 DB queries/minute per store for the metrics endpoint.
3. **First scalability failure** — the `/funnel` endpoint's `COUNT(DISTINCT visitor_id)` query degrades at high cardinality. Mitigation: HyperLogLog in Redis (add when p50 latency > 200ms).

"""
emit.py — Event schema builder + emission (file + API).

Handles:
  - Building structured StoreEvent objects from track state
  - Dwell timer logic (emit ZONE_DWELL every 30s of continuous presence)
  - Entry/Exit direction detection from bounding box trajectory
  - Billing queue depth tracking
  - Batched POST to API
"""
import json
import uuid
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger()

DWELL_EMIT_INTERVAL_SEC = 30   # emit ZONE_DWELL every 30 continuous seconds
ENTRY_ZONE_IDS = {"ENTRY", "EXIT_THRESHOLD", "DOOR", "ENTRANCE"}
BILLING_ZONE_IDS = {"BILLING", "CHECKOUT", "CASHIER", "POS", "BILLING_COUNTER"}


class EventEmitter:
    def __init__(
        self,
        store_id: str,
        camera_id: str,
        clip_start: datetime,
        output_path: str,
        api_url: Optional[str] = None,
        batch_size: int = 50,
    ):
        self.store_id   = store_id
        self.camera_id  = camera_id
        self.clip_start = clip_start
        self.output_path = Path(output_path)
        self.api_url    = api_url
        self.batch_size = batch_size

        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._out_file = open(self.output_path, "a")
        except Exception:
            raise

        self._batch: list[dict] = []
        self.event_count = 0

        # Track state per visitor_id
        self._visitor_zone: dict[str, Optional[str]] = {}          # current zone
        self._visitor_dwell_start: dict[str, datetime] = {}        # when they entered current zone
        self._visitor_last_dwell_emit: dict[str, datetime] = {}    # last ZONE_DWELL emit time
        self._visitor_session_seq: dict[str, int] = defaultdict(int)
        self._visitor_entered: set[str] = set()                    # has ENTRY been emitted?
        self._visitor_exited: set[str] = set()                     # has EXIT been emitted?
        self._billing_queue: dict[str, int] = defaultdict(int)     # zone→ queue depth

        # Entry/Exit direction: track bounding box Y-centroid history
        self._visitor_y_history: dict[str, list[float]] = defaultdict(list)

    # ── Main call from detect.py ──────────────────────────────────────────────

    def process_track(
        self,
        track: dict,
        zone: Optional[str],
        is_staff: bool,
        is_reentry: bool,
        frame_ts: datetime,
        confidence: float,
    ):
        visitor_id = track["visitor_id"]
        bbox       = track["bbox"]
        cy         = (bbox[1] + bbox[3]) / 2

        # Track vertical movement for entry/exit direction
        self._visitor_y_history[visitor_id].append(cy)
        if len(self._visitor_y_history[visitor_id]) > 30:
            self._visitor_y_history[visitor_id].pop(0)

        seq = self._visitor_session_seq[visitor_id]

        # ── REENTRY ──────────────────────────────────────────────────────────
        if is_reentry and visitor_id in self._visitor_exited:
            self._visitor_exited.discard(visitor_id)
            self._visitor_entered.add(visitor_id)
            seq = self._bump_seq(visitor_id)
            self._emit(self._build_event(
                event_type="REENTRY",
                visitor_id=visitor_id,
                zone_id=None,
                dwell_ms=0,
                is_staff=is_staff,
                confidence=confidence,
                frame_ts=frame_ts,
                session_seq=seq,
            ))

        # ── ENTRY ─────────────────────────────────────────────────────────────
        if visitor_id not in self._visitor_entered:
            self._visitor_entered.add(visitor_id)
            seq = self._bump_seq(visitor_id)
            self._emit(self._build_event(
                event_type="ENTRY",
                visitor_id=visitor_id,
                zone_id=None,
                dwell_ms=0,
                is_staff=is_staff,
                confidence=confidence,
                frame_ts=frame_ts,
                session_seq=seq,
            ))

        # ── Zone change ───────────────────────────────────────────────────────
        prev_zone = self._visitor_zone.get(visitor_id)
        if zone != prev_zone:
            # ZONE_EXIT for previous zone
            if prev_zone is not None:
                dwell_ms = self._calc_dwell_ms(visitor_id, frame_ts)
                seq = self._bump_seq(visitor_id)
                self._emit(self._build_event(
                    event_type="ZONE_EXIT",
                    visitor_id=visitor_id,
                    zone_id=prev_zone,
                    dwell_ms=dwell_ms,
                    is_staff=is_staff,
                    confidence=confidence,
                    frame_ts=frame_ts,
                    session_seq=seq,
                ))
                # Check billing abandon
                if prev_zone.upper() in BILLING_ZONE_IDS:
                    self._maybe_emit_abandon(visitor_id, prev_zone, is_staff, confidence, frame_ts)

            # ZONE_ENTER for new zone
            if zone is not None:
                self._visitor_zone[visitor_id] = zone
                self._visitor_dwell_start[visitor_id] = frame_ts
                self._visitor_last_dwell_emit[visitor_id] = frame_ts
                seq = self._bump_seq(visitor_id)
                self._emit(self._build_event(
                    event_type="ZONE_ENTER",
                    visitor_id=visitor_id,
                    zone_id=zone,
                    dwell_ms=0,
                    is_staff=is_staff,
                    confidence=confidence,
                    frame_ts=frame_ts,
                    session_seq=seq,
                ))
                # Billing queue join
                if zone.upper() in BILLING_ZONE_IDS:
                    qdepth = self._billing_queue.get(zone, 0) + 1
                    self._billing_queue[zone] = qdepth
                    if qdepth > 0:
                        seq = self._bump_seq(visitor_id)
                        evt = self._build_event(
                            event_type="BILLING_QUEUE_JOIN",
                            visitor_id=visitor_id,
                            zone_id=zone,
                            dwell_ms=0,
                            is_staff=is_staff,
                            confidence=confidence,
                            frame_ts=frame_ts,
                            session_seq=seq,
                        )
                        evt["metadata"]["queue_depth"] = qdepth
                        self._emit(evt)
            else:
                self._visitor_zone[visitor_id] = None

        # ── ZONE_DWELL (every 30s of continuous presence) ─────────────────────
        if zone is not None:
            last_dwell_emit = self._visitor_last_dwell_emit.get(visitor_id, frame_ts)
            elapsed = (frame_ts - last_dwell_emit).total_seconds()
            if elapsed >= DWELL_EMIT_INTERVAL_SEC:
                dwell_ms = int(elapsed * 1000)
                seq = self._bump_seq(visitor_id)
                self._emit(self._build_event(
                    event_type="ZONE_DWELL",
                    visitor_id=visitor_id,
                    zone_id=zone,
                    dwell_ms=dwell_ms,
                    is_staff=is_staff,
                    confidence=confidence,
                    frame_ts=frame_ts,
                    session_seq=seq,
                ))
                self._visitor_last_dwell_emit[visitor_id] = frame_ts

    def emit_exit(self, visitor_id: str, is_staff: bool, confidence: float, frame_ts: datetime):
        """Call this when a track disappears through the exit threshold."""
        if visitor_id in self._visitor_exited:
            return
        self._visitor_exited.add(visitor_id)
        current_zone = self._visitor_zone.get(visitor_id)
        if current_zone:
            dwell_ms = self._calc_dwell_ms(visitor_id, frame_ts)
            seq = self._bump_seq(visitor_id)
            self._emit(self._build_event(
                event_type="ZONE_EXIT",
                visitor_id=visitor_id,
                zone_id=current_zone,
                dwell_ms=dwell_ms,
                is_staff=is_staff,
                confidence=confidence,
                frame_ts=frame_ts,
                session_seq=seq,
            ))
        seq = self._bump_seq(visitor_id)
        self._emit(self._build_event(
            event_type="EXIT",
            visitor_id=visitor_id,
            zone_id=None,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            frame_ts=frame_ts,
            session_seq=seq,
        ))

    def flush(self):
        """Flush remaining batch to API and close file."""
        if self._batch:
            self._post_batch(self._batch)
            self._batch.clear()
        self._out_file.close()
        log.info("emitter_flushed", total_events=self.event_count)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _build_event(
        self,
        event_type: str,
        visitor_id: str,
        zone_id: Optional[str],
        dwell_ms: int,
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
        session_seq: int,
        metadata: Optional[dict] = None,
    ) -> dict:
        return {
            "event_id":   str(uuid.uuid4()),
            "store_id":   self.store_id,
            "camera_id":  self.camera_id,
            "visitor_id": visitor_id,
            "event_type": event_type,
            "timestamp":  frame_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "zone_id":    zone_id,
            "dwell_ms":   dwell_ms,
            "is_staff":   is_staff,
            "confidence": round(confidence, 4),
            "metadata": {
                "queue_depth": None,
                "sku_zone":    zone_id,
                "session_seq": session_seq,
                **(metadata or {}),
            },
        }

    def _emit(self, event: dict):
        """Write to file + buffer for API batch."""
        self._out_file.write(json.dumps(event) + "\n")
        self._batch.append(event)
        self.event_count += 1

        if len(self._batch) >= self.batch_size:
            self._post_batch(self._batch)
            self._batch.clear()

    def _post_batch(self, batch: list[dict]):
        if not self.api_url:
            return
        url = f"{self.api_url.rstrip('/')}/events/ingest"
        try:
            resp = requests.post(url, json={"events": batch}, timeout=10)
            resp.raise_for_status()
            log.debug("batch_posted", count=len(batch), status=resp.status_code)
        except Exception as exc:
            log.error("batch_post_failed", error=str(exc), batch_size=len(batch))

    def _bump_seq(self, visitor_id: str) -> int:
        self._visitor_session_seq[visitor_id] += 1
        return self._visitor_session_seq[visitor_id]

    def _calc_dwell_ms(self, visitor_id: str, now: datetime) -> int:
        start = self._visitor_dwell_start.get(visitor_id, now)
        return max(0, int((now - start).total_seconds() * 1000))

    def _maybe_emit_abandon(
        self,
        visitor_id: str,
        zone_id: str,
        is_staff: bool,
        confidence: float,
        frame_ts: datetime,
    ):
        """Emit BILLING_QUEUE_ABANDON — the API will later correlate with POS."""
        seq = self._bump_seq(visitor_id)
        self._emit(self._build_event(
            event_type="BILLING_QUEUE_ABANDON",
            visitor_id=visitor_id,
            zone_id=zone_id,
            dwell_ms=0,
            is_staff=is_staff,
            confidence=confidence,
            frame_ts=frame_ts,
            session_seq=seq,
        ))
        depth = self._billing_queue.get(zone_id, 1)
        self._billing_queue[zone_id] = max(0, depth - 1)

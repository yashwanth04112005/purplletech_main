"""
replay.py — Simulated real-time event replay for live dashboard (Part E).

Reads .jsonl event files and POSTs them to the API at a configurable speed
multiplier, preserving original relative timestamps so the dashboard sees
a realistic event stream.

Usage:
    python pipeline/replay.py \
        --events-dir data/events \
        --api-url    http://localhost:8000 \
        --speed      10        # 10x faster than real time
        [--store-id  STORE_BLR_002]   # filter to one store
        [--loop]               # loop continuously
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
import structlog

log = structlog.get_logger()


def parse_args():
    p = argparse.ArgumentParser(description="Real-time event replay for live dashboard")
    p.add_argument("--events-dir", required=True,  help="Directory containing .jsonl event files")
    p.add_argument("--api-url",    required=True,  help="Base API URL, e.g. http://localhost:8000")
    p.add_argument("--speed",      default=10.0,   type=float, help="Replay speed multiplier (default: 10x)")
    p.add_argument("--store-id",   default=None,   help="Filter to a specific store")
    p.add_argument("--batch-size", default=20,     type=int,   help="Events per API batch")
    p.add_argument("--loop",       action="store_true",        help="Loop continuously")
    return p.parse_args()


def load_all_events(events_dir: str, store_filter: str | None) -> list[dict]:
    """Load and sort all events by timestamp."""
    events = []
    for f in Path(events_dir).glob("**/*.jsonl"):
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                    if store_filter and evt.get("store_id") != store_filter:
                        continue
                    events.append(evt)
                except json.JSONDecodeError:
                    continue

    events.sort(key=lambda e: e.get("timestamp", ""))
    log.info("events_loaded", count=len(events))
    return events


def replay(events: list[dict], api_url: str, speed: float, batch_size: int):
    if not events:
        log.warning("no_events_to_replay")
        return

    ingest_url = f"{api_url.rstrip('/')}/events/ingest"
    first_ts   = _parse_ts(events[0]["timestamp"])
    replay_start = time.perf_counter()

    batch = []
    sent  = 0

    for evt in events:
        evt_ts = _parse_ts(evt["timestamp"])

        # How far into the clip this event is
        clip_offset_sec    = (evt_ts - first_ts).total_seconds()
        replay_offset_sec  = clip_offset_sec / speed

        # Wait until it's time to send this event
        elapsed = time.perf_counter() - replay_start
        wait    = replay_offset_sec - elapsed
        if wait > 0:
            time.sleep(wait)

        batch.append(evt)

        if len(batch) >= batch_size:
            _post(batch, ingest_url)
            sent += len(batch)
            batch.clear()
            log.info("replay_progress", sent=sent, total=len(events))

    # Flush
    if batch:
        _post(batch, ingest_url)
        sent += len(batch)

    log.info("replay_complete", total_sent=sent)


def _post(batch: list[dict], url: str):
    try:
        r = requests.post(url, json={"events": batch}, timeout=15)
        r.raise_for_status()
    except Exception as exc:
        log.error("replay_post_failed", error=str(exc), batch_size=len(batch))


def _parse_ts(ts_str: str) -> datetime:
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))


def main():
    args = parse_args()
    events = load_all_events(args.events_dir, args.store_id)

    if not events:
        print("No events found — ensure pipeline/run.sh has been executed first.")
        sys.exit(1)

    print(f"Replaying {len(events)} events at {args.speed}x speed -> {args.api_url}")
    print("Open http://localhost:3000 to see the live dashboard.")
    print("Press Ctrl+C to stop.\n")

    try:
        while True:
            replay(events, args.api_url, args.speed, args.batch_size)
            if not args.loop:
                break
            log.info("replay_loop_restart")
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nReplay stopped.")


if __name__ == "__main__":
    main()

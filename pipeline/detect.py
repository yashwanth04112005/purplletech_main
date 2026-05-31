"""
detect.py — Main CCTV detection + tracking script.
Pipeline: YOLOv8 (detection) + ByteTrack (multi-object tracking) + Re-ID (appearance embeddings)

Usage:
    python pipeline/detect.py \
        --video data/clips/STORE_BLR_002/CAM_ENTRY_01.mp4 \
        --store-id STORE_BLR_002 \
        --camera-id CAM_ENTRY_01 \
        --layout data/store_layout.json \
        --output data/events/entry_events.jsonl \
        [--api-url http://localhost:8000] \
        [--start-time 2026-03-03T14:00:00Z]
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import cv2
import numpy as np
import structlog

from pipeline.tracker import PersonTracker
from pipeline.emit import EventEmitter
from pipeline.staff_detector import StaffDetector
from pipeline.zone_mapper import ZoneMapper

log = structlog.get_logger()


def parse_args():
    p = argparse.ArgumentParser(description="Store Intelligence Detection Pipeline")
    p.add_argument("--video",       required=True,  help="Path to CCTV video clip")
    p.add_argument("--store-id",    required=True,  help="Store identifier (e.g. STORE_BLR_002)")
    p.add_argument("--camera-id",   required=True,  help="Camera identifier (e.g. CAM_ENTRY_01)")
    p.add_argument("--layout",      required=True,  help="Path to store_layout.json")
    p.add_argument("--output",      required=True,  help="Output .jsonl file for events")
    p.add_argument("--api-url",     default=None,   help="If set, POST events to this API")
    p.add_argument("--start-time",  default=None,   help="ISO-8601 clip start timestamp (UTC)")
    p.add_argument("--batch-size",  default=50,     type=int, help="Events batch size for API POST")
    p.add_argument("--conf-thresh", default=0.35,   type=float, help="YOLO detection confidence threshold")
    p.add_argument("--skip-frames", default=2,      type=int,   help="Process every Nth frame (speed vs accuracy)")
    p.add_argument("--device",      default="cpu",  help="'cpu' or 'cuda' or '0'")
    return p.parse_args()


def load_layout(path: str, store_id: str) -> dict:
    with open(path) as f:
        layout = json.load(f)
    # Support both list and dict layouts
    if isinstance(layout, list):
        for s in layout:
            if s.get("store_id") == store_id:
                return s
        log.warning("store_not_in_layout", store_id=store_id)
        return {"zones": [], "open_hours": {}}
    return layout.get(store_id, {"zones": [], "open_hours": {}})


def build_clip_timestamp(start_time_str: str | None, video_path: str) -> datetime:
    """Derive clip start timestamp. Falls back to file mtime if not provided."""
    if start_time_str:
        dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    mtime = Path(video_path).stat().st_mtime
    return datetime.fromtimestamp(mtime, tz=timezone.utc)


def main():
    args = parse_args()

    # ── Load model lazily so import errors are obvious ──────────────────────
    try:
        from ultralytics import YOLO
    except ImportError:
        print("ERROR: ultralytics not installed. Run: pip install ultralytics")
        sys.exit(1)

    log.info("pipeline_start", video=args.video, store_id=args.store_id, camera_id=args.camera_id)

    # ── Initialise components ─────────────────────────────────────────────────
    layout   = load_layout(args.layout, args.store_id)
    clip_ts  = build_clip_timestamp(args.start_time, args.video)
    model    = YOLO("yolov8m.pt")               # medium model — best speed/accuracy trade-off
    tracker  = PersonTracker(reid_enabled=True)
    emitter  = EventEmitter(
        store_id=args.store_id,
        camera_id=args.camera_id,
        clip_start=clip_ts,
        output_path=args.output,
        api_url=args.api_url,
        batch_size=args.batch_size,
    )
    staff_det = StaffDetector()
    zone_map  = ZoneMapper(layout, args.camera_id)

    # ── Open video ──────────────────────────────────────────────────────────
    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        log.error("cannot_open_video", path=args.video)
        sys.exit(1)

    fps       = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_f   = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    frame_idx = 0
    t_start   = time.perf_counter()

    log.info("video_opened", fps=fps, total_frames=total_f, clip_start=clip_ts.isoformat())

    # ── Main processing loop ─────────────────────────────────────────────────
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame_idx += 1

        # Skip frames for performance (still track every frame via interpolation)
        if frame_idx % (args.skip_frames + 1) != 0:
            continue

        # Frame timestamp (clip_start + elapsed video time)
        elapsed_sec = frame_idx / fps
        frame_ts    = clip_ts + timedelta(seconds=elapsed_sec)

        # ── YOLOv8 detection (person class = 0) ────────────────────────────
        results = model.predict(
            frame,
            classes=[0],              # person only
            conf=args.conf_thresh,
            verbose=False,
            device=args.device,
        )

        detections = []
        for r in results:
            for box in r.boxes:
                x1, y1, x2, y2 = map(int, box.xyxy[0].tolist())
                conf = float(box.conf[0])
                detections.append({
                    "bbox":       [x1, y1, x2, y2],
                    "confidence": conf,
                    "frame":      frame[y1:y2, x1:x2],   # crop for Re-ID
                })

        # ── Update tracker ─────────────────────────────────────────────────
        tracks = tracker.update(detections, frame, frame_ts)

        # ── Process each active track ───────────────────────────────────────
        for track in tracks:
            visitor_id  = track["visitor_id"]
            bbox        = track["bbox"]
            confidence  = track["confidence"]
            is_reentry  = track.get("is_reentry", False)

            # Staff detection via colour histogram heuristic
            crop = frame[bbox[1]:bbox[3], bbox[0]:bbox[2]]
            is_staff = staff_det.is_staff(crop, track.get("zone_history", []))

            # Zone classification from bounding box centroid
            cx = (bbox[0] + bbox[2]) // 2
            cy = (bbox[1] + bbox[3]) // 2
            zone = zone_map.classify(cx, cy)

            # ── Emit events based on track state ──────────────────────────
            emitter.process_track(
                track=track,
                zone=zone,
                is_staff=is_staff,
                is_reentry=is_reentry,
                frame_ts=frame_ts,
                confidence=confidence,
            )

        # ── Progress log every 300 frames (20s at 15fps) ───────────────────
        if frame_idx % 300 == 0:
            pct = (frame_idx / total_f * 100) if total_f else 0
            elapsed = time.perf_counter() - t_start
            log.info("pipeline_progress", frame=frame_idx, pct=f"{pct:.1f}%", elapsed_s=f"{elapsed:.1f}")

    # ── Flush remaining events ───────────────────────────────────────────────
    cap.release()
    emitter.flush()

    elapsed_total = time.perf_counter() - t_start
    log.info(
        "pipeline_complete",
        frames_processed=frame_idx,
        events_emitted=emitter.event_count,
        elapsed_s=f"{elapsed_total:.1f}",
        store_id=args.store_id,
        camera_id=args.camera_id,
    )


if __name__ == "__main__":
    main()

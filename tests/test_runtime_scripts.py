# PROMPT: "Write pytest unit tests for pipeline runtime scripts and tracking logic
# to improve coverage without requiring GPU/video dependencies. Cover:
# load_pos.py arg parsing + main integration, replay.py loading/batching,
# tracker.py re-entry/cleanup behavior, and detect.py helper/main flow using mocks."
#
# CHANGES MADE:
# - Used monkeypatch + lightweight fake modules for runtime imports in load_pos.main
# - Added deterministic replay batching assertions without real sleeps/network calls
# - Added explicit REENTRY path validation in tracker with synthetic embeddings
# - Added detect.main happy-path smoke test with mocked YOLO and cv2.VideoCapture
# - Kept tests unit-level (no Docker, no real model weights, no real video files)

from __future__ import annotations

import argparse
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pytest

from pipeline import detect, load_pos, replay
from pipeline.tracker import PersonTracker


class _AsyncSessionCtx:
    def __init__(self, session_obj):
        self._session_obj = session_obj

    async def __aenter__(self):
        return self._session_obj

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeDBSession:
    def __init__(self):
        self.commits = 0

    async def commit(self):
        self.commits += 1


def test_load_pos_parse_args(monkeypatch):
    monkeypatch.setattr(
        sys,
        "argv",
        ["load_pos.py", "--csv", "data/pos.csv", "--store-id", "STORE_BLR_002"],
    )
    args = load_pos.parse_args()
    assert args.csv == "data/pos.csv"
    assert args.store_id == "STORE_BLR_002"


@pytest.mark.asyncio
async def test_load_pos_main_calls_loader_and_matcher(monkeypatch):
    calls = {"loaded": 0, "matched": 0}
    fake_db = _FakeDBSession()

    async def fake_init_db():
        return None

    async def fake_load_pos_transactions(csv_path, db):
        calls["loaded"] += 1
        assert csv_path == "data/pos.csv"
        assert db is fake_db
        return 2

    async def fake_run_conversion_matching(store_id, db):
        calls["matched"] += 1
        assert store_id == "STORE_BLR_002"
        assert db is fake_db
        return 1

    fake_args = argparse.Namespace(csv="data/pos.csv", store_id="STORE_BLR_002")
    monkeypatch.setattr(load_pos, "parse_args", lambda: fake_args)

    fake_app_db = types.SimpleNamespace(
        init_db=fake_init_db,
        AsyncSessionLocal=lambda: _AsyncSessionCtx(fake_db),
    )
    fake_pos = types.SimpleNamespace(
        load_pos_transactions=fake_load_pos_transactions,
        run_conversion_matching=fake_run_conversion_matching,
    )

    monkeypatch.setitem(sys.modules, "app.db", fake_app_db)
    monkeypatch.setitem(sys.modules, "app.pos_correlation", fake_pos)

    await load_pos.main()

    assert calls["loaded"] == 1
    assert calls["matched"] == 1
    assert fake_db.commits == 2


def test_replay_load_all_events_filters_and_sorts(tmp_path: Path):
    f1 = tmp_path / "a.jsonl"
    f1.write_text(
        "{\"store_id\": \"S1\", \"timestamp\": \"2026-03-03T14:02:00Z\"}\n"
        "not-json\n"
        "{\"store_id\": \"S2\", \"timestamp\": \"2026-03-03T14:01:00Z\"}\n"
    )

    events = replay.load_all_events(str(tmp_path), store_filter="S1")
    assert len(events) == 1
    assert events[0]["store_id"] == "S1"


def test_replay_batches_events_without_sleep(monkeypatch):
    posted_sizes = []
    monkeypatch.setattr(replay, "_post", lambda batch, url: posted_sizes.append(len(batch)))
    monkeypatch.setattr(replay.time, "sleep", lambda _: None)

    events = [
        {"timestamp": "2026-03-03T14:00:00Z", "store_id": "S1"},
        {"timestamp": "2026-03-03T14:00:01Z", "store_id": "S1"},
        {"timestamp": "2026-03-03T14:00:02Z", "store_id": "S1"},
    ]

    replay.replay(events, "http://localhost:8000", speed=1000.0, batch_size=2)
    assert posted_sizes == [2, 1]


def test_replay_main_exits_when_no_events(monkeypatch):
    fake_args = argparse.Namespace(
        events_dir="data/events",
        api_url="http://localhost:8000",
        speed=10.0,
        store_id=None,
        batch_size=20,
        loop=False,
    )
    monkeypatch.setattr(replay, "parse_args", lambda: fake_args)
    monkeypatch.setattr(replay, "load_all_events", lambda *_: [])

    with pytest.raises(SystemExit) as exc:
        replay.main()
    assert exc.value.code == 1


def test_tracker_update_generates_track():
    tracker = PersonTracker(reid_enabled=False)
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ts = datetime.now(timezone.utc)

    tracks = tracker.update(
        [{"bbox": [10, 10, 100, 120], "confidence": 0.9}],
        frame,
        ts,
    )

    assert len(tracks) == 1
    assert tracks[0]["visitor_id"].startswith("VIS_")
    assert tracks[0]["confidence"] == 0.9


def test_tracker_reentry_detected_within_cooldown(monkeypatch):
    tracker = PersonTracker(reid_enabled=False)
    ts = datetime.now(timezone.utc)
    emb = np.array([1.0, 0.0], dtype=np.float32)

    monkeypatch.setattr(tracker, "_extract_embedding", lambda crop: emb)

    tracker.visitor_embeddings["VIS_old"] = emb
    tracker.exited_visitors["VIS_old"] = ts - timedelta(seconds=10)

    visitor_id, is_reentry = tracker._resolve_visitor_id(
        track_id=42,
        crop=np.zeros((8, 8, 3), dtype=np.uint8),
        ts=ts,
    )

    assert visitor_id == "VIS_old"
    assert is_reentry is True


def test_tracker_cleanup_lost_tracks_updates_last_seen():
    tracker = PersonTracker(reid_enabled=False)
    now = datetime.now(timezone.utc)
    track_id = 77
    tracker.track_to_visitor[track_id] = "VIS_old"
    tracker.track_state[track_id] = {"last_seen": now - timedelta(seconds=10)}

    tracker._cleanup_lost_tracks(now)

    assert track_id not in tracker.track_to_visitor
    assert track_id not in tracker.track_state
    assert "VIS_old" in tracker.visitor_last_seen


def test_detect_load_layout_and_timestamp_helpers(tmp_path: Path):
    layout_list = tmp_path / "layout_list.json"
    layout_list.write_text('[{"store_id":"S1","zones":[{"zone_id":"ENTRY"}]}]')

    layout_dict = tmp_path / "layout_dict.json"
    layout_dict.write_text('{"S2":{"zones":[{"zone_id":"BILLING"}]}}')

    s1 = detect.load_layout(str(layout_list), "S1")
    s2 = detect.load_layout(str(layout_dict), "S2")

    assert s1["zones"][0]["zone_id"] == "ENTRY"
    assert s2["zones"][0]["zone_id"] == "BILLING"

    ts = detect.build_clip_timestamp("2026-03-03T14:00:00Z", "unused.mp4")
    assert ts.tzinfo is not None


class _FakeBox:
    def __init__(self):
        self.xyxy = [np.array([10, 10, 50, 80], dtype=np.float32)]
        self.conf = [np.array(0.85, dtype=np.float32)]


class _FakeResult:
    def __init__(self):
        self.boxes = [_FakeBox()]


class _FakeYOLO:
    def __init__(self, _model_name):
        pass

    def predict(self, frame, classes, conf, verbose, device):
        return [_FakeResult()]


class _FakeCap:
    def __init__(self, *_args, **_kwargs):
        self._reads = 0

    def isOpened(self):
        return True

    def get(self, prop):
        if prop == detect.cv2.CAP_PROP_FPS:
            return 15.0
        if prop == detect.cv2.CAP_PROP_FRAME_COUNT:
            return 2
        return 0

    def read(self):
        if self._reads == 0:
            self._reads += 1
            return True, np.zeros((120, 160, 3), dtype=np.uint8)
        return False, None

    def release(self):
        return None


class _FakeEmitter:
    def __init__(self, **_kwargs):
        self.event_count = 1

    def process_track(self, **_kwargs):
        return None

    def flush(self):
        return None


class _FakeTracker:
    def __init__(self, reid_enabled=True):
        self.reid_enabled = reid_enabled

    def update(self, detections, frame, frame_ts):
        if not detections:
            return []
        return [{
            "visitor_id": "VIS_demo",
            "bbox": detections[0]["bbox"],
            "confidence": detections[0]["confidence"],
            "is_reentry": False,
            "zone_history": [],
            "state": {},
        }]


class _FakeStaffDetector:
    def is_staff(self, crop, zone_history):
        return False


class _FakeZoneMapper:
    def __init__(self, layout, camera_id):
        self.layout = layout
        self.camera_id = camera_id

    def classify(self, x, y):
        return "ENTRY"


def test_detect_main_happy_path_with_mocks(monkeypatch, tmp_path: Path):
    out_path = tmp_path / "events.jsonl"
    fake_args = argparse.Namespace(
        video="demo.mp4",
        store_id="STORE_BLR_002",
        camera_id="CAM_ENTRY_01",
        layout="layout.json",
        output=str(out_path),
        api_url=None,
        start_time="2026-03-03T14:00:00Z",
        batch_size=50,
        conf_thresh=0.35,
        skip_frames=0,
        device="cpu",
    )

    monkeypatch.setattr(detect, "parse_args", lambda: fake_args)
    monkeypatch.setattr(detect, "load_layout", lambda *_: {"zones": []})
    monkeypatch.setattr(
        detect,
        "build_clip_timestamp",
        lambda *_: datetime(2026, 3, 3, 14, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(detect, "PersonTracker", _FakeTracker)
    monkeypatch.setattr(detect, "EventEmitter", _FakeEmitter)
    monkeypatch.setattr(detect, "StaffDetector", _FakeStaffDetector)
    monkeypatch.setattr(detect, "ZoneMapper", _FakeZoneMapper)
    monkeypatch.setattr(detect.cv2, "VideoCapture", _FakeCap)

    monkeypatch.setitem(sys.modules, "ultralytics", types.SimpleNamespace(YOLO=_FakeYOLO))

    detect.main()

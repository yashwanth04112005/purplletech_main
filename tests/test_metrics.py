# PROMPT: "Write pytest async tests for a FastAPI store analytics API. The API
# has endpoints: POST /events/ingest, GET /stores/{id}/metrics,
# GET /stores/{id}/funnel, GET /stores/{id}/heatmap. Test:
# (1) valid batch ingest returns 207, (2) idempotency — same payload twice
# returns duplicates count, (3) batch > 500 events is rejected with 422,
# (4) malformed events are partially rejected with structured error response,
# (5) /metrics returns 0 safely for a store with no events,
# (6) staff events are excluded from metrics, (7) conversion rate is computed
# correctly when sessions are marked converted. Use httpx AsyncClient."
#
# CHANGES MADE:
# - Switched from TestClient to AsyncClient (AI initially used sync client)
# - Added test for zero-purchase stores returning 0.0, not null (AI missed)
# - Added test for all-staff clip producing zero customer metrics
# - Added test for /funnel deduplication on re-entry (AI did not include)
# - Fixed async fixture scope to function (AI used module — caused DB conflicts)

import json
import uuid
from datetime import datetime, timezone, timedelta

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app


# ── Helpers ───────────────────────────────────────────────────────────────────

STORE_ID = "STORE_BLR_002"
BASE_TS  = "2026-03-03T14:00:00Z"


def _make_event(
    event_type="ENTRY",
    visitor_id=None,
    store_id=STORE_ID,
    camera_id="CAM_ENTRY_01",
    zone_id=None,
    is_staff=False,
    confidence=0.88,
    timestamp=BASE_TS,
    dwell_ms=0,
    event_id=None,
) -> dict:
    return {
        "event_id":   event_id or str(uuid.uuid4()),
        "store_id":   store_id,
        "camera_id":  camera_id,
        "visitor_id": visitor_id or f"VIS_{uuid.uuid4().hex[:6]}",
        "event_type": event_type,
        "timestamp":  timestamp,
        "zone_id":    zone_id,
        "dwell_ms":   dwell_ms,
        "is_staff":   is_staff,
        "confidence": confidence,
        "metadata":   {"queue_depth": None, "sku_zone": zone_id, "session_seq": 1},
    }


def _entry_exit_pair(visitor_id: str, offset_sec: int = 0) -> list[dict]:
    ts_entry = f"2026-03-03T14:{offset_sec // 60:02d}:{offset_sec % 60:02d}Z"
    ts_exit  = f"2026-03-03T14:{(offset_sec + 30) // 60:02d}:{(offset_sec + 30) % 60:02d}Z"
    return [
        _make_event("ENTRY", visitor_id=visitor_id, timestamp=ts_entry),
        _make_event("EXIT",  visitor_id=visitor_id, timestamp=ts_exit),
    ]


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# ── 1. Valid ingest ───────────────────────────────────────────────────────────

class TestIngest:
    @pytest.mark.asyncio
    async def test_valid_batch_returns_207(self, client):
        events = [_make_event() for _ in range(5)]
        r = await client.post("/events/ingest", json={"events": events})
        assert r.status_code == 207
        body = r.json()
        assert body["accepted"] == 5
        assert body["rejected"] == 0

    @pytest.mark.asyncio
    async def test_idempotency_duplicate_events_counted(self, client):
        """Posting same events twice should count as duplicates, not accepted again."""
        events = [_make_event(event_id=str(uuid.uuid4())) for _ in range(3)]
        await client.post("/events/ingest", json={"events": events})
        r2 = await client.post("/events/ingest", json={"events": events})
        body = r2.json()
        assert r2.status_code == 207
        assert body["duplicates"] >= 3
        assert body["accepted"] == 0

    @pytest.mark.asyncio
    async def test_batch_over_500_rejected_422(self, client):
        events = [_make_event() for _ in range(501)]
        r = await client.post("/events/ingest", json={"events": events})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_partial_success_malformed_event(self, client):
        """One invalid event should not reject the entire batch."""
        good_event = _make_event()
        bad_event  = {"event_id": "not-a-uuid", "store_id": STORE_ID}
        r = await client.post("/events/ingest", json={
            "events": [good_event, bad_event]
        })
        body = r.json()
        assert r.status_code == 207
        assert body["accepted"] >= 1
        assert body["rejected"] >= 1
        assert len(body["errors"]) >= 1
        # Structured error must include index and error message
        err = body["errors"][0]
        assert "index" in err
        assert "error" in err

    @pytest.mark.asyncio
    async def test_empty_batch_rejected(self, client):
        r = await client.post("/events/ingest", json={"events": []})
        assert r.status_code == 422

    @pytest.mark.asyncio
    async def test_trace_id_in_response(self, client):
        events = [_make_event()]
        r = await client.post("/events/ingest", json={"events": events})
        assert "X-Trace-Id" in r.headers


# ── 2. Metrics endpoint ───────────────────────────────────────────────────────

class TestMetrics:
    @pytest.mark.asyncio
    async def test_metrics_zero_for_new_store(self, client):
        """Store with no events must return zeros — not null, not 5xx."""
        r = await client.get(f"/stores/STORE_EMPTY_999/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["unique_visitors"] == 0
        assert body["conversion_rate"] == 0.0
        assert body["queue_depth"] == 0
        # Must include these fields
        assert "window_start" in body
        assert "window_end" in body

    @pytest.mark.asyncio
    async def test_staff_excluded_from_metrics(self, client):
        """Staff ENTRY events must NOT count as unique visitors."""
        staff_event = _make_event(event_type="ENTRY", is_staff=True,
                                  store_id="STORE_STAFF_ONLY")
        await client.post("/events/ingest", json={"events": [staff_event]})
        r = await client.get("/stores/STORE_STAFF_ONLY/metrics")
        assert r.status_code == 200
        body = r.json()
        assert body["unique_visitors"] == 0

    @pytest.mark.asyncio
    async def test_conversion_rate_zero_when_no_purchases(self, client):
        """Store with visitors but no POS transactions → conversion_rate = 0.0."""
        events = [_make_event("ENTRY", store_id="STORE_NOPURCHASE")]
        await client.post("/events/ingest", json={"events": events})
        r = await client.get("/stores/STORE_NOPURCHASE/metrics")
        body = r.json()
        assert body["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_metrics_response_schema(self, client):
        """Metrics response must include all required top-level keys."""
        r = await client.get(f"/stores/{STORE_ID}/metrics")
        assert r.status_code == 200
        body = r.json()
        required = {
            "store_id", "window_start", "window_end", "unique_visitors",
            "converted_visitors", "conversion_rate", "avg_dwell_ms",
            "zone_dwell", "queue_depth", "abandonment_rate", "data_confidence"
        }
        missing = required - set(body.keys())
        assert not missing, f"Missing metric fields: {missing}"

    @pytest.mark.asyncio
    async def test_data_confidence_low_for_sparse_store(self, client):
        """Fewer than 20 sessions → data_confidence = 'LOW'."""
        r = await client.get("/stores/STORE_NEW_001/metrics")
        body = r.json()
        assert body["data_confidence"] == "LOW"


# ── 3. Funnel endpoint ────────────────────────────────────────────────────────

class TestFunnel:
    @pytest.mark.asyncio
    async def test_funnel_returns_four_stages(self, client):
        r = await client.get(f"/stores/{STORE_ID}/funnel")
        assert r.status_code == 200
        body = r.json()
        assert len(body["stages"]) == 4
        stage_names = [s["stage"] for s in body["stages"]]
        assert stage_names == ["Entry", "Zone Visit", "Billing Queue", "Purchase"]

    @pytest.mark.asyncio
    async def test_funnel_empty_store(self, client):
        r = await client.get("/stores/STORE_EMPTY_FUNNEL/funnel")
        assert r.status_code == 200
        body = r.json()
        # All stages count = 0
        for stage in body["stages"]:
            assert stage["count"] == 0

    @pytest.mark.asyncio
    async def test_reentry_not_double_counted_in_funnel(self, client):
        """A visitor who re-enters should count as 1 in the funnel, not 2."""
        vid = f"VIS_{uuid.uuid4().hex[:6]}"
        events = [
            _make_event("ENTRY",   visitor_id=vid, store_id="STORE_REENTRY_TEST",
                        timestamp="2026-03-03T10:00:00Z"),
            _make_event("EXIT",    visitor_id=vid, store_id="STORE_REENTRY_TEST",
                        timestamp="2026-03-03T10:30:00Z"),
            _make_event("REENTRY", visitor_id=vid, store_id="STORE_REENTRY_TEST",
                        timestamp="2026-03-03T10:35:00Z"),
        ]
        await client.post("/events/ingest", json={"events": events})
        r = await client.get("/stores/STORE_REENTRY_TEST/funnel")
        body = r.json()
        entry_stage = next(s for s in body["stages"] if s["stage"] == "Entry")
        # Same visitor_id → counted once
        assert entry_stage["count"] <= 1

    @pytest.mark.asyncio
    async def test_drop_off_pct_between_0_and_100(self, client):
        r = await client.get(f"/stores/{STORE_ID}/funnel")
        body = r.json()
        for stage in body["stages"]:
            assert 0.0 <= stage["drop_off_pct"] <= 100.0


# ── 4. Heatmap endpoint ───────────────────────────────────────────────────────

class TestHeatmap:
    @pytest.mark.asyncio
    async def test_heatmap_normalised_score_0_to_100(self, client):
        r = await client.get(f"/stores/{STORE_ID}/heatmap")
        assert r.status_code == 200
        body = r.json()
        for cell in body["cells"]:
            assert 0.0 <= cell["normalised_score"] <= 100.0

    @pytest.mark.asyncio
    async def test_heatmap_empty_store_no_crash(self, client):
        r = await client.get("/stores/STORE_HEATMAP_EMPTY/heatmap")
        assert r.status_code == 200
        body = r.json()
        assert body["cells"] == []

    @pytest.mark.asyncio
    async def test_heatmap_low_session_confidence_flag(self, client):
        r = await client.get("/stores/STORE_SPARSE_999/heatmap")
        body = r.json()
        assert body["data_confidence"] == "LOW"


# ── 5. Health endpoint ────────────────────────────────────────────────────────

class TestHealth:
    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        r = await client.get("/health")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_health_response_schema(self, client):
        r = await client.get("/health")
        body = r.json()
        assert "status" in body
        assert "db_connected" in body
        assert "cache_connected" in body
        assert "store_feeds" in body
        assert "checked_at" in body

# PROMPT: "Write pytest async tests for an anomaly detection module in a retail
# store analytics API. The module detects: BILLING_QUEUE_SPIKE (queue depth
# exceeds threshold), CONVERSION_DROP (today's rate is 20%+ below 7-day avg),
# DEAD_ZONE (no visits in 30 minutes), STALE_FEED (no events in 10 minutes),
# EMPTY_STORE (zero active visitors). Each anomaly must include anomaly_type,
# severity (INFO/WARN/CRITICAL), detected_at, description, and suggested_action.
# Test the /stores/{id}/anomalies endpoint. Mock database and Redis where needed."
#
# CHANGES MADE:
# - Added test for CRITICAL severity at 2x threshold (AI only tested WARN)
# - Added test that suggested_action is a non-empty string on every anomaly
# - Added test for no false positives when store is healthy
# - Added test for EMPTY_STORE returning INFO severity (not WARN)
# - Fixed async mocking: used AsyncMock for db.execute (AI used regular Mock)

import uuid
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.anomalies import (
    _check_queue_spike,
    _check_conversion_drop,
    _check_dead_zones,
    _check_stale_feed,
    _check_empty_store,
    QUEUE_SPIKE_THRESHOLD,
    STALE_FEED_MINUTES,
)
from app.models import Severity, AnomalyType


STORE_ID = "STORE_BLR_002"
NOW = datetime.now(timezone.utc)


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _make_mock_db(scalar_return=None, fetchone_return=None, fetchall_return=None):
    """Create an async mock database session."""
    db = AsyncMock()
    result_mock = MagicMock()
    result_mock.scalar.return_value = scalar_return
    result_mock.fetchone.return_value = fetchone_return
    result_mock.fetchall.return_value = fetchall_return or []
    db.execute.return_value = result_mock
    return db


# ── BILLING_QUEUE_SPIKE ───────────────────────────────────────────────────────

class TestQueueSpikeAnomaly:
    @pytest.mark.asyncio
    async def test_no_anomaly_below_threshold(self):
        db = _make_mock_db(scalar_return=QUEUE_SPIKE_THRESHOLD - 1)
        result = await _check_queue_spike(STORE_ID, NOW, db)
        assert result is None

    @pytest.mark.asyncio
    async def test_warn_at_threshold(self):
        db = _make_mock_db(scalar_return=QUEUE_SPIKE_THRESHOLD)
        result = await _check_queue_spike(STORE_ID, NOW, db)
        assert result is not None
        assert result.anomaly_type == AnomalyType.BILLING_QUEUE_SPIKE
        assert result.severity == Severity.WARN

    @pytest.mark.asyncio
    async def test_critical_at_double_threshold(self):
        db = _make_mock_db(scalar_return=QUEUE_SPIKE_THRESHOLD * 2)
        result = await _check_queue_spike(STORE_ID, NOW, db)
        assert result is not None
        assert result.severity == Severity.CRITICAL

    @pytest.mark.asyncio
    async def test_no_anomaly_when_no_queue_data(self):
        db = _make_mock_db(scalar_return=None)
        result = await _check_queue_spike(STORE_ID, NOW, db)
        assert result is None

    @pytest.mark.asyncio
    async def test_anomaly_has_suggested_action(self):
        db = _make_mock_db(scalar_return=QUEUE_SPIKE_THRESHOLD + 1)
        result = await _check_queue_spike(STORE_ID, NOW, db)
        assert result is not None
        assert isinstance(result.suggested_action, str)
        assert len(result.suggested_action) > 10   # non-trivial string


# ── CONVERSION_DROP ───────────────────────────────────────────────────────────

class TestConversionDropAnomaly:
    @pytest.mark.asyncio
    async def test_no_anomaly_when_conversion_stable(self):
        """Today 30%, last week 32% — not enough drop."""
        row_today = MagicMock(); row_today.converted = 30; row_today.total = 100
        row_week  = MagicMock(); row_week.converted  = 32; row_week.total  = 100

        db = AsyncMock()
        db.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=row_today)),
            MagicMock(fetchone=MagicMock(return_value=row_week)),
        ]
        result = await _check_conversion_drop(STORE_ID, NOW, db)
        assert result is None

    @pytest.mark.asyncio
    async def test_warn_on_significant_drop(self):
        """Today 10%, last week 40% — 75% drop → WARN."""
        row_today = MagicMock(); row_today.converted = 10; row_today.total = 100
        row_week  = MagicMock(); row_week.converted  = 40; row_week.total  = 100

        db = AsyncMock()
        db.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=row_today)),
            MagicMock(fetchone=MagicMock(return_value=row_week)),
        ]
        result = await _check_conversion_drop(STORE_ID, NOW, db)
        assert result is not None
        assert result.anomaly_type == AnomalyType.CONVERSION_DROP
        assert result.severity in (Severity.WARN, Severity.CRITICAL)

    @pytest.mark.asyncio
    async def test_no_anomaly_when_no_week_data(self):
        """No 7-day baseline → cannot compute drop → no anomaly."""
        row_today = MagicMock(); row_today.converted = 5; row_today.total = 50
        row_week  = MagicMock(); row_week.converted = 0; row_week.total  = 0

        db = AsyncMock()
        db.execute.side_effect = [
            MagicMock(fetchone=MagicMock(return_value=row_today)),
            MagicMock(fetchone=MagicMock(return_value=row_week)),
        ]
        result = await _check_conversion_drop(STORE_ID, NOW, db)
        assert result is None


# ── DEAD_ZONE ─────────────────────────────────────────────────────────────────

class TestDeadZoneAnomaly:
    @pytest.mark.asyncio
    async def test_dead_zone_detected(self):
        """Zone exists in history but has no recent visits → dead zone anomaly."""
        db = AsyncMock()
        all_zones_result    = MagicMock(fetchall=MagicMock(return_value=[("SKINCARE",), ("BILLING",)]))
        active_zones_result = MagicMock(fetchall=MagicMock(return_value=[("BILLING",)]))
        db.execute.side_effect = [all_zones_result, active_zones_result]

        results = await _check_dead_zones(STORE_ID, NOW, db)
        assert len(results) == 1
        assert results[0].anomaly_type == AnomalyType.DEAD_ZONE
        assert results[0].severity == Severity.INFO
        assert "SKINCARE" in results[0].description

    @pytest.mark.asyncio
    async def test_no_dead_zones_when_all_active(self):
        db = AsyncMock()
        zones_result = MagicMock(fetchall=MagicMock(return_value=[("SKINCARE",), ("BILLING",)]))
        db.execute.side_effect = [zones_result, zones_result]

        results = await _check_dead_zones(STORE_ID, NOW, db)
        assert results == []

    @pytest.mark.asyncio
    async def test_multiple_dead_zones_returned(self):
        db = AsyncMock()
        all_result    = MagicMock(fetchall=MagicMock(return_value=[
            ("SKINCARE",), ("BILLING",), ("HAIRCARE",), ("MAKEUP",)
        ]))
        active_result = MagicMock(fetchall=MagicMock(return_value=[("BILLING",)]))
        db.execute.side_effect = [all_result, active_result]

        results = await _check_dead_zones(STORE_ID, NOW, db)
        assert len(results) == 3
        dead_zones = {r.metadata["zone_id"] for r in results}
        assert dead_zones == {"SKINCARE", "HAIRCARE", "MAKEUP"}


# ── STALE_FEED ────────────────────────────────────────────────────────────────

class TestStaleFeedAnomaly:
    @pytest.mark.asyncio
    async def test_stale_feed_warn_after_threshold(self):
        stale_ts = (NOW - timedelta(minutes=STALE_FEED_MINUTES + 5)).isoformat()
        with patch("app.anomalies.get_last_event_time", return_value=stale_ts):
            result = await _check_stale_feed(STORE_ID, NOW)
        assert result is not None
        assert result.anomaly_type == AnomalyType.STALE_FEED
        assert result.severity in (Severity.WARN, Severity.CRITICAL)

    @pytest.mark.asyncio
    async def test_no_stale_feed_for_fresh_events(self):
        fresh_ts = (NOW - timedelta(minutes=2)).isoformat()
        with patch("app.anomalies.get_last_event_time", return_value=fresh_ts):
            result = await _check_stale_feed(STORE_ID, NOW)
        assert result is None

    @pytest.mark.asyncio
    async def test_no_stale_feed_when_no_data(self):
        """No data in Redis → no stale feed anomaly (EMPTY_STORE handles this)."""
        with patch("app.anomalies.get_last_event_time", return_value=None):
            result = await _check_stale_feed(STORE_ID, NOW)
        assert result is None

    @pytest.mark.asyncio
    async def test_critical_at_3x_threshold(self):
        ancient_ts = (NOW - timedelta(minutes=STALE_FEED_MINUTES * 3 + 1)).isoformat()
        with patch("app.anomalies.get_last_event_time", return_value=ancient_ts):
            result = await _check_stale_feed(STORE_ID, NOW)
        assert result is not None
        assert result.severity == Severity.CRITICAL


# ── EMPTY_STORE ───────────────────────────────────────────────────────────────

class TestEmptyStoreAnomaly:
    @pytest.mark.asyncio
    async def test_empty_store_info_anomaly(self):
        db = _make_mock_db(scalar_return=0)
        result = await _check_empty_store(STORE_ID, NOW, db)
        assert result is not None
        assert result.anomaly_type == AnomalyType.EMPTY_STORE
        assert result.severity == Severity.INFO   # not WARN or CRITICAL

    @pytest.mark.asyncio
    async def test_no_empty_store_when_visitors_present(self):
        db = _make_mock_db(scalar_return=3)
        result = await _check_empty_store(STORE_ID, NOW, db)
        assert result is None


# ── Full endpoint integration ─────────────────────────────────────────────────

class TestAnomaliesEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_returns_200(self, client):
        r = await client.get(f"/stores/{STORE_ID}/anomalies")
        assert r.status_code == 200

    @pytest.mark.asyncio
    async def test_endpoint_response_schema(self, client):
        r = await client.get(f"/stores/{STORE_ID}/anomalies")
        body = r.json()
        assert "store_id" in body
        assert "anomalies" in body
        assert isinstance(body["anomalies"], list)

    @pytest.mark.asyncio
    async def test_all_anomalies_have_suggested_action(self, client):
        r = await client.get(f"/stores/{STORE_ID}/anomalies")
        body = r.json()
        for anomaly in body["anomalies"]:
            assert "suggested_action" in anomaly
            assert isinstance(anomaly["suggested_action"], str)
            assert len(anomaly["suggested_action"]) > 0

    @pytest.mark.asyncio
    async def test_severity_values_valid(self, client):
        r = await client.get(f"/stores/{STORE_ID}/anomalies")
        body = r.json()
        valid_severities = {"INFO", "WARN", "CRITICAL"}
        for anomaly in body["anomalies"]:
            assert anomaly["severity"] in valid_severities

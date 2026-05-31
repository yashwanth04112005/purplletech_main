"""
conftest.py — Shared pytest fixtures and test database setup.

Uses an in-memory SQLite DB (via aiosqlite) for unit tests so no
PostgreSQL instance is required when running tests locally.
Redis is mocked via unittest.mock for isolation.
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator
from unittest.mock import AsyncMock, patch, MagicMock

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# ── In-memory test database ──────────────────────────────────────────────────

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(TEST_DB_URL, echo=False)
TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    expire_on_commit=False,
    class_=AsyncSession,
)

CREATE_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT    NOT NULL UNIQUE,
    store_id    TEXT    NOT NULL,
    camera_id   TEXT    NOT NULL,
    visitor_id  TEXT    NOT NULL,
    event_type  TEXT    NOT NULL,
    timestamp   TEXT    NOT NULL,
    zone_id     TEXT,
    dwell_ms    INTEGER NOT NULL DEFAULT 0,
    is_staff    INTEGER NOT NULL DEFAULT 0,
    confidence  REAL    NOT NULL,
    queue_depth INTEGER,
    sku_zone    TEXT,
    session_seq INTEGER,
    raw_payload TEXT    NOT NULL,
    ingested_at TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS visitor_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT    NOT NULL DEFAULT (lower(hex(randomblob(16)))),
    store_id        TEXT    NOT NULL,
    visitor_id      TEXT    NOT NULL,
    entry_time      TEXT,
    exit_time       TEXT,
    is_converted    INTEGER NOT NULL DEFAULT 0,
    transaction_id  TEXT,
    zones_visited   TEXT    DEFAULT '[]',
    was_in_billing  INTEGER NOT NULL DEFAULT 0,
    reentry_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS pos_transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    store_id        TEXT    NOT NULL,
    transaction_id  TEXT    NOT NULL UNIQUE,
    timestamp       TEXT    NOT NULL,
    basket_value    REAL,
    matched_session TEXT
);
CREATE TABLE IF NOT EXISTS anomalies (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    anomaly_id       TEXT    NOT NULL DEFAULT (lower(hex(randomblob(16)))),
    store_id         TEXT    NOT NULL,
    anomaly_type     TEXT    NOT NULL,
    severity         TEXT    NOT NULL,
    detected_at      TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at      TEXT,
    description      TEXT,
    suggested_action TEXT,
    metadata         TEXT
);
"""


@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Create all tables in the in-memory test DB once per session."""
    from sqlalchemy import text
    async with test_engine.begin() as conn:
        for statement in CREATE_TABLES_SQL.strip().split(";"):
            stmt = statement.strip()
            if stmt:
                await conn.execute(text(stmt))
    yield
    await test_engine.dispose()


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yields a test DB session, rolls back after each test."""
    async with TestSessionLocal() as session:
        yield session
        await session.rollback()


# ── Redis mock ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_redis():
    """Mock all Redis interactions so tests don't need a Redis instance."""
    redis_mock = AsyncMock()
    redis_mock.ping.return_value = True
    redis_mock.get.return_value  = None
    redis_mock.set.return_value  = True
    redis_mock.setex.return_value = True
    redis_mock.delete.return_value = True
    redis_mock.publish.return_value = 1
    redis_mock.keys.return_value = []

    with patch("app.cache._redis", redis_mock), \
         patch("app.cache.get_cache", return_value=redis_mock), \
         patch("app.cache.check_cache_health", return_value=True), \
         patch("app.cache.get_last_event_time", return_value=None), \
         patch("app.cache.get_all_store_ids",   return_value=[]), \
         patch("app.cache.update_last_event_time", return_value=None), \
         patch("app.cache.invalidate_metrics",    return_value=None), \
         patch("app.cache.publish_event",         return_value=None), \
         patch("app.cache.set_metrics",           return_value=None), \
         patch("app.cache.get_metrics",           return_value=None):
        yield redis_mock


# ── DB dependency override ────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def override_db(db_session):
    """Override the FastAPI get_db dependency with the test session."""
    from app.main import app
    from app import db as db_module

    async def _test_db():
        yield db_session

    app.dependency_overrides[db_module.get_db] = _test_db
    yield
    app.dependency_overrides.clear()


# ── DB health mock ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def mock_db_health():
    with patch("app.db.check_db_health", return_value=True), \
         patch("app.db.init_db",         return_value=None), \
         patch("app.db.close_db",        return_value=None):
        yield


# ── HTTP client fixture ──────────────────────────────────────────────────────

@pytest_asyncio.fixture
async def client():
    from app.main import app
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ── Convenience event factory ────────────────────────────────────────────────

def make_event(
    event_type="ENTRY",
    visitor_id=None,
    store_id="STORE_BLR_002",
    camera_id="CAM_ENTRY_01",
    zone_id=None,
    is_staff=False,
    confidence=0.88,
    timestamp="2026-03-03T14:00:00Z",
    dwell_ms=0,
) -> dict:
    return {
        "event_id":   str(uuid.uuid4()),
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

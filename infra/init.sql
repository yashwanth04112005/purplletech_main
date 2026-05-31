-- Store Intelligence DB Schema
-- Run automatically by Docker on first start

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─────────────────────────────────────────
-- Events table (append-only event log)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS events (
    id             BIGSERIAL PRIMARY KEY,
    event_id       UUID        NOT NULL UNIQUE,
    store_id       VARCHAR(64) NOT NULL,
    camera_id      VARCHAR(64) NOT NULL,
    visitor_id     VARCHAR(64) NOT NULL,
    event_type     VARCHAR(64) NOT NULL,
    timestamp      TIMESTAMPTZ NOT NULL,
    zone_id        VARCHAR(64),
    dwell_ms       INTEGER     NOT NULL DEFAULT 0,
    is_staff       BOOLEAN     NOT NULL DEFAULT FALSE,
    confidence     FLOAT       NOT NULL,
    queue_depth    INTEGER,
    sku_zone       VARCHAR(64),
    session_seq    INTEGER,
    raw_payload    JSONB       NOT NULL,
    ingested_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_events_store_ts  ON events(store_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_visitor    ON events(visitor_id);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_zone       ON events(store_id, zone_id, timestamp DESC);

-- ─────────────────────────────────────────
-- Visitor sessions (materialised per visit)
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS visitor_sessions (
    id              BIGSERIAL PRIMARY KEY,
    session_id      UUID        NOT NULL DEFAULT uuid_generate_v4(),
    store_id        VARCHAR(64) NOT NULL,
    visitor_id      VARCHAR(64) NOT NULL,
    entry_time      TIMESTAMPTZ,
    exit_time       TIMESTAMPTZ,
    is_converted    BOOLEAN     NOT NULL DEFAULT FALSE,
    transaction_id  VARCHAR(64),
    zones_visited   TEXT[],
    was_in_billing  BOOLEAN     NOT NULL DEFAULT FALSE,
    reentry_count   INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sessions_store  ON visitor_sessions(store_id, entry_time DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_visitor ON visitor_sessions(visitor_id);

-- ─────────────────────────────────────────
-- POS Transactions
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS pos_transactions (
    id              BIGSERIAL PRIMARY KEY,
    store_id        VARCHAR(64) NOT NULL,
    transaction_id  VARCHAR(64) NOT NULL UNIQUE,
    timestamp       TIMESTAMPTZ NOT NULL,
    basket_value    NUMERIC(12,2),
    matched_session UUID
);

CREATE INDEX IF NOT EXISTS idx_pos_store_ts ON pos_transactions(store_id, timestamp DESC);

-- ─────────────────────────────────────────
-- Anomalies log
-- ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS anomalies (
    id               BIGSERIAL PRIMARY KEY,
    anomaly_id       UUID        NOT NULL DEFAULT uuid_generate_v4(),
    store_id         VARCHAR(64) NOT NULL,
    anomaly_type     VARCHAR(64) NOT NULL,
    severity         VARCHAR(16) NOT NULL,  -- INFO | WARN | CRITICAL
    detected_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ,
    description      TEXT,
    suggested_action TEXT,
    metadata         JSONB
);

CREATE INDEX IF NOT EXISTS idx_anomalies_store ON anomalies(store_id, detected_at DESC);

"""
Pydantic models — Event schema, request/response types.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ── Enums ─────────────────────────────────────────────────────────────────────

class EventType(str, Enum):
    ENTRY                  = "ENTRY"
    EXIT                   = "EXIT"
    ZONE_ENTER             = "ZONE_ENTER"
    ZONE_EXIT              = "ZONE_EXIT"
    ZONE_DWELL             = "ZONE_DWELL"
    BILLING_QUEUE_JOIN     = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON  = "BILLING_QUEUE_ABANDON"
    REENTRY                = "REENTRY"


class Severity(str, Enum):
    INFO     = "INFO"
    WARN     = "WARN"
    CRITICAL = "CRITICAL"


class AnomalyType(str, Enum):
    BILLING_QUEUE_SPIKE  = "BILLING_QUEUE_SPIKE"
    CONVERSION_DROP      = "CONVERSION_DROP"
    DEAD_ZONE            = "DEAD_ZONE"
    STALE_FEED           = "STALE_FEED"
    EMPTY_STORE          = "EMPTY_STORE"


# ── Core Event ─────────────────────────────────────────────────────────────────

class EventMetadata(BaseModel):
    queue_depth:  Optional[int]   = None
    sku_zone:     Optional[str]   = None
    session_seq:  Optional[int]   = None

    model_config = {"extra": "allow"}


class StoreEvent(BaseModel):
    event_id:   uuid.UUID   = Field(default_factory=uuid.uuid4)
    store_id:   str         = Field(..., min_length=1, max_length=64)
    camera_id:  str         = Field(..., min_length=1, max_length=64)
    visitor_id: str         = Field(..., min_length=1, max_length=64)
    event_type: EventType
    timestamp:  datetime
    zone_id:    Optional[str] = None
    dwell_ms:   int         = Field(default=0, ge=0)
    is_staff:   bool        = False
    confidence: float       = Field(..., ge=0.0, le=1.0)
    metadata:   EventMetadata = Field(default_factory=EventMetadata)

    @field_validator("zone_id")
    @classmethod
    def zone_required_for_zone_events(cls, v: Optional[str], info) -> Optional[str]:
        return v

    @model_validator(mode="after")
    def validate_zone_events(self) -> "StoreEvent":
        zone_events = {
            EventType.ZONE_ENTER,
            EventType.ZONE_EXIT,
            EventType.ZONE_DWELL,
            EventType.BILLING_QUEUE_JOIN,
            EventType.BILLING_QUEUE_ABANDON,
        }
        if self.event_type in zone_events and not self.zone_id:
            raise ValueError(f"zone_id required for event_type={self.event_type}")
        return self


# ── Ingest Request / Response ──────────────────────────────────────────────────

class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(..., min_length=1, max_length=500)


class EventError(BaseModel):
    index:   int
    event_id: Optional[str]
    error:   str


class IngestResponse(BaseModel):
    accepted:    int
    rejected:    int
    duplicates:  int
    errors:      list[EventError] = []
    trace_id:    Optional[str]    = None


# ── Metrics ────────────────────────────────────────────────────────────────────

class ZoneDwell(BaseModel):
    zone_id:       str
    avg_dwell_ms:  float
    visit_count:   int


class StoreMetrics(BaseModel):
    store_id:          str
    window_start:      datetime
    window_end:        datetime
    unique_visitors:   int
    converted_visitors: int
    conversion_rate:   float   # 0.0–1.0
    avg_dwell_ms:      float
    zone_dwell:        list[ZoneDwell]
    queue_depth:       int
    abandonment_rate:  float
    data_confidence:   str     # HIGH | MEDIUM | LOW


# ── Funnel ─────────────────────────────────────────────────────────────────────

class FunnelStage(BaseModel):
    stage:        str
    count:        int
    drop_off_pct: float


class StoreFunnel(BaseModel):
    store_id: str
    stages:   list[FunnelStage]
    sessions: int


# ── Heatmap ────────────────────────────────────────────────────────────────────

class HeatmapCell(BaseModel):
    zone_id:          str
    visit_frequency:  int
    avg_dwell_ms:     float
    normalised_score: float   # 0–100
    data_confidence:  str


class StoreHeatmap(BaseModel):
    store_id:         str
    generated_at:     datetime
    total_sessions:   int
    cells:            list[HeatmapCell]
    data_confidence:  str


# ── Anomalies ──────────────────────────────────────────────────────────────────

class Anomaly(BaseModel):
    anomaly_id:       str
    store_id:         str
    anomaly_type:     AnomalyType
    severity:         Severity
    detected_at:      datetime
    description:      str
    suggested_action: str
    metadata:         dict[str, Any] = {}


class AnomalyList(BaseModel):
    store_id: str
    anomalies: list[Anomaly]


# ── Health ─────────────────────────────────────────────────────────────────────

class StoreFeedStatus(BaseModel):
    store_id:          str
    last_event_at:     Optional[datetime]
    lag_minutes:       Optional[float]
    status:            str   # OK | STALE_FEED | NO_DATA


class HealthResponse(BaseModel):
    status:        str
    version:       str
    db_connected:  bool
    cache_connected: bool
    store_feeds:   list[StoreFeedStatus]
    checked_at:    datetime

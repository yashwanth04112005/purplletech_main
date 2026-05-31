"""
assertions.py — 10 example test assertions the API must pass.
Run against a live API: python data/assertions.py --api-url http://localhost:8000

These are illustrative checks only (not the full evaluator harness).
"""

import argparse
import sys
import uuid
from datetime import datetime, timezone

import requests

PASS = "[PASS]"
FAIL = "[FAIL]"
REQUEST_TIMEOUT_SECONDS = 20


def check(name: str, condition: bool, detail: str = "") -> bool:
    status = PASS if condition else FAIL
    print(f"  {status}  {name}")
    if not condition and detail:
        print(f"         -> {detail}")
    return condition


def make_event(
    *,
    store_id: str,
    visitor_id: str,
    event_type: str,
    timestamp: str,
    camera_id: str = "CAM_ENTRY_01",
    zone_id=None,
    dwell_ms: int = 0,
    is_staff: bool = False,
    confidence: float = 0.9,
    queue_depth=None,
    session_seq: int = 1,
):
    return {
        "event_id": str(uuid.uuid4()),
        "store_id": store_id,
        "camera_id": camera_id,
        "visitor_id": visitor_id,
        "event_type": event_type,
        "timestamp": timestamp,
        "zone_id": zone_id,
        "dwell_ms": dwell_ms,
        "is_staff": is_staff,
        "confidence": confidence,
        "metadata": {
            "queue_depth": queue_depth,
            "sku_zone": zone_id,
            "session_seq": session_seq,
        },
    }


def post_json(base: str, path: str, payload: dict):
    return requests.post(
        f"{base}{path}",
        json=payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )


def get_json(base: str, path: str):
    return requests.get(f"{base}{path}", timeout=REQUEST_TIMEOUT_SECONDS)


def run_assertions(api_url: str) -> bool:
    base = api_url.rstrip("/")
    results: list[bool] = []

    # Use an isolated store id so assertions are deterministic and not polluted
    # by prior test runs or seeded datasets.
    store_id = f"STORE_ASSERT_{uuid.uuid4().hex[:8].upper()}"
    visitor_customer = f"VIS_{uuid.uuid4().hex[:6]}"
    visitor_staff = f"VIS_{uuid.uuid4().hex[:6]}"
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"\nRunning assertions against {base}")
    print(f"Isolated test store: {store_id}\n")

    valid_events = [
        make_event(
            store_id=store_id,
            visitor_id=visitor_customer,
            event_type="ENTRY",
            timestamp=now_ts,
            is_staff=False,
        ),
        make_event(
            store_id=store_id,
            visitor_id=visitor_staff,
            event_type="ENTRY",
            timestamp=now_ts,
            is_staff=True,
        ),
        make_event(
            store_id=store_id,
            visitor_id=visitor_customer,
            event_type="REENTRY",
            timestamp=now_ts,
            is_staff=False,
        ),
        make_event(
            store_id=store_id,
            visitor_id=visitor_customer,
            event_type="ZONE_ENTER",
            timestamp=now_ts,
            zone_id="SKINCARE",
            is_staff=False,
            session_seq=2,
        ),
    ]

    # 1) Ingest valid batch and assert expected status code.
    r1 = post_json(base, "/events/ingest", {"events": valid_events})
    results.append(check(
        "1. POST /events/ingest(valid) returns 207",
        r1.status_code == 207,
        f"got {r1.status_code}",
    ))
    b1 = r1.json() if r1.headers.get("content-type", "").startswith("application/json") else {}

    # 2) Valid events were accepted.
    results.append(check(
        "2. Ingest accepts all valid events",
        b1.get("accepted") == len(valid_events) and b1.get("rejected") == 0,
        str(b1),
    ))

    # 3) Partial success for malformed event in mixed batch.
    mixed_batch = [
        make_event(
            store_id=store_id,
            visitor_id=f"VIS_{uuid.uuid4().hex[:6]}",
            event_type="ENTRY",
            timestamp=now_ts,
        ),
        {"event_id": "not-a-uuid", "store_id": store_id},
    ]
    r2 = post_json(base, "/events/ingest", {"events": mixed_batch})
    b2 = r2.json() if r2.headers.get("content-type", "").startswith("application/json") else {}
    has_structured_error = bool(b2.get("errors")) and "index" in b2["errors"][0] and "error" in b2["errors"][0]
    results.append(check(
        "3. Ingest supports partial success with structured errors",
        r2.status_code == 207 and b2.get("accepted", 0) >= 1 and b2.get("rejected", 0) >= 1 and has_structured_error,
        str(b2),
    ))

    # 4) Idempotency by event_id.
    r3 = post_json(base, "/events/ingest", {"events": valid_events})
    b3 = r3.json() if r3.headers.get("content-type", "").startswith("application/json") else {}
    results.append(check(
        "4. Idempotency: replayed payload is counted as duplicates",
        r3.status_code == 207 and b3.get("accepted") == 0 and b3.get("duplicates", 0) >= len(valid_events),
        str(b3),
    ))

    # 5) Batch limit enforcement.
    first = valid_events[0].copy()
    big_batch = []
    for _ in range(501):
        candidate = first.copy()
        candidate["event_id"] = str(uuid.uuid4())
        candidate["visitor_id"] = f"VIS_{uuid.uuid4().hex[:6]}"
        big_batch.append(candidate)
    r4 = post_json(base, "/events/ingest", {"events": big_batch})
    results.append(check(
        "5. Batch > 500 events returns 422",
        r4.status_code == 422,
        f"got {r4.status_code}",
    ))

    # 6) Metrics endpoint basic contract + required fields.
    r5 = get_json(base, f"/stores/{store_id}/metrics")
    m = r5.json() if r5.headers.get("content-type", "").startswith("application/json") else {}
    required_metric_fields = {
        "store_id", "window_start", "window_end", "unique_visitors",
        "converted_visitors", "conversion_rate", "avg_dwell_ms",
        "zone_dwell", "queue_depth", "abandonment_rate", "data_confidence",
    }
    metric_missing = required_metric_fields - set(m.keys())
    results.append(check(
        "6. GET /stores/{id}/metrics returns full schema",
        r5.status_code == 200 and not metric_missing,
        f"status={r5.status_code}, missing={sorted(metric_missing)}",
    ))

    # 7) Staff exclusion in metrics (we ingested exactly one customer ENTRY + one staff ENTRY).
    results.append(check(
        "7. Metrics exclude is_staff=true from unique_visitors",
        m.get("unique_visitors") == 1,
        f"unique_visitors={m.get('unique_visitors')}",
    ))

    # 8) Funnel session logic and stage shape.
    r6 = get_json(base, f"/stores/{store_id}/funnel")
    f = r6.json() if r6.headers.get("content-type", "").startswith("application/json") else {}
    stages = f.get("stages", [])
    entry_stage = next((s for s in stages if s.get("stage") == "Entry"), None)
    results.append(check(
        "8. Funnel has 4 stages and re-entry does not inflate Entry count",
        r6.status_code == 200 and len(stages) == 4 and entry_stage is not None and entry_stage.get("count") == 1,
        str(f),
    ))

    # 9) Heatmap response includes confidence flag (required for sparse data).
    r7 = get_json(base, f"/stores/{store_id}/heatmap")
    hmap = r7.json() if r7.headers.get("content-type", "").startswith("application/json") else {}
    results.append(check(
        "9. Heatmap returns data_confidence for sparse window",
        r7.status_code == 200 and hmap.get("data_confidence") in {"LOW", "MEDIUM", "HIGH"},
        str(hmap),
    ))

    # 10) Health endpoint is available and confirms DB connectivity.
    r8 = get_json(base, "/health")
    h = r8.json() if r8.headers.get("content-type", "").startswith("application/json") else {}
    results.append(check(
        "10. GET /health is healthy and db_connected is true",
        r8.status_code == 200 and h.get("db_connected") is True,
        str(h),
    ))

    passed = sum(results)
    total = len(results)
    print("\n" + "=" * 48)
    print(f"  Assertions passed: {passed}/{total}")
    if passed == total:
        print("  All assertions passed.")
    else:
        print(f"  {total - passed} assertion(s) failed.")
    print("=" * 48 + "\n")
    return passed == total


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-url", default="http://localhost:8000")
    args = parser.parse_args()
    ok = run_assertions(args.api_url)
    sys.exit(0 if ok else 1)

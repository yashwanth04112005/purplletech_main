"""
POS transaction correlation.
Matches pos_transactions.csv visitors to sessions in a 5-minute billing window.
"""
import csv
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import List

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()

POS_WINDOW_MINUTES = 5


async def load_pos_transactions(csv_path: str, db: AsyncSession) -> int:
    """Load POS CSV into pos_transactions table (idempotent via ON CONFLICT)."""
    path = Path(csv_path)
    if not path.exists():
        log.warning("pos_csv_not_found", path=csv_path)
        return 0

    inserted = 0
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sql = text("""
                INSERT INTO pos_transactions (store_id, transaction_id, timestamp, basket_value)
                VALUES (:store_id, :transaction_id, :timestamp, :basket_value)
                ON CONFLICT (transaction_id) DO NOTHING
            """)
            await db.execute(sql, {
                "store_id":       row["store_id"].strip(),
                "transaction_id": row["transaction_id"].strip(),
                "timestamp":      datetime.fromisoformat(row["timestamp"].strip().replace("Z", "+00:00")),
                "basket_value":   float(row["basket_value_inr"].strip()),
            })
            inserted += 1

    log.info("pos_transactions_loaded", count=inserted, path=csv_path)
    return inserted


async def run_conversion_matching(store_id: str, db: AsyncSession) -> int:
    """
    For each unmatched POS transaction, find sessions where the visitor
    was in the billing zone within POS_WINDOW_MINUTES before the transaction.
    Updates visitor_sessions.is_converted = TRUE.
    """
    sql_txns = text("""
        SELECT transaction_id, timestamp
        FROM pos_transactions
        WHERE store_id    = :store_id
          AND matched_session IS NULL
        ORDER BY timestamp
    """)
    txns = (await db.execute(sql_txns, {"store_id": store_id})).fetchall()

    matched = 0
    for txn in txns:
        txn_ts = txn.timestamp
        # SQLite returns timestamps as strings; PostgreSQL returns datetime objects
        if isinstance(txn_ts, str):
            txn_ts = datetime.fromisoformat(txn_ts.replace("Z", "+00:00"))
        if txn_ts.tzinfo is None:
            txn_ts = txn_ts.replace(tzinfo=timezone.utc)
        window_start = txn_ts - timedelta(minutes=POS_WINDOW_MINUTES)

        # Find a session that was in billing zone during the window
        sql_session = text("""
            SELECT vs.visitor_id, vs.session_id
            FROM visitor_sessions vs
            INNER JOIN events e ON e.visitor_id = vs.visitor_id
              AND e.store_id   = :store_id
              AND e.event_type = 'BILLING_QUEUE_JOIN'
              AND e.timestamp  BETWEEN :window_start AND :txn_ts
            WHERE vs.store_id       = :store_id
              AND vs.was_in_billing = TRUE
              AND vs.is_converted   = FALSE
            LIMIT 1
        """)
        # Normalize datetimes to string form for SQLite compatibility
        txn_param = txn_ts.strftime("%Y-%m-%dT%H:%M:%S")
        window_param = window_start.strftime("%Y-%m-%dT%H:%M:%S")

        row = (await db.execute(sql_session, {
            "store_id":     store_id,
            "window_start": window_param,
            "txn_ts":       txn_param,
        })).fetchone()

        if row is None:
            continue

        # Mark session as converted
        await db.execute(text("""
            UPDATE visitor_sessions
            SET is_converted = TRUE, transaction_id = :txn_id, updated_at = CURRENT_TIMESTAMP
            WHERE visitor_id = :visitor_id AND store_id = :store_id
        """), {
            "txn_id":     txn.transaction_id,
            "visitor_id": row.visitor_id,
            "store_id":   store_id,
        })

        # Mark transaction as matched
        await db.execute(text("""
            UPDATE pos_transactions
            SET matched_session = :session_id
            WHERE transaction_id = :txn_id
        """), {"session_id": str(row.session_id), "txn_id": txn.transaction_id})

        matched += 1

    log.info("conversion_matching_done", store_id=store_id, matched=matched)
    return matched

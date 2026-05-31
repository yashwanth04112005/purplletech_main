"""
load_pos.py — Load POS transactions CSV and run conversion matching.
Called by pipeline/run.sh after each store's clips are processed.

Usage:
    python pipeline/load_pos.py --csv data/pos_transactions.csv --store-id STORE_BLR_002
"""
import argparse
import asyncio
import sys

sys.path.insert(0, ".")


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--csv",      required=True, help="Path to pos_transactions.csv")
    p.add_argument("--store-id", required=True, help="Store ID to match")
    return p.parse_args()


async def main():
    args = parse_args()
    from app.db import init_db, AsyncSessionLocal
    from app.pos_correlation import load_pos_transactions, run_conversion_matching

    await init_db()
    async with AsyncSessionLocal() as db:
        n = await load_pos_transactions(args.csv, db)
        await db.commit()
        matched = await run_conversion_matching(args.store_id, db)
        await db.commit()
        print(f"  ✓ POS: {n} loaded, {matched} sessions matched for {args.store_id}")


if __name__ == "__main__":
    asyncio.run(main())

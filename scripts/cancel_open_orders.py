from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.auth import PredictAuthService
from core.http_client import PredictHttpClient


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cancel open Predict orders by hash")
    parser.add_argument("--market-id", type=int, default=None, help="Only cancel OPEN orders for this market id")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print matching OPEN orders without cancelling",
    )
    parser.add_argument("--first", type=int, default=100, help="How many recent orders to query")
    return parser


async def run(market_id: int | None, dry_run: bool, first: int) -> int:
    auth = PredictAuthService()
    http = PredictHttpClient(auth)

    try:
        await auth.refresh_jwt()
        orders = await http.get_orders(first=first)
        rows = orders.get("data", [])

        open_rows: list[dict] = []
        for row in rows:
            if str(row.get("status", "")).upper() != "OPEN":
                continue
            if market_id is not None and int(row.get("marketId", -1)) != int(market_id):
                continue
            open_rows.append(row)

        print(f"open_count={len(open_rows)} market_filter={market_id}")
        if not open_rows:
            return 0

        hashes: list[str] = []
        for row in open_rows:
            order = row.get("order", {})
            order_hash = order.get("hash")
            if not order_hash:
                continue
            hashes.append(order_hash)
            print(
                "order",
                f"id={row.get('id')}",
                f"status={row.get('status')}",
                f"market={row.get('marketId')}",
                f"side={order.get('side')}",
                f"hash={order_hash}",
            )

        if not hashes:
            print("No cancellable hashes found among OPEN orders")
            return 0

        if dry_run:
            print("dry-run enabled: no cancellation executed")
            return 0

        resp = await http.remove_orders_by_hash(hashes)
        print("cancel_response", resp)

        orders_after = await http.get_orders(first=first)
        open_after = []
        for row in orders_after.get("data", []):
            if str(row.get("status", "")).upper() != "OPEN":
                continue
            if market_id is not None and int(row.get("marketId", -1)) != int(market_id):
                continue
            open_after.append(row)
        print(f"open_count_after={len(open_after)}")
        return 0
    finally:
        await http.close()


def main() -> int:
    args = build_parser().parse_args()
    if args.first <= 0:
        raise SystemExit("--first must be > 0")
    return asyncio.run(run(market_id=args.market_id, dry_run=args.dry_run, first=args.first))


if __name__ == "__main__":
    raise SystemExit(main())

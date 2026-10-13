from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from predict_sdk import ApprovalScope, ChainId, OrderBuilder, OrderBuilderOptions

from config.settings import settings
from core.auth import PredictAuthService
from core.http_client import PredictHttpClient


def _chain_id() -> ChainId:
    return ChainId.BNB_MAINNET if settings.predict_env == "prod" else ChainId.BNB_TESTNET


def _print_header(address: str, market_id: int) -> None:
    print(f"env={settings.predict_env} market_id={market_id}")
    print(f"signer={address}")


def _print_checks(checks: list) -> None:
    for check in checks:
        step = check.step
        mark = "OK" if check.satisfied else "MISSING"
        print(f"[{mark}] {step.id} | type={step.type} | spender={step.spender} | token={step.token}")


def _print_run_report(report) -> None:
    print(f"run_success={report.success}")
    for item in report.steps:
        tx_hash = None
        tx = getattr(item, "transaction", None)
        if tx is not None:
            receipt = getattr(tx, "receipt", None)
            if receipt is not None:
                tx_hash = getattr(receipt, "transactionHash", None)
                if tx_hash is not None and not isinstance(tx_hash, str):
                    tx_hash = tx_hash.hex()
        print(f"[{item.status}] {item.step.id} tx={tx_hash}")


async def _get_market_flags(market_id: int) -> tuple[bool, bool]:
    auth = PredictAuthService()
    http = PredictHttpClient(auth)
    try:
        market = await http.get_market_by_id(market_id)
        if market is None:
            raise ValueError(f"market not found: {market_id}")
        return market.is_neg_risk, market.is_yield_bearing
    finally:
        await http.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Check or set Predict exchange approvals for trading")
    parser.add_argument("--market-id", type=int, default=(settings.parsed_market_ids[0] if settings.parsed_market_ids else None))
    parser.add_argument("--apply", action="store_true", help="Send approval transactions for missing steps")
    args = parser.parse_args()

    if not settings.predict_wallet_private_key:
        raise SystemExit("PREDICT_WALLET_PRIVATE_KEY is required")
    if args.market_id is None:
        raise SystemExit("Provide --market-id or set TARGET_MARKET_IDS")

    import asyncio

    is_neg_risk, is_yield_bearing = asyncio.run(_get_market_flags(args.market_id))

    options = OrderBuilderOptions(predict_account=settings.predict_account_address) if settings.predict_account_address else None
    builder = OrderBuilder.make(_chain_id(), settings.predict_wallet_private_key, options)

    signer = getattr(builder, "signer", None) or getattr(builder, "_signer", None)
    signer_addr = signer.address if signer else "<none>"
    _print_header(signer_addr, args.market_id)
    print(f"market_flags is_neg_risk={is_neg_risk} is_yield_bearing={is_yield_bearing}")

    try:
        bal = builder.balance_of("USDT")
        print(f"usdt_balance_wei={bal}")
    except Exception as exc:  # best-effort diagnostic
        print(f"usdt_balance_error={exc}")

    scope = ApprovalScope(
        operation="TRADE",
        is_neg_risk=is_neg_risk,
        is_yield_bearing=is_yield_bearing,
    )
    steps = builder.get_approval_steps(scope)
    checks = builder.check_approvals(steps)
    _print_checks(checks)

    missing = [c for c in checks if not c.satisfied]
    print(f"missing_steps={len(missing)}")
    if not args.apply:
        return 0

    if not missing:
        print("All approvals already set.")
        return 0

    print("Sending approval transactions...")
    report = builder.run_approvals(steps, skip_satisfied=True, stop_on_error=True)
    _print_run_report(report)
    return 0 if report.success else 2


if __name__ == "__main__":
    raise SystemExit(main())

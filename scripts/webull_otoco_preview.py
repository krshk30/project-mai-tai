#!/usr/bin/env python
"""Webull OTOCO combo bracket — STEP-1 item 0 preview probe (ZERO RISK, validate-without-place).

Calls the v3 `preview_order` endpoint through WebullBrokerAdapter.preview_bracket_order for a
3-leg MASTER(BUY) + STOP_PROFIT(SELL LIMIT) + STOP_LOSS(SELL STOP) combo, on a REAL Webull account,
and prints the broker's validation result. NOTHING is placed -- `preview_order` is Webull's
validate-without-place endpoint (the whole point). This re-establishes the exact `new_orders`
shape the account accepts (the 07-20 "validated" artifact was lost) and de-risks the attended
qty-1 STEP-1 gate.

Defaults mirror Webull's own SDK combo sample (F, LIMIT 10.5 master / 11.5 target / 10.0 stop) --
the shape most likely to be accepted. Override for a far-from-market or different-price probe.

Usage (read-only; safe any time -- preview never places):
  python scripts/webull_otoco_preview.py --account live:orb --symbol F
  python scripts/webull_otoco_preview.py --account live:orb --symbol F \
      --entry 10.50 --target 11.50 --stop 10.00 --qty 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from decimal import Decimal

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, configured_webull_accounts
from project_mai_tai.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("webull-otoco-preview")


def _build_request(account: str, symbol: str, qty: int, entry: str, target: str, stop: str,
                   entry_type: str) -> OrderRequest:
    metadata: dict[str, object] = {
        "bracket": "true",
        "bracket_entry_type": entry_type,     # LIMIT or MARKET (buy-STOP master rejects on Webull)
        "bracket_target_price": target,       # STOP_PROFIT (+target)
        "bracket_stop_price": stop,           # STOP_LOSS (-protect)
    }
    if entry_type == "LIMIT":
        metadata["limit_price"] = entry
    return OrderRequest(
        client_order_id=f"otoco-preview-{symbol}-{qty}",
        broker_account_name=account,
        strategy_code="schwab_1m_v2",
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal(str(qty)),
        reason="STEP1_PREVIEW",
        order_type="limit",
        metadata=metadata,
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    accounts = configured_webull_accounts(settings)
    if args.account not in accounts:
        log.error("account %s not configured (have: %s). Set MAI_TAI_WEBULL_ACCOUNT_ID + a webull "
                  "registration.", args.account, sorted(accounts))
        return 2
    adapter = WebullBrokerAdapter(settings, accounts_by_name=accounts)
    request = _build_request(args.account, args.symbol, args.qty, args.entry, args.target,
                             args.stop, args.entry_type.upper())

    legs = adapter._build_combo_payload(request)  # what we are sending (also validates the shape)
    log.info("PREVIEW (no order placed) new_orders = \n%s", json.dumps(legs, indent=2))

    status, body = await adapter.preview_bracket_order(request)
    log.info("preview_order -> status=%s", status)
    log.info("body = \n%s", json.dumps(body, indent=2, default=str))

    ok = 200 <= int(status or 0) < 300
    # Webull may return 2xx with an error field OR raise (-> 599). Surface a clear read either way.
    err = None
    if isinstance(body, dict):
        err = body.get("error") or body.get("error_code") or body.get("msg") or body.get("message")
    if ok and not err:
        log.info("RESULT: ✅ PREVIEW ACCEPTED -- the account accepts this OTOCO new_orders shape.")
        return 0
    log.warning("RESULT: ⚠ PREVIEW NOT CLEAN (status=%s err=%r). Inspect the body above: a shape "
                "error is fixable now; a session/hours error means retry in RTH.", status, err)
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Webull OTOCO preview probe (read-only, no place).")
    p.add_argument("--account", default="live:orb")
    p.add_argument("--symbol", default="F")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--entry", default="10.50", help="MASTER limit price (ignored for --entry-type MARKET)")
    p.add_argument("--target", default="11.50", help="STOP_PROFIT limit price")
    p.add_argument("--stop", default="10.00", help="STOP_LOSS stop price")
    p.add_argument("--entry-type", default="LIMIT", choices=["LIMIT", "MARKET", "limit", "market"])
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

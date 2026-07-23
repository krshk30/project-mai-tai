#!/usr/bin/env python3
"""Preview a STOP_LIMIT-master OTOCO at Schwab — the gate for the v2 resting flip-entry. ZERO RISK.

Calls POST /previewOrder (validate-without-place) for a bracket whose parent is a buy STOP_LIMIT
(triggers at stopPrice, fills only <= price/the band cap) with the +2%/-5% OCO attached. NOTHING is
placed. STOP and LIMIT masters were preview-validated 2026-07-21; a STOP_LIMIT master (both prices)
has NOT been, so this must return HTTP 200 / zero rejects before the resting flip-entry goes live
(docs/v2-resting-flip-entry-design.md, PR-3 gate).

Prices are FAR above market by default (a resting buy-stop-limit that could not fill now) — but it
never places, so the exact values only need to be structurally valid (target>entry, protect<entry).

Usage (read-only; safe any time):
  python scripts/schwab_stoplimit_preview.py --symbol SOFI
  python scripts/schwab_stoplimit_preview.py --symbol SOFI --stop 30.00 --band 0.5 --qty 1
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from decimal import Decimal

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter, configured_schwab_accounts
from project_mai_tai.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("schwab-stoplimit-preview")

ACCOUNT = "live:schwab_1m_v2"


def _req(symbol: str, qty: int, stop: float, band: float) -> OrderRequest:
    limit = round(stop * (1 + band / 100.0), 2)   # the slippage-cap band above the trigger
    target = round(stop * 1.02, 2)                 # +2%
    protect = round(stop * 0.95, 2)                # -5%
    return OrderRequest(
        client_order_id=f"stoplimit-preview-{symbol}",
        broker_account_name=ACCOUNT,
        strategy_code="schwab_1m_v2",
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=Decimal(str(qty)),
        reason="STOPLIMIT_PREVIEW",
        order_type="stop_limit",
        metadata={
            "bracket": "true",
            "bracket_entry_type": "STOP_LIMIT",
            "stop_price": f"{stop:.2f}",       # the ATR line (trigger)
            "limit_price": f"{limit:.2f}",     # line * (1 + band%) -> fill cap
            "bracket_target_price": f"{target:.2f}",
            "bracket_stop_price": f"{protect:.2f}",
        },
    )


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    accounts = configured_schwab_accounts(settings)
    if ACCOUNT not in accounts:
        log.error("account %s not configured (have %s)", ACCOUNT, list(accounts))
        return 2
    adapter = SchwabBrokerAdapter(settings)
    req = _req(args.symbol.upper(), args.qty, args.stop, args.band)
    log.info("PREVIEW (no order placed) payload = \n%s",
             json.dumps(adapter._build_bracket_payload(req), indent=2))

    status, body = await adapter.preview_bracket_order(req)
    rejects = []
    if isinstance(body, dict):
        rejects = ((body.get("orderValidationResult") or {}).get("rejects")) or []
    log.info("previewOrder -> HTTP %s, rejects=%d", status, len(rejects))
    if isinstance(body, dict):
        log.info("advancedOrderType=%s", body.get("advancedOrderType"))
    for r in rejects:
        log.warning("   reject: %s", r.get("activityMessage"))
    if status in (200, 201) and not rejects:
        log.info("RESULT: ✅ STOP_LIMIT-master OTOCO ACCEPTED — resting flip-entry shape is broker-valid.")
        return 0
    log.warning("RESULT: ⚠ NOT CLEAN (HTTP %s, %d rejects) — inspect above; fix the shape before live.",
                status, len(rejects))
    return 1


def main() -> int:
    p = argparse.ArgumentParser(description="Schwab STOP_LIMIT-master OTOCO preview (read-only).")
    p.add_argument("--symbol", required=True, help="liquid, cheap, NOT CYN/CELZ")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--stop", type=float, default=30.00, help="trigger (the ATR line); default far above market")
    p.add_argument("--band", type=float, default=0.5, help="limit band above the trigger, %%")
    return asyncio.run(_run(p.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())

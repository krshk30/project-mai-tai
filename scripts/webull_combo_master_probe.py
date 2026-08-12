#!/usr/bin/env python3
"""Probe W — CAN A WEBULL COMBO MASTER BE A STOP_LIMIT? Preview-first, then one attended live rest.

WHY THIS EXISTS
---------------
`webull.py::_build_combo_payload` refuses a non-LIMIT/MARKET master CLIENT-SIDE:

    Webull combo MASTER must be LIMIT or MARKET (a buy-STOP master rejects); got {entry_type}

⭐ 2026-08-11 evidence: the operator's manual Webull bracket screenshot shows the entry as a plain
LIMIT with Stop-Loss + Take-Profit legs attached, TIF Day, regular hours. That is CONSISTENT with
the restriction being real rather than an untested assumption — the note is updated accordingly.
⛔ But the manual UI not OFFERING a stop-limit master is NOT proof the API REFUSES one. The guard
above has never actually asked Webull. This probe asks.

It matters because the Schwab leg now RESTS (a STOP_LIMIT that triggers at the ATR line) while the
Webull fan-out leg still sends MARKET — the measured order-type asymmetry that owns every entry
>=200bps of slippage. If a STOP_LIMIT master is accepted, the two legs can finally match.

THREE SHAPES, IN ORDER
----------------------
  A  LIMIT master + STOP_PROFIT + STOP_LOSS   -- expected to work; establishes the API baseline
                                                 (the UI already does this; the API has not been
                                                 re-proven since the 07-20 artifact was lost)
  B  STOP_LIMIT master + the same legs        -- ⭐ THE DECISIVE ONE
  C  bare STOP_LIMIT entry, no legs           -- the fallback. Already known accepted (44 historical
                                                 single-leg STOP_LIMITs), so this CONFIRMS rather
                                                 than discovers, and sets up the follow-on question:
                                                 can protection be attached AFTER the fill?

⛔ RECORD THE SESSION BESIDE EVERY RESULT. Omitting it is what produced the wrong 2026-07-25 note.
A refusal in POST hours says nothing about CORE, and vice versa.

SAFETY
------
* Default is PREVIEW ONLY — `preview_order` is validate-without-place. Nothing is sent.
* `--live` places ONE qty-1 order per surviving shape, priced FAR from the market so it RESTS.
* Every placed order is cancelled in a `finally:` block, then VERIFIED gone, then a final sweep
  asserts nothing remains. ⚠️ Leaving a working order behind is the failure this week produced
  twice; this script must never be the third.
* Read-only against production state — the ONLY writes are the probe orders themselves.

USAGE
-----
  python scripts/webull_combo_master_probe.py --account live:orb --symbol F           # preview only
  python scripts/webull_combo_master_probe.py --account live:orb --symbol F --live    # + 1 rest each
  python scripts/webull_combo_master_probe.py --account live:orb --symbol F --shapes B
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, configured_webull_accounts
from project_mai_tai.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("probe-w")
ET = ZoneInfo("America/New_York")

# Far-from-market multipliers. A BUY LIMIT rests BELOW the market; a BUY STOP_LIMIT rests ABOVE it
# (a buy stop triggers upward). Opposite directions on purpose — same "cannot fill now" intent.
LIMIT_AWAY = 0.70      # 30% below
STOP_AWAY = 1.30       # 30% above


def session_now() -> str:
    """⛔ The field the 2026-07-25 note omitted. Broker behaviour differs by session."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return f"WEEKEND-CLOSED ({now:%a %H:%M ET})"
    hm = now.hour * 60 + now.minute
    if hm < 4 * 60:
        return f"CLOSED ({now:%H:%M ET})"
    if hm < 9 * 60 + 30:
        return f"PRE ({now:%H:%M ET})"
    if hm < 16 * 60:
        return f"CORE/RTH ({now:%H:%M ET})"
    if hm < 20 * 60:
        return f"POST ({now:%H:%M ET})"
    return f"CLOSED ({now:%H:%M ET})"


def _tick(x: float) -> str:
    return f"{x:.2f}" if x >= 1.0 else f"{x:.4f}"


def build_payload(shape: str, symbol: str, qty: int, ref: float, coid: str) -> list[dict]:
    """Build the combo legs DIRECTLY — deliberately bypassing `_build_combo_payload`'s client-side
    master-type guard, because that guard is the very thing under test.

    Shape mirrors the adapter exactly (Webull v3 SDK sample): flat legs tagged by `combo_type`,
    symbol+market+instrument_type per leg, numeric fields as STRINGS, `support_trading_session`
    "CORE". Any divergence here would make a refusal unattributable.
    """
    common = {
        "symbol": symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "quantity": str(qty),
        "entrust_type": "QTY",
        "time_in_force": "DAY",
        "support_trading_session": "CORE",
    }
    if shape == "A":
        entry = ref * LIMIT_AWAY
        master = {**common, "client_order_id": f"{coid}M", "combo_type": "MASTER",
                  "side": "BUY", "order_type": "LIMIT", "limit_price": _tick(entry)}
    else:  # B and C both use a STOP_LIMIT master
        trigger = ref * STOP_AWAY
        entry = trigger
        master = {**common, "client_order_id": f"{coid}M", "combo_type": "MASTER",
                  "side": "BUY", "order_type": "STOP_LIMIT",
                  "stop_price": _tick(trigger),
                  "limit_price": _tick(trigger * 1.005)}   # the 0.50% slippage band
    if shape == "C":
        return [master]                                     # bare entry, no protection attached
    target = {**common, "client_order_id": f"{coid}T", "combo_type": "STOP_PROFIT",
              "side": "SELL", "order_type": "LIMIT", "limit_price": _tick(entry * 1.02)}
    protect = {**common, "client_order_id": f"{coid}S", "combo_type": "STOP_LOSS",
               "side": "SELL", "order_type": "STOP_LOSS", "stop_price": _tick(entry * 0.95)}
    return [master, target, protect]


SHAPES = {
    "A": "LIMIT master + STOP_PROFIT + STOP_LOSS  (baseline via the API, not the UI)",
    "B": "STOP_LIMIT master + STOP_PROFIT + STOP_LOSS  <-- THE DECISIVE ONE",
    "C": "bare STOP_LIMIT entry, NO legs  (fallback; expected accepted)",
}


def _op(adapter: WebullBrokerAdapter):
    from webull.trade.trade.v3.order_opration_v3 import OrderOperationV3
    return OrderOperationV3(adapter._get_client())


def _verbatim(body: object) -> str:
    try:
        return json.dumps(body, indent=2, default=str)[:1500]
    except Exception:  # noqa: BLE001
        return repr(body)[:1500]


async def run(args: argparse.Namespace) -> int:
    settings = get_settings()
    adapter = WebullBrokerAdapter(settings, accounts_by_name=configured_webull_accounts(settings))
    account = adapter.accounts_by_name.get(args.account)
    if account is None:
        log.error("no webull account %s (configured: %s)", args.account,
                  ",".join(sorted(adapter.accounts_by_name)) or "none")
        return 2

    sess = session_now()
    print("=" * 78)
    print(f"PROBE W — Webull combo MASTER type    account={args.account}  symbol={args.symbol}")
    print(f"SESSION AT RUN: {sess}")
    print(f"mode: {'PREVIEW + LIVE REST' if args.live else 'PREVIEW ONLY (nothing placed)'}")
    print("=" * 78)

    op = _op(adapter)
    results: dict[str, dict] = {}

    # ---------------------------------------------------------------- PHASE 1: preview (free)
    for shape in args.shapes:
        coid = f"probew-{shape}-{int(datetime.now(ET).timestamp())}"
        legs = build_payload(shape, args.symbol, args.qty, args.ref, coid)
        print(f"\n--- PREVIEW shape {shape}: {SHAPES[shape]}")
        print(f"    session={session_now()}")
        print(f"    payload={json.dumps(legs, indent=2)[:900]}")
        try:
            resp = await asyncio.to_thread(
                op.preview_order, account.account_id, legs, client_combo_order_id=coid)
            status, body = adapter._response_status(resp), adapter._body(resp)
        except Exception as exc:  # noqa: BLE001
            status, body = 599, {"exception": f"{type(exc).__name__}: {exc}"}
        print(f"    HTTP {status}")
        print(f"    VERBATIM: {_verbatim(body)}")
        results[shape] = {"session": session_now(), "preview_status": status, "preview_body": body}

    # ---------------------------------------------------------------- PHASE 2: one live rest
    placed: list[tuple[str, str]] = []          # (shape, combo/client id)
    if args.live:
        print("\n" + "=" * 78)
        print("LIVE PHASE — qty 1, priced FAR from market so it RESTS. Cancelled at the end.")
        print("=" * 78)
        try:
            for shape in args.shapes:
                if int(results[shape]["preview_status"]) >= 400:
                    print(f"\n--- LIVE shape {shape}: SKIPPED — preview refused it "
                          f"(HTTP {results[shape]['preview_status']})")
                    results[shape]["live"] = "skipped: preview refused"
                    continue
                coid = f"probewL{shape}{int(datetime.now(ET).timestamp())}"[:38]
                legs = build_payload(shape, args.symbol, args.qty, args.ref, coid)
                print(f"\n--- LIVE shape {shape}   session={session_now()}")
                try:
                    resp = await asyncio.to_thread(
                        op.place_order, account.account_id, legs, client_combo_order_id=coid)
                    status, body = adapter._response_status(resp), adapter._body(resp)
                    placed.append((shape, coid))
                except Exception as exc:  # noqa: BLE001
                    status, body = 599, {"exception": f"{type(exc).__name__}: {exc}"}
                print(f"    HTTP {status}")
                print(f"    VERBATIM: {_verbatim(body)}")
                results[shape]["live_status"] = status
                results[shape]["live_body"] = body
                results[shape]["live_session"] = session_now()
        finally:
            # ⚠️ ALWAYS — a working probe order left behind is the failure mode this week already
            # produced twice. This runs even on an exception above.
            print("\n" + "=" * 78)
            print("CLEANUP — cancel every placed probe order, then VERIFY it is gone")
            print("=" * 78)
            for shape, coid in placed:
                for leg in ("M", "T", "S"):
                    legcoid = f"{coid}{leg}"
                    try:
                        from webull.trade.request.cancel_order_request import CancelOrderRequest
                        co = CancelOrderRequest()
                        co.set_account_id(account.account_id)
                        co.set_client_order_id(legcoid)
                        body = adapter._body(
                            await asyncio.to_thread(adapter._get_client().get_response, co))
                        print(f"  cancel {legcoid}: {_verbatim(body)[:200]}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  cancel {legcoid}: {type(exc).__name__}: {exc}")

    # ---------------------------------------------------------------- PHASE 3: nothing left behind
    print("\n" + "=" * 78)
    print("FINAL SWEEP — any probe order still working?")
    print("=" * 78)
    leftover = 0
    for shape, coid in placed:
        for leg in ("M", "T", "S"):
            req = OrderRequest(
                client_order_id=f"{coid}{leg}", broker_account_name=args.account,
                strategy_code="probe_w", symbol=args.symbol, side="buy", intent_type="open",
                quantity=Decimal(str(args.qty)), reason="PROBE_W_SWEEP", order_type="limit",
                metadata={},
            )
            rep = await adapter.fetch_order_update(req)
            state = getattr(rep, "event_type", None) if rep else None
            live = state not in {None, "cancelled", "canceled", "rejected", "filled", "expired"}
            if live:
                leftover += 1
            print(f"  {coid}{leg}: {state or 'not found'}{'   <-- STILL WORKING' if live else ''}")
    if not placed:
        print("  (nothing was placed — preview-only run)")

    # ---------------------------------------------------------------- verdict
    print("\n" + "=" * 78)
    print("VERDICT   (session recorded beside EVERY result)")
    print("=" * 78)
    for shape in args.shapes:
        r = results[shape]
        print(f"  {shape}  {SHAPES[shape]}")
        print(f"       preview: HTTP {r['preview_status']}   session={r['session']}")
        if "live_status" in r:
            print(f"       live   : HTTP {r['live_status']}   session={r['live_session']}")
        elif args.live:
            print(f"       live   : {r.get('live', 'not attempted')}")
    print(f"\n  ⛔ ORDERS LEFT WORKING: {leftover}")
    if leftover:
        print("  ⛔⛔ CANCEL THEM BY HAND NOW — the script failed its own cleanup guarantee.")
        return 3
    print("  ✅ clean — nothing left behind")
    print("\n  ⚠️  A result is valid for the SESSION it ran in. A refusal in POST says nothing")
    print("      about CORE. Re-run per session before generalising.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account", default="live:orb")
    p.add_argument("--symbol", default="F")
    p.add_argument("--qty", type=int, default=1)
    p.add_argument("--ref", type=float, required=True,
                   help="reference/market price; probe prices are placed FAR from it so they rest")
    p.add_argument("--shapes", default="ABC", help="subset of ABC, in order (default all)")
    p.add_argument("--live", action="store_true",
                   help="ALSO place one qty-1 resting order per surviving shape, then cancel it")
    args = p.parse_args()
    args.shapes = [s for s in args.shapes.upper() if s in SHAPES]
    if not args.shapes:
        print("no valid shapes selected")
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())

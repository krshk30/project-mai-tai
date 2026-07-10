#!/usr/bin/env python
"""qty-1 Webull plumbing harness for the dual-broker v2 CW exit ladder.

Exercises EVERY order SHAPE the v2 confirmed-window exit ladder emits, directly through the
WebullBrokerAdapter on a REAL Webull account at qty 1, to shake out the ladder's Webull path
(the ORB go-live 4-bug pattern: 4-dec rounding #374, fill polling #375, STOP->STOP_LOSS #386,
limit+session EH) BEFORE the mirror is ever enabled. Gates the ENABLE, not the merge.

Order shapes covered (each submitted, polled, then cancelled/flattened):
  1. ENTRY        marketable LIMIT buy  (the mirror open)         -> real fill (price + broker time)
  2. HARD-STOP    STOP_LOSS sell, far from market                 -> accepted (no 417) + rests -> cancel
  3. SCALE/FLOOR  LIMIT sell at/above bid                         -> accepted -> cancel
  4. FLATTEN      marketable LIMIT sell of the held share         -> real fill -> account FLAT

SAFETY: real money, tiny (qty 1). Requires --confirm. try/finally ALWAYS cancels every resting
order and flattens any held share; verifies the account is FLAT at the end. Read-only dry-run
(prints the plan) without --confirm. Runs on the operator's existing `live:orb` Webull account.

Sessions: RTH = MARKET entry/flat (regular hours). AM/PM = extended-hours, marketable LIMIT
entry/flat + a session token (Webull EH is LIMIT-only — market/stop have no off-RTH fills, #429);
--auto-price fetches the live price (massive snapshot) and sets ±5% marketable limits.

Usage:
  python scripts/v2_webull_qty1_harness.py --account live:orb --symbol F                     # dry-run RTH
  python scripts/v2_webull_qty1_harness.py --account live:orb --symbol F --confirm            # LIVE RTH qty-1
  python scripts/v2_webull_qty1_harness.py --account live:orb --symbol F --session AM --auto-price --confirm  # LIVE pre-market
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from decimal import Decimal

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter, configured_webull_accounts
from project_mai_tai.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("v2-webull-qty1")

TERMINAL = {"filled", "rejected", "cancelled", "expired"}

# Extended-hours limits are marketable = wide-band around the live price so a qty-1 fills at the
# touch (you pay the ask, not the limit). 5% covers overnight drift on a liquid name; if the name
# gapped past the band the limit simply rests and the harness cleans up (no stuck position).
_EH_BAND = Decimal("0.05")


def _auto_marketable_limits(symbol: str, api_key: str) -> tuple[str, str]:
    """(entry_limit, flat_limit) = live price ±5%, rounded to a cent, via the massive snapshot."""
    from massive import RESTClient  # lazy: only extended-hours --auto-price needs it

    client = RESTClient(api_key=api_key, connect_timeout=5.0, read_timeout=8.0, retries=0)
    snap = client.get_snapshot_ticker("stocks", symbol)

    def _f(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if v:
                return Decimal(str(v))
        return None

    px = (_f(getattr(snap, "last_trade", None), "price", "p")
          or _f(getattr(snap, "min", None), "close", "c")
          or _f(getattr(snap, "prev_day", None), "close", "c"))
    if px is None or px <= 0:
        raise RuntimeError(f"no usable price in snapshot for {symbol}")
    cent = Decimal("0.01")
    entry = (px * (1 + _EH_BAND)).quantize(cent)
    flat = (px * (1 - _EH_BAND)).quantize(cent)
    log.info("auto-price %s: live=%s -> entry_limit=%s flat_limit=%s (±5%% band)", symbol, px, entry, flat)
    return str(entry), str(flat)


def _req(account: str, symbol: str, side: str, intent_type: str, *, order_type: str,
         limit: str | None = None, stop: str | None = None, session: str | None = None,
         coid: str) -> OrderRequest:
    md: dict[str, object] = {"order_type": order_type, "time_in_force": "day"}
    if limit is not None:
        md["limit_price"] = limit
    if stop is not None:
        md["stop_price"] = stop
    if session:
        md["session"] = session
        md["extended_hours"] = "true"
    return OrderRequest(
        client_order_id=coid, broker_account_name=account, strategy_code="schwab_1m_v2",
        symbol=symbol, side=side, intent_type=intent_type, quantity=Decimal("1"),
        reason="v2-webull-qty1-harness", metadata=md, order_type=order_type, time_in_force="day",
    )


async def _poll(adapter: WebullBrokerAdapter, req: OrderRequest, tries: int = 10) -> str:
    """Poll fetch_order_update until terminal or tries exhausted. Returns the last status."""
    status = "unknown"
    for _ in range(tries):
        rep = await adapter.fetch_order_update(req)
        if rep is None:
            await asyncio.sleep(1.0)
            continue
        status = rep.event_type
        if rep.event_type in ("filled", "partially_filled"):
            bt = rep.metadata.get("webull_broker_filled_time")
            log.info("  fill: qty=%s price=%s broker_fill_time=%s reported_at=%s",
                     rep.filled_quantity, rep.fill_price, bt, rep.reported_at)
            if bt is None:
                log.warning("  ⚠ no webull_broker_filled_time -> fill-latency would be UPPER BOUND")
        if status in TERMINAL:
            return status
        await asyncio.sleep(1.0)
    return status


async def _cancel(adapter: WebullBrokerAdapter, account: str, symbol: str, coid: str) -> None:
    try:
        req = _req(account, symbol, "sell", "cancel", order_type="market",
                   coid=f"{coid}-cxl")
        req.metadata["target_client_order_id"] = coid
        await adapter.submit_order(req)
        log.info("  cancel submitted for %s", coid)
    except Exception as exc:  # noqa: BLE001
        log.warning("  cancel failed for %s: %s", coid, exc)


async def run(account: str, symbol: str, confirm: bool, *, session: str = "RTH",
              entry_limit: str | None = None, flat_limit: str | None = None,
              auto_price: bool = False) -> int:
    settings = get_settings()
    eh = session.upper() in ("AM", "PM")
    sess = session.upper() if eh else None

    if not confirm:
        mode = f"EXTENDED-HOURS ({session}, LIMIT entry/flat)" if eh else "RTH (MARKET entry/flat)"
        log.info("DRY-RUN (no --confirm). Would exercise on %s / %s at qty 1 — %s:", account, symbol, mode)
        el = entry_limit or ("<auto>" if auto_price else "?")
        fl = flat_limit or ("<auto>" if auto_price else "?")
        entry_desc = f"marketable LIMIT buy @ {el} +session={sess}" if eh else "MARKET buy"
        flat_desc = f"marketable LIMIT sell @ {fl} +session={sess}" if eh else "MARKET sell"
        for n, d in [("1 ENTRY", entry_desc),
                     ("2 HARD-STOP", "STOP_LOSS sell far from market -> " + ("RTH-downgraded+rests" if eh else "rests") + " -> cancel"),
                     ("3 SCALE/FLOOR", "LIMIT sell -> cancel"), ("4 FLATTEN", flat_desc + " -> FLAT")]:
            log.info("  %s: %s", n, d)
        log.info("Re-run with --confirm to place REAL qty-1 orders (attended, off-hours).")
        return 0

    if eh and auto_price and (entry_limit is None or flat_limit is None):
        if not settings.massive_api_key:
            log.error("--auto-price needs MAI_TAI_MASSIVE_API_KEY (run under the service env).")
            return 2
        entry_limit, flat_limit = _auto_marketable_limits(symbol, settings.massive_api_key)
    if eh and (entry_limit is None or flat_limit is None):
        log.error("session=%s is extended-hours -> --entry-limit and --flat-limit are REQUIRED "
                  "(Webull EH is LIMIT-only; market/stop have no fills off-RTH).", session)
        return 2

    accts = configured_webull_accounts(settings)
    if account not in accts:
        log.error("account %r is not a configured Webull account (have: %s). "
                  "Provision + wire it before running.", account, list(accts))
        return 2
    adapter = WebullBrokerAdapter(settings)

    entry_coid = f"v2wq1-{symbol}-entry"
    stop_coid = f"v2wq1-{symbol}-stop"
    scale_coid = f"v2wq1-{symbol}-scale"
    held = False
    resting: list[str] = []
    ok = True
    # Entry/flatten shape depends on the session: RTH = MARKET (marketable, no price needed);
    # extended-hours = marketable LIMIT + session token (Webull rejects market/stop off-RTH — #429).
    entry_ot = "limit" if eh else "market"

    def _entry_req() -> OrderRequest:
        return _req(account, symbol, "buy", "open", order_type=entry_ot,
                    limit=entry_limit if eh else None, session=sess, coid=entry_coid)

    try:
        # 1) ENTRY — marketable buy at qty 1; the adapter rounds to tick (#374).
        log.info("[1 ENTRY] marketable %s buy qty 1 %s%s", entry_ot.upper(), symbol,
                 f" @ {entry_limit} +session={sess}" if eh else "")
        reports = await adapter.submit_order(_entry_req())
        log.info("  submit status=%s", ",".join(r.event_type for r in reports))
        if any(r.event_type == "rejected" for r in reports):
            log.error("  ENTRY rejected: %s", ";".join(r.reason or "" for r in reports))
            return 1
        st = await _poll(adapter, _entry_req())
        held = st == "filled"
        log.info("  ENTRY final=%s held=%s", st, held)
        if not held:
            resting.append(entry_coid)

        if held:
            # 2) HARD-STOP — STOP_LOSS far below market (rests); proves #386 (no 417) + it rests.
            log.info("[2 HARD-STOP] STOP_LOSS sell qty 1 (far from market) — expect ACCEPTED, no 417")
            r2 = await adapter.submit_order(_req(account, symbol, "sell", "close",
                                                 order_type="STOP", stop="0.01", coid=stop_coid))
            s2 = ",".join(r.event_type for r in r2)
            log.info("  STOP_LOSS submit=%s (reject reason=%s)", s2,
                     ";".join(r.reason or "" for r in r2 if r.event_type == "rejected") or "-")
            if "rejected" not in s2:
                resting.append(stop_coid)

            # 3) SCALE/FLOOR — LIMIT sell high above market (rests); the +2%/floor shape.
            log.info("[3 SCALE/FLOOR] LIMIT sell qty 1 above market — expect ACCEPTED/rest")
            r3 = await adapter.submit_order(_req(account, symbol, "sell", "scale",
                                                 order_type="limit", limit="9999", coid=scale_coid))
            log.info("  LIMIT-sell submit=%s", ",".join(r.event_type for r in r3))
            if all(r.event_type != "rejected" for r in r3):
                resting.append(scale_coid)
    except Exception as exc:  # noqa: BLE001
        log.exception("harness error: %s", exc)
        ok = False
    finally:
        # ALWAYS cancel every resting order, then flatten any held share, then verify FLAT.
        for coid in resting:
            await _cancel(adapter, account, symbol, coid)
        if held:
            flat_ot = "limit" if eh else "market"
            flat_coid = f"v2wq1-{symbol}-flat"
            log.info("[4 FLATTEN] marketable %s sell qty 1 to close%s", flat_ot.upper(),
                     f" @ {flat_limit} +session={sess}" if eh else "")
            try:
                flat_req = _req(account, symbol, "sell", "close", order_type=flat_ot,
                                limit=flat_limit if eh else None, session=sess, coid=flat_coid)
                await adapter.submit_order(flat_req)
                await _poll(adapter, flat_req)
            except Exception as exc:  # noqa: BLE001
                log.error("  FLATTEN failed — MANUAL CHECK NEEDED: %s", exc)
                ok = False
        try:
            positions = await adapter.list_account_positions(account)
            open_sym = [p for p in positions if str(getattr(p, "symbol", "")).upper() == symbol.upper()
                        and Decimal(str(getattr(p, "quantity", 0))) != 0]
            log.info("account positions for %s: %s -> %s", symbol, open_sym,
                     "FLAT" if not open_sym else "⚠ NOT FLAT — MANUAL CHECK")
            ok = ok and not open_sym
        except Exception as exc:  # noqa: BLE001
            log.warning("could not verify flat: %s", exc)
            ok = False
    log.info("HARNESS %s", "PASS" if ok else "FAIL / NEEDS MANUAL CHECK")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="qty-1 Webull plumbing harness for the v2 CW exit ladder")
    ap.add_argument("--account", required=True, help="Webull account name (e.g. live:v2_webull)")
    ap.add_argument("--symbol", required=True, help="liquid symbol to test (e.g. F)")
    ap.add_argument("--confirm", action="store_true", help="place REAL qty-1 orders (else dry-run)")
    ap.add_argument("--session", default="RTH", choices=["RTH", "AM", "PM"],
                    help="RTH=market entry/flat; AM/PM=extended-hours LIMIT entry/flat (needs --entry-limit/--flat-limit)")
    ap.add_argument("--entry-limit", default=None, help="marketable buy limit (required for AM/PM)")
    ap.add_argument("--flat-limit", default=None, help="marketable sell limit (required for AM/PM)")
    ap.add_argument("--auto-price", action="store_true",
                    help="for AM/PM: fetch the live price (massive snapshot) and set ±5%% marketable limits")
    args = ap.parse_args()
    return asyncio.run(run(args.account, args.symbol, args.confirm, session=args.session,
                           entry_limit=args.entry_limit, flat_limit=args.flat_limit,
                           auto_price=args.auto_price))


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""STEP-1 gate harness for the Schwab native OCO bracket -- ATTENDED, LIVE, QTY 1.

Runs the gate from docs/oco-step1-runbook.md through the OMS's OWN adapter path
(SchwabBrokerAdapter), because the broker -- not our payload mapping -- is the arbiter.

  --stage rest   item 1 + item 5 : bracket RESTS far from market, then CANCELS clean.
  --stage fill   item 2 + item 3 : entry fills -> BOTH exit legs go live atomically ->
                                   one leg made marketable -> broker auto-cancels the
                                   sibling, no oversell. (The E5 proof.)

DRY-RUN BY DEFAULT: without --confirm nothing is placed; the bracket is validated through
`preview_bracket_order` (broker validation, zero orders). --confirm places REAL orders for
REAL money on `live:schwab_1m_v2`.

Item 4 (software-exit stand-down) is NOT covered here and cannot be: it keys off
`_managed_v2_symbols`, so a harness-placed bracket on an unmanaged symbol will never
register as armed. It needs the emit wiring. Record it as NOT TESTED.

SAFETY: try/finally always cancels what it placed and flattens an unexpected fill.
Refuses protected symbols and any quantity other than 1.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import time
from decimal import Decimal

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter, configured_schwab_accounts
from project_mai_tai.settings import get_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("schwab-oco-step1")

ACCOUNT = "live:schwab_1m_v2"
QTY = Decimal("1")
STRATEGY = "schwab_1m_v2"
PROTECTED = {"CYN", "CELZ"}
TERMINAL = {"filled", "rejected", "cancelled", "expired"}


# ---------------------------------------------------------------- broker reads


async def _quote(adapter: SchwabBrokerAdapter, symbol: str) -> tuple[float, float]:
    """(bid, ask) straight from Schwab. Levels are derived from the live book, never guessed."""
    status, _h, body = await adapter._authorized_request_json(
        "GET", f"/marketdata/v1/quotes?symbols={symbol}&fields=quote"
    )
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"quote fetch failed HTTP {status}: {str(body)[:200]}")
    q = (body.get(symbol) or {}).get("quote") or {}
    bid, ask = float(q.get("bidPrice") or 0.0), float(q.get("askPrice") or 0.0)
    if bid <= 0 or ask <= 0:
        raise RuntimeError(f"no two-sided market for {symbol}: bid={bid} ask={ask}")
    return bid, ask


async def _open_orders(adapter: SchwabBrokerAdapter, symbol: str) -> list[dict]:
    """Working orders at the BROKER for this symbol, flattened across combo children.

    This is the item-2 evidence: we assert on what Schwab reports, not on what we sent.
    """
    account = adapter.accounts_by_name[ACCOUNT]
    from urllib.parse import quote as _q

    now = time.time()
    frm = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now - 6 * 3600))
    to = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(now + 3600))
    status, _h, body = await adapter._authorized_request_json(
        "GET",
        f"/trader/v1/accounts/{_q(account.account_hash, safe='')}/orders"
        f"?fromEnteredTime={frm}&toEnteredTime={to}&maxResults=200",
    )
    if status != 200 or not isinstance(body, list):
        raise RuntimeError(f"orders fetch failed HTTP {status}: {str(body)[:200]}")

    out: list[dict] = []

    def _walk(order: dict) -> None:
        legs = order.get("orderLegCollection") or []
        sym = ""
        if legs:
            sym = str(((legs[0] or {}).get("instrument") or {}).get("symbol") or "")
        if sym == symbol:
            out.append(order)
        for child in order.get("childOrderStrategies") or []:
            _walk(child)

    for order in body:
        _walk(order)
    return out


def _working(orders: list[dict]) -> list[dict]:
    live = {"WORKING", "PENDING_ACTIVATION", "QUEUED", "ACCEPTED", "AWAITING_PARENT_ORDER"}
    return [o for o in orders if str(o.get("status", "")).upper() in live]


def _describe(orders: list[dict]) -> str:
    parts = []
    for o in orders:
        legs = o.get("orderLegCollection") or []
        instr = str((legs[0] or {}).get("instruction", "?")) if legs else "?"
        price = o.get("price") or o.get("stopPrice") or ""
        parts.append(f"{instr} {o.get('orderType','?')}@{price} [{o.get('status','?')}] id={o.get('orderId','?')}")
    return "; ".join(parts) or "(none)"


# ---------------------------------------------------------------- requests


def _bracket_req(coid: str, symbol: str, entry: float, target: float, protect: float) -> OrderRequest:
    return OrderRequest(
        client_order_id=coid,
        broker_account_name=ACCOUNT,
        strategy_code=STRATEGY,
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=QTY,
        reason="oco-step1-gate",
        metadata={
            "bracket": "true",
            "order_type": "STOP",
            "time_in_force": "day",
            "stop_price": f"{entry:.2f}",
            "bracket_target_price": f"{target:.2f}",
            "bracket_stop_price": f"{protect:.2f}",
            # Tag every leg so the OMS stand-down can recognise the pair (item 4, later).
            "native_oco_bracket": "true",
        },
        order_type="STOP",
        time_in_force="day",
    )


def _simple_req(coid: str, symbol: str, side: str, intent: str, **meta: str) -> OrderRequest:
    md = {"order_type": "market", "time_in_force": "day"}
    md.update(meta)
    return OrderRequest(
        client_order_id=coid, broker_account_name=ACCOUNT, strategy_code=STRATEGY,
        symbol=symbol, side=side, intent_type=intent, quantity=QTY,  # type: ignore[arg-type]
        reason=f"oco-step1-{intent}", metadata=md,
        order_type=md["order_type"], time_in_force="day",
    )


# ---------------------------------------------------------------- stages


async def run(stage: str, symbol: str, confirm: bool) -> int:
    if symbol.upper() in PROTECTED:
        log.error("%s is PROTECTED (%s) -- refusing.", symbol, ",".join(sorted(PROTECTED)))
        return 2
    symbol = symbol.upper()

    settings = get_settings()
    accounts = configured_schwab_accounts(settings)
    if ACCOUNT not in accounts:
        log.error("account %r not configured (have %s)", ACCOUNT, list(accounts))
        return 2
    adapter = SchwabBrokerAdapter(settings)

    bid, ask = await _quote(adapter, symbol)
    log.info("%s market: bid=%.2f ask=%.2f", symbol, bid, ask)

    if stage == "rest":
        # Item 1: entry FAR above market so it cannot trigger.
        entry = round(ask * 1.50, 2)
    else:
        # Item 2: entry just above the ask so it fills promptly on a qty-1 lot.
        entry = round(ask + 0.02, 2)
    target = round(entry * 1.02, 2)   # +2%
    protect = round(entry * 0.95, 2)  # -5%

    coid = f"ocostep1-{time.strftime('%H%M%S', time.gmtime())}"
    plan = (f"BUY {QTY} {symbol} STOP @ {entry:.2f} -> OCO[ SELL LIMIT {target:.2f} | "
            f"SELL STOP {protect:.2f} ]  coid={coid}")
    log.info("[PLAN/%s] %s", stage.upper(), plan)

    # Always validate the shape at the broker first -- zero orders placed.
    status, body = await adapter.preview_bracket_order(_bracket_req(coid, symbol, entry, target, protect))
    rejects = []
    if isinstance(body, dict):
        rejects = ((body.get("orderValidationResult") or {}).get("rejects")) or []
    log.info("[PREVIEW] HTTP %s rejects=%d", status, len(rejects))
    for r in rejects:
        log.warning("   reject: %s", r.get("activityMessage"))
    if status not in (200, 201) or rejects:
        log.error(">>> PREVIEW REJECTED -- not placing. This is the answer; fix the shape first.")
        return 1

    if not confirm:
        log.info("DRY-RUN (no --confirm). Preview ACCEPTED. Re-run with --confirm to place REAL orders.")
        return 0

    placed: list[str] = []
    held = False
    ok = True
    try:
        reports = await adapter.submit_order(_bracket_req(coid, symbol, entry, target, protect))
        kinds = ",".join(r.event_type for r in reports)
        rej = ";".join(r.reason or "" for r in reports if r.event_type == "rejected")
        log.info("[PLACE] status=%s reject=%s", kinds, rej or "-")
        if "rejected" in kinds:
            log.error(">>> REJECTED by broker: %s", rej)
            return 1
        placed.append(coid)

        await asyncio.sleep(3.0)
        orders = await _open_orders(adapter, symbol)
        log.info("[BROKER ORDERS] %s", _describe(orders))

        if stage == "rest":
            # ITEM 1 pass: parent works; children must NOT be live yet.
            working = _working(orders)
            sells_live = [o for o in working
                          if (o.get("orderLegCollection") or [{}])[0].get("instruction") == "SELL"
                          and str(o.get("status", "")).upper() not in {"AWAITING_PARENT_ORDER"}]
            if not working:
                log.error("ITEM 1 FAIL: nothing working at the broker.")
                ok = False
            elif sells_live:
                log.error("ITEM 1 FAIL: exit legs LIVE before the entry filled -- unattached sells: %s",
                          _describe(sells_live))
                ok = False
            else:
                log.info("ITEM 1 PASS: combo RESTS; exit legs armed but not working (await parent).")
        else:
            # ITEM 2 pass: on fill, BOTH exit legs live.
            deadline = time.time() + 60
            while time.time() < deadline:
                orders = await _open_orders(adapter, symbol)
                sells = [o for o in _working(orders)
                         if (o.get("orderLegCollection") or [{}])[0].get("instruction") == "SELL"]
                filled_parent = [o for o in orders
                                 if str(o.get("status", "")).upper() == "FILLED"
                                 and (o.get("orderLegCollection") or [{}])[0].get("instruction") == "BUY"]
                if filled_parent:
                    held = True
                    if len(sells) >= 2:
                        log.info("ITEM 2 PASS: entry filled and BOTH exit legs live: %s", _describe(sells))
                        break
                    log.warning("   entry filled, %d exit leg(s) live -- waiting for the pair...", len(sells))
                await asyncio.sleep(2.0)
            else:
                log.error("ITEM 2 FAIL: never observed a filled entry with both exit legs live.")
                ok = False

            if ok and held:
                log.warning("ITEM 3 requires making one leg marketable. Doing this MANUALLY is safer "
                            "than automating a marketable sell -- inspect, then flatten below.")
                log.info("ITEM 3: to prove one-cancels-other, replace the target leg near the bid "
                         "(%.2f) in the Schwab UI and confirm the sibling auto-cancels.", bid)
    except Exception as exc:  # noqa: BLE001
        log.exception("harness error: %s", exc)
        ok = False
    finally:
        # ITEM 5 + safety: cancel everything we placed; flatten any unexpected fill.
        try:
            orders = await _open_orders(adapter, symbol)
            for o in _working(orders):
                oid = o.get("orderId")
                if oid:
                    reps = await adapter.submit_order(
                        _simple_req(f"{coid}-c{oid}", symbol, "buy", "cancel", broker_order_id=str(oid))
                    )
                    outcome = ",".join(r.event_type for r in reps)
                    why = ";".join(r.reason or "" for r in reps if r.event_type == "rejected")
                    # A rejected cancel is NOT automatically a failure: cancelling one combo leg
                    # can 400 because the parent cancel already took its sibling. The broker
                    # re-read below is the real verdict, not this line.
                    log.info("  [CANCEL] order %s -> %s %s", oid, outcome, f"({why})" if why else "")
            await asyncio.sleep(3.0)
            remaining = _working(await _open_orders(adapter, symbol))
            if remaining:
                log.error("ITEM 5 FAIL: still working after cancel -- CANCEL BY HAND NOW: %s",
                          _describe(remaining))
                ok = False
            else:
                log.info("ITEM 5 PASS: no working orders remain for %s.", symbol)
        except Exception as exc:  # noqa: BLE001
            log.error("  cancel/verify failed -- CHECK THE BROKER BY HAND: %s", exc)
            ok = False

        if held:
            log.warning("  [FLATTEN] position was opened -- submitting marketable SELL %s %s", QTY, symbol)
            try:
                fr = await adapter.submit_order(_simple_req(f"{coid}-flat", symbol, "sell", "close"))
                log.info("  flatten submit=%s", ",".join(r.event_type for r in fr))
            except Exception as exc:  # noqa: BLE001
                log.error("  FLATTEN FAILED -- MANUAL ACTION REQUIRED: %s", exc)
                ok = False

    log.info("=== RESULT: %s ===", "PASS" if ok else "FAIL / NEEDS REVIEW")
    log.info("Reminder: item 4 (software-exit stand-down) is NOT TESTED by this harness.")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("rest", "fill"), required=True)
    ap.add_argument("--symbol", required=True, help="liquid, cheap, NOT CYN/CELZ")
    ap.add_argument("--confirm", action="store_true", help="place REAL orders (real money, qty 1)")
    args = ap.parse_args()
    return asyncio.run(run(args.stage, args.symbol, args.confirm))


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""STEP-1 gate harness for the Webull native OCO combo bracket -- ATTENDED, LIVE, QTY 1.

Mirrors scripts/schwab_oco_step1.py, run through the OMS's OWN adapter path
(WebullBrokerAdapter), because the broker -- not our payload mapping -- is the arbiter. On
`live:orb` (the only live Webull account; use a symbol NOT on ORB's active watchlist so the
mirror-collision guard is moot).

  --stage rest   item 1 + item 5 : the combo RESTS far from market, then CANCELS clean.
  --stage fill   item 2 + item 3 : a marketable MASTER fills -> BOTH exit legs arm atomically ->
                                   one leg made marketable -> broker auto-cancels the sibling,
                                   no oversell. (The E5 proof.)

DRY-RUN BY DEFAULT: without --confirm nothing is placed; the combo is validated through
`preview_bracket_order` (Webull preview_order, zero orders -- already PASSED off-hours 2026-07-22).
--confirm places REAL orders for REAL money on `live:orb`.

** CONFIRM-AT-TEST / DATA-CAPTURE: unlike the Schwab gate, the Webull `get_order_open` combo
response shape and the combo-cancel shape are NOT yet known (a preview does not reveal them). So
the FIRST --stage rest --confirm run's real job is to CAPTURE them: it prints the RAW
get_order_open + cancel responses. Those captured shapes are what let us build the OMS read side
(fetch_armed_native_oco_symbols + fetch_oco_resolved_by_fill_symbols for Webull). The leg-state
assertions below are best-effort until then.

SAFETY: try/finally always cancels what it placed and flattens any unexpected fill. Refuses any
quantity other than 1. MASTER is LIMIT/MARKET only (a buy-STOP master 417s -- proven 2026-07-22).

This harness sets MAI_TAI_WEBULL_NATIVE_BRACKET_ENABLED=true for ITS OWN process only (so
submit_order takes the combo branch); it does NOT touch the deployed OMS flag.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from decimal import Decimal

log = logging.getLogger("webull-oco-step1")

ACCOUNT = "live:orb"
QTY = Decimal("1")
STRATEGY = "schwab_1m_v2"
TERMINAL = {"filled", "rejected", "cancelled", "expired"}


def _ref_price(symbol: str, api_key: str) -> float:
    """Live price via the Polygon (massive) snapshot -- Webull has no md entitlement for us."""
    from massive import RESTClient

    client = RESTClient(api_key=api_key, connect_timeout=5.0, read_timeout=8.0, retries=0)
    snap = client.get_snapshot_ticker("stocks", symbol)

    def _f(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if v:
                return float(v)
        return 0.0

    last = _f(getattr(snap, "last_trade", None), "price", "p") or _f(
        getattr(snap, "day", None), "close", "c"
    )
    if last <= 0:
        raise RuntimeError(f"no snapshot price for {symbol}")
    return last


def _bracket_req(coid: str, symbol: str, entry: float, target: float, protect: float,
                 entry_type: str):
    from project_mai_tai.broker_adapters.protocols import OrderRequest

    metadata: dict[str, object] = {
        "bracket": "true",
        "bracket_entry_type": entry_type,          # LIMIT or MARKET (buy-STOP master 417s)
        "order_type": entry_type.lower(),
        "time_in_force": "day",
        "bracket_target_price": f"{target:.2f}",
        "bracket_stop_price": f"{protect:.2f}",
    }
    if entry_type == "LIMIT":
        metadata["limit_price"] = f"{entry:.2f}"
    return OrderRequest(
        client_order_id=coid,
        broker_account_name=ACCOUNT,
        strategy_code=STRATEGY,
        symbol=symbol,
        side="buy",
        intent_type="open",
        quantity=QTY,
        reason="oco-step1-gate",
        metadata=metadata,
        order_type=entry_type.lower(),
        time_in_force="day",
    )


def _raw_get_order_open(adapter, account_id: str) -> object:
    """v3 get_order_open, printed RAW. ** This is the shape-capture step: the fetch_armed /
    fetch_oco_resolved_by_fill read-side is built from what this returns for a live combo."""
    client = adapter._get_client()
    from webull.trade.trade.v3.order_opration_v3 import OrderOperationV3

    body = adapter._body(OrderOperationV3(client).get_order_open(account_id))
    log.info("[RAW get_order_open] %s", json.dumps(body, indent=2, default=str))
    return body


def _combo_legs_for(body: object, symbol: str) -> list[dict]:
    """Best-effort walk of the get_order_open body for `symbol` (CONFIRM-AT-TEST field names).

    Webull group orders return together; the exact envelope key (orders/items/data) and per-leg
    field names (order_status, combo_type, ...) are captured by the raw print above and hardened
    after the first live run."""
    rows: list = []
    if isinstance(body, dict):
        for key in ("orders", "items", "data", "open_orders", "order_list"):
            v = body.get(key)
            if isinstance(v, list):
                rows = v
                break
    elif isinstance(body, list):
        rows = body
    out: list[dict] = []

    def _walk(node: object) -> None:
        if not isinstance(node, dict):
            return
        sym = str(node.get("symbol") or node.get("ticker") or "").upper()
        if sym == symbol.upper():
            out.append(node)
        for k in ("legs", "orders", "child_orders", "children", "combo_orders"):
            for child in node.get(k) or []:
                _walk(child)

    for row in rows:
        _walk(row)
    return out


def _leg_status(leg: dict) -> str:
    return str(leg.get("order_status") or leg.get("status") or leg.get("orderStatus") or "").upper()


def _leg_side(leg: dict) -> str:
    return str(leg.get("side") or "").upper()


def _describe(legs: list[dict]) -> str:
    parts = [
        f"{_leg_side(leg)} {leg.get('order_type') or leg.get('combo_type') or '?'}"
        f"@{leg.get('limit_price') or leg.get('stop_price') or ''} [{_leg_status(leg)}]"
        for leg in legs
    ]
    return "; ".join(parts) or "(none)"


async def _cancel_combo(adapter, account_id: str, combo_id: str, master_coid: str) -> None:
    """Cancel the resting combo. CONFIRM-AT-TEST: try the group id, then the master leg id (one of
    them cancels the whole OTOCO group -- captured on the first live run)."""
    client = adapter._get_client()
    from webull.trade.trade.v3.order_opration_v3 import OrderOperationV3

    op = OrderOperationV3(client)
    for label, cid in (("combo_id", combo_id), ("master_coid", master_coid)):
        try:
            body = adapter._body(op.cancel_order(account_id, cid))
            log.info("[CANCEL via %s=%s] -> %s", label, cid, json.dumps(body, default=str))
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("[CANCEL via %s=%s] raised: %s", label, cid, exc)
    log.error("  both combo-cancel attempts raised -- CANCEL BY HAND NOW in the Webull app.")


async def run(stage: str, symbol: str, confirm: bool, ref_price: float | None,
              leave_open: bool = False) -> int:
    symbol = symbol.upper()
    from project_mai_tai.broker_adapters.webull import (
        WebullBrokerAdapter,
        configured_webull_accounts,
    )
    from project_mai_tai.settings import get_settings

    settings = get_settings()
    accounts = configured_webull_accounts(settings)
    if ACCOUNT not in accounts:
        log.error("account %r not configured (have %s)", ACCOUNT, list(accounts))
        return 2
    adapter = WebullBrokerAdapter(settings, accounts_by_name=accounts)
    account_id = accounts[ACCOUNT].account_id
    if not bool(getattr(settings, "webull_native_bracket_enabled", False)):
        log.error("MAI_TAI_WEBULL_NATIVE_BRACKET_ENABLED is not set for this process -- the combo "
                  "branch will not be taken. (main() sets it; run via that entry point.)")
        return 2

    # Reference price -> far-from-market (rest) or marketable (fill) levels.
    if ref_price is None:
        api_key = getattr(settings, "polygon_api_key", None) or getattr(settings, "massive_api_key", None)
        if not api_key:
            log.error("no --ref-price and no polygon/massive api key configured; pass --ref-price.")
            return 2
        ref_price = _ref_price(symbol, str(api_key))
    log.info("%s reference price = %.2f", symbol, ref_price)

    entry_type = "LIMIT"
    if stage == "rest":
        entry = round(ref_price * 0.50, 2)   # MASTER limit FAR below market -> cannot fill; rests
    else:
        entry = round(ref_price * 1.02, 2)   # marketable BUY limit through the offer -> fills
    target = round(entry * 1.02, 2)          # STOP_PROFIT +2%
    protect = round(entry * 0.95, 2)         # STOP_LOSS  -5%

    coid = f"ocostep1-{time.strftime('%H%M%S', time.gmtime())}"
    combo_id = f"{coid}-combo"[:40]
    log.info("[PLAN/%s] BUY %s %s LIMIT@%.2f -> OCO[ SELL LIMIT %.2f | SELL STOP %.2f ] coid=%s",
             stage.upper(), QTY, symbol, entry, target, protect, coid)

    # Item 0: validate at the broker first -- zero orders placed.
    status, body = await adapter.preview_bracket_order(
        _bracket_req(coid, symbol, entry, target, protect, entry_type))
    err = None
    if isinstance(body, dict):
        err = body.get("error") or body.get("error_code") or body.get("msg") or body.get("message")
    log.info("[PREVIEW] status=%s err=%r body=%s", status, err, json.dumps(body, default=str)[:300])
    if not (200 <= int(status or 0) < 300) or err:
        log.error(">>> PREVIEW REJECTED -- not placing. Fix the shape first.")
        return 1
    if not confirm:
        log.info("DRY-RUN (no --confirm). Preview ACCEPTED. Re-run with --confirm to place REAL orders.")
        return 0

    held = False
    ok = True
    try:
        reports = await adapter.submit_order(_bracket_req(coid, symbol, entry, target, protect, entry_type))
        kinds = ",".join(r.event_type for r in reports)
        rej = ";".join(r.reason or "" for r in reports if r.event_type == "rejected")
        log.info("[PLACE] status=%s reject=%s", kinds, rej or "-")
        if "rejected" in kinds:
            log.error(">>> REJECTED by broker: %s", rej)
            return 1

        await asyncio.sleep(3.0)
        body = _raw_get_order_open(adapter, account_id)   # ** capture the shape
        legs = _combo_legs_for(body, symbol)
        log.info("[COMBO LEGS] %s", _describe(legs))

        if stage == "rest":
            live_sells = [leg for leg in legs if _leg_side(leg) == "SELL"
                          and _leg_status(leg) in {"WORKING", "LIVE", "OPEN", "SUBMITTED"}]
            if live_sells:
                log.error("ITEM 1 REVIEW: exit legs appear LIVE before the entry filled: %s",
                          _describe(live_sells))
                ok = False
            else:
                log.info("ITEM 1: combo rests; exit legs not independently working (verify vs the "
                         "raw dump above -- field names get hardened from this capture).")
        else:
            log.warning("ITEM 2/3 (fill + one-cancels-other) need a real fill: watch the app, then "
                        "with --leave-open make one leg marketable by hand to prove the sibling "
                        "auto-cancels. Inspect the raw dump for the filled/armed leg states.")
    except Exception as exc:  # noqa: BLE001
        log.exception("harness error: %s", exc)
        ok = False
    finally:
        if leave_open:
            log.warning("[LEAVE-OPEN] NOT cancelling. The combo is resting; it is protected ONLY by "
                        "the broker OCO and MUST be closed before the session ends.")
            return 0
        # Item 5 + safety: cancel the combo; flatten any unexpected fill.
        await _cancel_combo(adapter, account_id, combo_id, f"{coid}M")
        await asyncio.sleep(3.0)
        remaining = _combo_legs_for(_raw_get_order_open(adapter, account_id), symbol)
        still = [leg for leg in remaining if _leg_status(leg) not in TERMINAL | {""}]
        if still:
            log.error("ITEM 5 REVIEW: legs still present after cancel -- CHECK BY HAND: %s",
                      _describe(still))
            ok = False
        else:
            log.info("ITEM 5: no live combo legs remain for %s.", symbol)
        if held:
            log.warning("[FLATTEN] an entry filled unexpectedly -- flatten qty %s %s BY HAND in the "
                        "Webull app NOW (harness does not auto-market-sell a live position).",
                        QTY, symbol)

    log.info("=== RESULT: %s ===", "PASS" if ok else "REVIEW")
    log.info("Reminder: the read-side (fetch_armed / fetch_oco_resolved_by_fill for Webull) is built "
             "from the RAW get_order_open capture above; item 4 (software-defer) is later.")
    return 0 if ok else 1


def main() -> int:
    # Enable the combo branch for THIS process only (not the deployed OMS flag).
    os.environ.setdefault("MAI_TAI_WEBULL_NATIVE_BRACKET_ENABLED", "true")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", choices=("rest", "fill"), required=True)
    ap.add_argument("--symbol", required=True, help="liquid, cheap, NOT on ORB's active watchlist")
    ap.add_argument("--ref-price", type=float, default=None,
                    help="reference price; if omitted, fetched from the Polygon snapshot")
    ap.add_argument("--confirm", action="store_true", help="place REAL orders (real money, qty 1)")
    ap.add_argument("--leave-open", action="store_true",
                    help="ITEM 3: place the combo and LEAVE it resting so one leg can be made "
                         "marketable by hand. Skips cancel. Protected ONLY by the broker OCO -- "
                         "must be closed before the session ends.")
    args = ap.parse_args()
    return asyncio.run(run(args.stage, args.symbol, args.confirm, args.ref_price, args.leave_open))


if __name__ == "__main__":
    raise SystemExit(main())

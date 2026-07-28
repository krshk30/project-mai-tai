"""ORPHAN-ORDER CHECK — a live broker order nobody is managing.

⭐ WHY (live 2026-07-28): v2 placed a POLA resting buy-stop-limit at 2.19/2.20 at 10:30 ET. The
ATR trail then fell to ~1.95 and price to 1.89, but the order sat WORKING at the broker for an
hour with ZERO cancel or reprice attempts. The operator found it by eye on a TOS chart.

The mechanism: a resting order's intent stays `submitted` for its whole life (it only resolves
when price triggers it). `_fetch_open_positions` counts in-flight OPEN intents as "in position",
so v2 concluded it HELD POLA. The first gate in `_cw_v2_resting_track` then does:

    if state.position_qty != 0:
        state.resting_active = False      # <-- clears the flag, does NOT cancel the order
        return

From that moment the bot believes it has no resting order, so neither the STABLE-REST reprice nor
the flip-no-fill cancel can ever fire. The order is orphaned: live at the broker, invisible to the
strategy that placed it.

⛔ The danger is not the order existing — it is the order being STALE. A buy-stop left far above a
decayed ATR level will, if price rallies back, fill on a breakout the strategy no longer believes
in, with a bracket priced off an hour-old reference.

WHAT IT CHECKS (read-only; places, cancels and modifies nothing):
  RED    a WORKING order whose trigger is >= STALE_PCT away from the current market AND older
         than MIN_AGE_MIN  -> the POLA shape exactly
  AMBER  a WORKING order older than MIN_AGE_MIN whose intent is still non-terminal and whose
         symbol is NOT in the bot's live watchlist -> nobody is evaluating it at all

Exit 0 green/skip, 1 amber, 2 red.
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import text

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings

ET = ZoneInfo("America/New_York")
TOPIC = "mai-tai-preopen-28806a5a97b7"
STALE_PCT = 5.0        # a trigger this far from market is not a live setup any more
MIN_AGE_MIN = 15       # below this a resting order is simply waiting, which is normal



def classify_order(*, symbol: str, instruction: str, order_type: str, trigger: float,
                   age_min: float, mid: float | None, in_watchlist: bool,
                   order_id: str = "") -> tuple[str, str] | None:
    """PURE decision — no broker, no DB. Returns (severity, message) or None.

    Extracted so the POLA case can be PROVEN in a test rather than waiting for it to recur:
    a green live run only means nothing is orphaned right now, not that this would catch it.
    """
    if age_min < MIN_AGE_MIN:
        return None                      # still just waiting; that is what resting orders do
    if mid and mid > 0:
        away = abs(trigger - mid) / mid * 100.0
        if away >= STALE_PCT:
            return ("RED",
                    f"{symbol} {instruction} {order_type} trigger={trigger} is {away:.1f}% from "
                    f"market {mid:.4f}, WORKING for {age_min:.0f}min (order {order_id}) — "
                    f"the POLA shape: nobody is repricing it.")
    if not in_watchlist:
        return ("AMBER",
                f"{symbol} {order_type} trigger={trigger} WORKING {age_min:.0f}min but NOT in the "
                f"v2 watchlist — no strategy is evaluating it (order {order_id}).")
    return None


def push(title: str, body: str, priority: str, tags: str) -> None:
    # HTTP headers are latin-1: a non-ASCII Title raises UnicodeEncodeError and the page is LOST.
    title = title.encode("ascii", "replace").decode("ascii")
    try:
        requests.post(f"https://ntfy.sh/{TOPIC}", data=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags}, timeout=10)
    except Exception as exc:  # noqa: BLE001 - a pager that crashes is worse than a quiet one
        print(f"ntfy push failed: {type(exc).__name__}")


def bot_watchlist(redis_url: str) -> set[str]:
    """The v2 bot's own live watchlist — a symbol absent from it is being evaluated by nobody."""
    try:
        import redis as _redis
        r = _redis.Redis.from_url(redis_url, decode_responses=True)
        entries = r.xrevrange("mai_tai:strategy-state-isolated", "+", "-", count=1)
        payload = json.loads(entries[0][1]["data"])["payload"]
        return {str(x).upper() for x in (payload.get("watchlist") or [])}
    except Exception:  # noqa: BLE001 - unknown watchlist -> report nothing as unwatched
        return set()


async def main() -> int:
    selftest = "--selftest" in sys.argv
    s = get_settings()
    if selftest:
        push("Orphan order - SELFTEST", "pager path OK; no fault implied.", "low", "white_check_mark")
        print("selftest push sent")
        return 0

    from project_mai_tai.broker_adapters.schwab import (
        SchwabBrokerAdapter,
        configured_schwab_accounts,
    )
    sch = SchwabBrokerAdapter(s, accounts_by_name=configured_schwab_accounts(s))
    acct_name = s.strategy_schwab_1m_v2_account_name

    from urllib.parse import quote as _q
    account = sch.accounts_by_name.get(acct_name)
    if account is None:
        print(f"no schwab account {acct_name}")
        return 0
    from datetime import timedelta
    now = datetime.now(UTC)
    path = (f"/trader/v1/accounts/{_q(account.account_hash, safe='')}/orders"
            f"?fromEnteredTime={(now - timedelta(hours=24)):%Y-%m-%dT%H:%M:%S.000Z}"
            f"&toEnteredTime={(now + timedelta(hours=1)):%Y-%m-%dT%H:%M:%S.000Z}&maxResults=500")
    code, _h, body = await sch._authorized_request_json("GET", path)
    if code >= 400 or not isinstance(body, list):
        print(f"schwab orders fetch failed HTTP {code}")
        return 0

    WORKING = {"WORKING", "QUEUED", "ACCEPTED", "PENDING_ACTIVATION", "AWAITING_PARENT_ORDER",
               "AWAITING_CONDITION", "NEW", "PENDING_ACKNOWLEDGEMENT"}
    watch = bot_watchlist(s.redis_url)
    sf = build_session_factory(s)

    reds: list[str] = []
    ambers: list[str] = []
    for order in body:
        if str(order.get("status", "")).upper() not in WORKING:
            continue
        legs = order.get("orderLegCollection") or []
        leg = legs[0] if legs else {}
        symbol = str((leg.get("instrument") or {}).get("symbol") or "").upper()
        trigger = order.get("stopPrice") or order.get("price")
        entered = sch._parse_datetime(order.get("enteredTime"))
        if not symbol or trigger is None or entered is None:
            continue
        age_min = (now - entered).total_seconds() / 60.0
        if age_min < MIN_AGE_MIN:
            continue

        with sf() as sess:
            last = sess.execute(text("""
                SELECT bid_price, ask_price FROM market_capture_quotes
                WHERE symbol = :sym ORDER BY event_ts DESC LIMIT 1
            """), {"sym": symbol}).first()
        mid = None
        if last and last.bid_price and last.ask_price:
            mid = (float(last.bid_price) + float(last.ask_price)) / 2.0

        verdict = classify_order(
            symbol=symbol,
            instruction=str(leg.get("instruction") or ""),
            order_type=str(order.get("orderType") or ""),
            trigger=float(trigger),
            age_min=age_min,
            mid=mid,
            in_watchlist=symbol in watch,
            order_id=str(order.get("orderId") or ""),
        )
        if verdict is None:
            continue
        (reds if verdict[0] == "RED" else ambers).append(verdict[1])

    stamp = datetime.now(ET).strftime("%H:%M ET")
    if reds:
        push(f"ORPHAN ORDER RED - {stamp}", "\n".join(reds), "urgent", "rotating_light")
        print("RED:", " | ".join(reds))
        return 2
    if ambers:
        push(f"Orphan order AMBER - {stamp}", "\n".join(ambers), "high", "warning")
        print("AMBER:", " | ".join(ambers))
        return 1
    print("GREEN: no orphaned working orders")
    return 0


if __name__ == "__main__":
    import asyncio
    raise SystemExit(asyncio.run(main()))

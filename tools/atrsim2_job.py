"""ATRSIM2 — the two paper tables the operator asked for (10-29): FIRSTBAR and FIRSTBAR+2min.

Simulation only, never orders. Runs every 5 minutes; writes ONE dashboard_snapshots row per
trading day (snapshot_type 'atr_sim2_day'), replacing that day's row each run. Rows of earlier
days are kept untouched — that is the archive. The ATR page shows today's row.

Rules (same as the 10-27 approved FIRSTBAR descriptions):
  signal  = a BUY flip of the ATR line on the bot's own stored 1-minute bars (04:00 ET seed)
  entry   = first round-lot trade at or above the flip bar's high + $0.01, after the flip bar
            closes, from 07:00 ET, before 15:45 ET and before the next SELL flip
  exits   = sell flip (first trade after that bar closes) / session end 20:00 ET,
            else target +5% (limit at the target, cent ceiling), else stop -8% (that trade's price)
  filter  = only BUY flips whose bar closes while the stock is CONFIRMED on the momentum scanner
            (scanner_confirmed_events CONFIRM..FADE), like the live ATR bot (operator 10-29); every
            ENTRY print must also be inside a CONFIRM..FADE window (coach 10-29: no fill before CONFIRM)
  +2min   = also sold at the first trade after 2 minutes if it never traded +1% in those 2 minutes
Trades are read only from each signal to its exit, so the cost does not grow with the day's tape.
"""
from __future__ import annotations

import bisect
import json
import math
import statistics
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail  # noqa: E402

ET = ZoneInfo("America/New_York")
SNAPSHOT_TYPE = "atr_sim2_day"
NOTIONAL = 600.0
# Same condition filter as the bot's bars: non-last-updating conditions and odd lots never fill.
EXCLUDED = {"2", "7", "10", "13", "15", "16", "20", "21", "22", "29", "33", "37", "38", "52", "53"}
PAGE = 5000


def eligible(conditions: str | None) -> bool:
    return not (set((conditions or "").split(",")) & EXCLUDED)


def ceil_cent(value: float) -> float:
    return math.ceil(round(value * 100, 6)) / 100


def flips_by_symbol(conn, anchor):
    rows = conn.execute(text(
        "select symbol, bar_time, open_price, high_price, low_price, close_price, volume "
        "from strategy_bar_history where strategy_code='schwab_1m_v2' and interval_secs=60 "
        "and bar_time >= :a and bar_time < :b order by symbol, bar_time"),
        # Coach 10-29: bounded to this trading day — an old day re-run must not read later days' bars.
        {"a": anchor, "b": anchor + timedelta(days=1)}).all()
    bars = {}
    for s, t, o, h, l, c, v in rows:
        bars.setdefault(s, []).append(Bar(int(t.timestamp() * 1000), float(o), float(h), float(l), float(c), int(v or 0)))
    out = {}
    for symbol, series in bars.items():
        rows = compute_atr_trail(series)
        LINES[symbol] = (series, rows)
        RANGES.pop(symbol, None)
        out[symbol] = [(series[i], r["flip"]) for i, r in enumerate(rows) if r["flip"]]
    return out


LINES = {}
RANGES = {}
WINDOWS = None  # set by run(): scanner CONFIRM..FADE windows of the day being simulated


def confirmed(symbol, ts):
    """Coach 10-29 (15 LIVE fills before CONFIRM): an order exists only while the stock is confirmed on the
    momentum scanner — the live bot cancels on a scanner drop — so an entry print must be inside a window."""
    return WINDOWS is None or on_scanner(WINDOWS, symbol, ts)


def continuous(symbol, t0, t1):
    """Coach 10-30 (TCRT 10-14): the order exists only if the stock stayed confirmed for the WHOLE stretch
    t0..t1 — a FADE gap with no print in it still cancels the live order."""
    return WINDOWS is None or any(start <= t0 and (end is None or t1 < end)
                                  for start, end in WINDOWS.get(symbol, ()))


def sane(symbol, ts, price):
    """Drop bad prints, causally (coach 10-30): a trade counts only within ±10% of the CLOSE of the last
    completed minute bar before it — never the finished bar of its own minute (that is later information).
    BIYA 10-07 had a $10.36 print on a ~$3 stock that would fake a fill."""
    closes = RANGES.get(symbol)
    if closes is None:
        series, _ = LINES.get(symbol, ([], None))
        closes = RANGES[symbol] = ([b.ts for b in series], [b.close for b in series])
    minute = int(ts.timestamp() // 60) * 60_000
    i = bisect.bisect_left(closes[0], minute) - 1  # last bar that STARTED before this print's minute
    return i < 0 or closes[1][i] * 0.9 <= price <= closes[1][i] * 1.1


def five_minute_return(symbol, at):
    """Return over the last 5 completed minute bars before `at`, or None (bar missing / older than 180 s)."""
    series = LINES.get(symbol, ([], None))[0]
    if not series:
        return None
    ts = [b.ts for b in series]
    minute = int(at.timestamp() // 60) * 60_000
    k = bisect.bisect_left(ts, minute) - 1
    if k < 0 or at.timestamp() * 1000 - (ts[k] + 60_000) > 180_000:
        return None  # last completed bar ended more than 180 s before the decision
    j = bisect.bisect_left(ts, ts[k] - 5 * 60_000)
    if j >= k or ts[j] != ts[k] - 5 * 60_000 or not series[j].close:
        return None
    return series[k].close / series[j].close - 1


def h2_check(symbol, at):
    """Coach H2 (frozen 10-30 01:24 ET): VETO when the median 5-min return of the OTHER stocks confirmed >=60 s
    at both window ends is negative and this stock's 5-min return is at or below it. Needs >=3 peers."""
    start = at - timedelta(minutes=5)
    target = five_minute_return(symbol, at)
    if target is None or not continuous(symbol, start, at):
        return {"verdict": "UNKNOWN", "reason": "target bars or scanner window missing", "target_ret": target}
    peers = []
    for other, windows in (WINDOWS or {}).items():
        if other == symbol or not any(s <= start - timedelta(seconds=60) and (e is None or at < e) for s, e in windows):
            continue
        r = five_minute_return(other, at)
        if r is not None:
            peers.append(r)
    if len(peers) < 3:
        return {"verdict": "UNKNOWN", "reason": f"only {len(peers)} valid peers", "target_ret": target, "peers": len(peers)}
    med = statistics.median(peers)
    verdict = "VETO" if med < 0 and target <= med else "KEEP"
    return {"verdict": verdict, "target_ret": round(target, 4), "peer_median": round(med, 4), "peers": len(peers)}
# Live ATR rules read from the running bot 10-29: resting buy-stop at line + 0.5% (cent ceiling),
# limit 0.5% above the trigger, following the line each minute while short (live log 10-29: PCLA re-placed
# 13:53/13:56/13:58 UTC as the line fell; NCPL held 1.81 while its line stayed ~1.79); exits: OCO target +5% / stop -8%,
# CONF1 confirmation (bar after the fill's bar must close long), sell flip.
LIVE_OFFSET, LIVE_BAND, LIVE_TARGET, LIVE_STOP = 0.005, 0.005, 0.05, 0.08


def live_rules_sim(conn, symbol, flip_bar, sell_close, day, now, base, two_min=False):
    """Same signal, the live bot's own entry and exit rules (simulation, not real fills)."""
    series, rows = LINES[symbol]
    fi = next(i for i, b in enumerate(series) if b.ts == flip_bar.ts)
    si = max((i for i in range(fi) if rows[i]["flip"] == "SELL"), default=None)
    if si is None:
        return dict(base, status="SKIPPED", reason="no short segment before this signal")
    open_from = datetime.combine(day, datetime.min.time(), ET).replace(hour=7)
    cutoff = datetime.combine(day, datetime.min.time(), ET).replace(hour=15, minute=45)
    session_end = datetime.combine(day, datetime.min.time(), ET).replace(hour=20)
    at = lambda i: datetime.fromtimestamp(series[i].ts / 1000, timezone.utc)
    first = si + 1
    if first > fi or rows[first - 1]["trail"] is None:
        return dict(base, status="SKIPPED", reason="no resting bar before the flip")
    seg_start = max(at(first), open_from)
    # The live waiting buy stays after the flip bar closes until it fills, a sell flip, or 15:45.
    seg_end = min(sell_close or cutoff, cutoff)
    if seg_end <= seg_start:
        return dict(base, status="SKIPPED", reason="outside the 07:00-15:45 entry window")
    stream = trades(conn, symbol, seg_start, min(session_end, now))
    fill = None
    triggered, k, prev_ts = False, None, None
    index = {series[i].ts: i for i in range(first, fi + 1)}
    for ts, price in stream:
        if ts >= seg_end:
            break
        if triggered and prev_ts is not None and not continuous(symbol, prev_ts, ts):
            triggered = False  # a FADE between two prints cancelled the order even with no print in the gap
        minute = int(ts.timestamp() // 60) * 60_000
        if minute > series[fi].ts:
            bar_k = k  # after the flip: the last resting price is kept (no new short line)
        else:
            bar_k = index.get(minute)
        if bar_k is None or rows[bar_k - 1]["trail"] is None:
            continue  # minute without a stored bar: the bot had no line to rest on
        if not confirmed(symbol, ts):
            triggered = False
            continue  # not confirmed on the scanner at this print: no order is resting
        if bar_k != k:
            k, triggered = bar_k, False
            trigger = ceil_cent(rows[k - 1]["trail"] * (1 + LIVE_OFFSET))
            limit = ceil_cent(trigger * (1 + LIVE_BAND))
        prev_ts = ts
        if not triggered and price >= trigger:
            triggered = True
        if triggered and price <= limit:
            fill = (ts, price, k, trigger)
            break
    if fill is None:
        return dict(base, status="SKIPPED" if now >= seg_end else "WAITING",
                    reason="live resting buy never filled" if now >= seg_end else "live resting buy waiting")
    f_ts, f_px, fk, trig = fill
    qty = int(NOTIONAL // f_px)
    # Live OCO target goes through broker_adapters.target_price.target_limit_price (ROUND_CEILING to the tick).
    from decimal import Decimal
    from project_mai_tai.broker_adapters.target_price import target_limit_price
    target = float(target_limit_price(Decimal(str(f_px)) * Decimal(str(1 + LIVE_TARGET))))
    stop = f_px * (1 - LIVE_STOP)
    # CONF1 (live confirmation_bar_start_ms): the bar after the bar that CONTAINS the fill.
    fill_bar_ms = int(f_ts.timestamp() // 60) * 60_000
    by_ts = {b.ts: i for i, b in enumerate(series)}
    conf_i = by_ts.get(fill_bar_ms + 60_000)
    conf_close = datetime.fromtimestamp((fill_bar_ms + 120_000) / 1000, timezone.utc)
    conf_fails = conf_i is not None and rows[conf_i]["state"] != "long" and conf_close <= now
    exit_ = None
    last = f_px
    deadline, hit_one = f_ts + timedelta(seconds=120), False
    for ts, price in stream:
        last = price
        if ts <= deadline and price >= f_px * 1.01:
            hit_one = True
        if sell_close is not None and ts >= sell_close:
            exit_ = (ts, price, "sell flip")
        elif two_min and ts >= deadline and not hit_one:
            # Operator 10-29 4th table: live entry + the 2-minute rule (not +1% within 2 min -> sell).
            exit_ = (ts, price, "not +1% in 2 min")
        elif conf_fails and ts >= conf_close:
            exit_ = (ts, price, "confirmation exit (bar after fill not long)")
        elif ts >= session_end:
            exit_ = (ts, price, "session end")
        elif price >= target:
            exit_ = (ts, target, "target +5%")
        elif price <= stop:
            exit_ = (ts, price, "stop -8%")
        if exit_:
            break
    if exit_ is None and now >= session_end:
        exit_ = (session_end, last, "session end")
    x_px = exit_[1] if exit_ else last
    return dict(base, trigger=trig, status="CLOSED" if exit_ else "OPEN", quantity=qty,
                entry_time=f_ts.astimezone(ET).strftime("%H:%M:%S"), entry_price=round(f_px, 4),
                exit_time=exit_[0].astimezone(ET).strftime("%H:%M:%S") if exit_ else "open",
                exit_price=round(x_px, 4), reason=exit_[2] if exit_ else "still open (marked at last trade)",
                pnl=round((x_px - f_px) * qty, 2), pnl_pct=round((x_px / f_px - 1) * 100, 2))


def confirmed_windows(conn, day):
    """Momentum-scanner confirmation windows per symbol (CONFIRM opens, FADE closes)."""
    rows = conn.execute(text(
        "select symbol, event_type, event_at from scanner_confirmed_events "
        "where trade_date = :d and event_type in ('CONFIRM','FADE') order by event_at, id"), {"d": day}).all()
    windows, open_at = {}, {}
    for symbol, kind, at in rows:
        if kind == "CONFIRM":
            open_at.setdefault(symbol, at)
        elif symbol in open_at:
            windows.setdefault(symbol, []).append((open_at.pop(symbol), at))
    for symbol, at in open_at.items():
        windows.setdefault(symbol, []).append((at, None))
    return windows


def on_scanner(windows, symbol, at):
    return any(start <= at and (end is None or at < end) for start, end in windows.get(symbol, ()))


def live_fills(conn, anchor):
    """Real ATR-bot fills today (both broker accounts), from the broker fill records."""
    rows = conn.execute(text(
        "select f.symbol, f.side, f.quantity, f.price, f.filled_at from fills f "
        "join broker_orders o on o.id = f.order_id join strategies st on st.id = o.strategy_id "
        "where st.code = 'schwab_1m_v2' and f.filled_at >= :a order by f.filled_at"), {"a": anchor}).all()
    fills = {}
    for symbol, side, qty, price, at in rows:
        fills.setdefault(symbol, []).append((side, float(qty), float(price), at))
    return fills


def live_row(fills, symbol, bar, sell_close, session_end, base):
    """What the live bot actually did for this signal: its real buy in the signal's segment and the
    real sells that closed it. A resting buy can fill inside the flip bar, so the window starts there."""
    start = datetime.fromtimestamp(bar.ts / 1000, timezone.utc)
    end = sell_close or session_end
    buys = [f for f in fills.get(symbol, ()) if f[0] == "buy" and start <= f[3] < end]
    if not buys:
        return dict(base, status="NO LIVE TRADE", reason="live bot did not buy this signal")
    qty = sum(f[1] for f in buys)
    entry = sum(f[1] * f[2] for f in buys) / qty
    first = buys[0][3]
    sells, left = [], qty
    for f in fills.get(symbol, ()):
        if f[0] == "sell" and f[3] > first and left > 0:
            take = min(left, f[1])
            sells.append((take, f[2], f[3]))
            left -= take
    if not sells:
        return dict(base, status="OPEN", quantity=int(qty), entry_time=first.astimezone(ET).strftime("%H:%M:%S"),
                    entry_price=round(entry, 4), exit_time="open", exit_price=round(entry, 4),
                    reason="live position still open", pnl=0.0, pnl_pct=0.0)
    sold = sum(t for t, _, _ in sells)
    exit_px = sum(t * p for t, p, _ in sells) / sold
    return dict(base, status="CLOSED", quantity=int(qty), entry_time=first.astimezone(ET).strftime("%H:%M:%S"),
                entry_price=round(entry, 4), exit_time=sells[-1][2].astimezone(ET).strftime("%H:%M:%S"),
                exit_price=round(exit_px, 4), reason="real live trade (broker fills)",
                pnl=round((exit_px - entry) * sold, 2), pnl_pct=round((exit_px / entry - 1) * 100, 2))


def trades(conn, symbol, start, end):
    """Eligible trades in [start, end), streamed in pages keyed on (event_ts, id) so ties never drop."""
    last_ts, last_id = start, -1
    while True:
        page = conn.execute(text(
            "select event_ts, price, conditions, size, id from market_capture_trades where symbol=:s "
            "and (event_ts, id) > (:t, :i) and event_ts >= :a and event_ts < :b and size > 0 "
            "order by event_ts, id limit :n"),
            {"s": symbol, "t": last_ts, "i": last_id, "a": start, "b": end, "n": PAGE}).all()
        for ts, price, cond, size, _ in page:
            # Same as the old FIRSTBAR execution rule: round lots only (>=100 shares) and no excluded condition.
            if size >= 100 and eligible(cond) and sane(symbol, ts, float(price)):
                yield ts, float(price)
        if len(page) < PAGE:
            return
        last_ts, last_id = page[-1][0], page[-1][4]


def simulate(conn, symbol, bar, sell_close, day, now):
    flip_close = datetime.fromtimestamp(bar.ts / 1000 + 60, timezone.utc)
    open_from = datetime.combine(day, datetime.min.time(), ET).replace(hour=7)
    cutoff = datetime.combine(day, datetime.min.time(), ET).replace(hour=15, minute=45)
    session_end = datetime.combine(day, datetime.min.time(), ET).replace(hour=20)
    entry_end = min(cutoff, sell_close or session_end)
    trigger = round(bar.high + 0.01, 4)
    signal = flip_close.astimezone(ET).strftime("%H:%M")
    base = {"symbol": symbol, "signal": signal, "trigger": trigger}
    if flip_close >= entry_end:
        return [dict(base, status="SKIPPED", reason="entry window ended")] * 2
    horizon = min(session_end, now)
    stream = trades(conn, symbol, max(flip_close, open_from), horizon)
    entry = None
    pending = []
    for ts, price in stream:
        if entry is None:
            if ts >= entry_end:
                break
            if price >= trigger and confirmed(symbol, ts):
                entry = (ts, price)
                break
    if entry is None:
        reason = "buy price never reached" if now >= entry_end else "waiting for buy price"
        return [dict(base, status="SKIPPED" if now >= entry_end else "WAITING", reason=reason)] * 2
    e_ts, e_px = entry
    qty = int(NOTIONAL // e_px)
    target = ceil_cent(e_px * 1.05)
    stop = e_px * 0.92
    deadline = e_ts + timedelta(seconds=120)
    results = {"FIRSTBAR": None, "FIRSTBAR+2min": None}
    hit_one = False
    last = e_px
    for ts, price in stream:  # continues after the entry trade
        last = price
        if ts <= deadline and price >= e_px * 1.01:
            hit_one = True
        for name in results:
            if results[name] is not None:
                continue
            reason, px = None, price
            if sell_close is not None and ts >= sell_close:
                reason = "sell flip"
            elif ts >= session_end:
                reason = "session end"
            elif price >= target:
                reason, px = "target +5%", target
            elif price <= stop:
                reason = "stop -8%"
            elif name == "FIRSTBAR+2min" and ts >= deadline and not hit_one:
                reason = "not +1% in 2 min"
            if reason:
                results[name] = (ts, px, reason)
        if all(results.values()):
            break
    if now >= session_end:  # no trade printed at/after 20:00: close at the session's last trade
        for name in results:
            results[name] = results[name] or (session_end, last, "session end")
    out = []
    for name in ("FIRSTBAR", "FIRSTBAR+2min"):
        r = results[name]
        row = dict(base, status="CLOSED" if r else "OPEN", quantity=qty, entry_time=e_ts.astimezone(ET).strftime("%H:%M:%S"),
                   entry_price=round(e_px, 4))
        x_px = r[1] if r else last
        row.update(exit_time=r[0].astimezone(ET).strftime("%H:%M:%S") if r else "open",
                   exit_price=round(x_px, 4), reason=r[2] if r else "still open (marked at last trade)",
                   pnl=round((x_px - e_px) * qty, 2), pnl_pct=round((x_px / e_px - 1) * 100, 2))
        out.append(row)
    return out


def run(now=None):
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(ET).date()
    anchor = datetime.combine(day, datetime.min.time(), ET).replace(hour=4)
    url = os.environ["MAI_TAI_DATABASE_URL"]
    engine = create_engine(url, pool_pre_ping=True)
    tables = {"FIRSTBAR": [], "FIRSTBAR+2min": [], "LIVE rules SIM": [], "LIVE+2min SIM": []}
    global WINDOWS
    with engine.connect() as conn:
        conn.execute(text("set statement_timeout = 20000"))
        windows = WINDOWS = confirmed_windows(conn, day)
        fills = live_fills(conn, anchor)
        session_end = datetime.combine(day, datetime.min.time(), ET).replace(hour=20)
        for symbol, flips in flips_by_symbol(conn, anchor).items():
            for i, (bar, flip) in enumerate(flips):
                if flip != "BUY":
                    continue
                # Operator 10-29: only signals while the stock is confirmed on the momentum scanner.
                if not on_scanner(windows, symbol, datetime.fromtimestamp(bar.ts / 1000 + 60, timezone.utc)):
                    continue
                nxt = next((b for b, f in flips[i + 1:] if f == "SELL"), None)
                sell_close = datetime.fromtimestamp(nxt.ts / 1000 + 60, timezone.utc) if nxt else None
                if sell_close is not None and sell_close > now:
                    sell_close = None
                a, b = simulate(conn, symbol, bar, sell_close, day, now)
                tables["FIRSTBAR"].append(a)
                tables["FIRSTBAR+2min"].append(b)
                tables["LIVE rules SIM"].append(live_rules_sim(conn, symbol, bar, sell_close, day, now,
                    {"symbol": symbol, "signal": a["signal"], "trigger": a["trigger"]}))
                live2 = live_rules_sim(conn, symbol, bar, sell_close, day, now,
                    {"symbol": symbol, "signal": a["signal"], "trigger": a["trigger"]}, two_min=True)
                if live2.get("status") in ("CLOSED", "OPEN"):
                    # Operator 10-30: record the H2 inputs at every buy decision (here: the simulated fill).
                    hh, mm, ss = (int(x) for x in live2["entry_time"].split(":"))
                    fill_at = datetime.combine(day, datetime.min.time(), ET).replace(hour=hh, minute=mm, second=ss)
                    live2["h2"] = h2_check(symbol, fill_at)
                tables["LIVE+2min SIM"].append(live2)
    for rows in tables.values():
        rows.sort(key=lambda r: (r.get("entry_time") or "99", r["signal"], r["symbol"]))
    doc = {"schema": "ATRSIM2_DAY_V1", "trading_day": day.isoformat(), "generated_at": now.isoformat(),
           "elapsed_seconds": round(time.monotonic() - started, 2), "notional_usd": NOTIONAL, "tables": tables}
    with engine.begin() as conn:
        conn.execute(text("delete from dashboard_snapshots where snapshot_type=:t and payload->>'trading_day'=:d"),
                     {"t": SNAPSHOT_TYPE, "d": day.isoformat()})
        conn.execute(text("insert into dashboard_snapshots (id, snapshot_type, payload, created_at) "
                          "values (gen_random_uuid(), :t, cast(:p as json), now())"),
                     {"t": SNAPSHOT_TYPE, "p": json.dumps(doc)})
    counts = {k: sum(1 for r in v if r["status"] in ("CLOSED", "OPEN")) for k, v in tables.items()}
    print(json.dumps({"event": "ATRSIM2_WRITTEN", "trading_day": doc["trading_day"], "trades": counts,
                      "signals": len(tables["FIRSTBAR"]), "elapsed_seconds": round(time.monotonic() - started, 2)}),
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())

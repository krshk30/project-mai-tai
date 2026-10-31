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
import hashlib
import json
import math
import statistics
import os
import subprocess
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
# Operator 10-31 (RENT 10-26): the trade capture follows the scanner subscription, so a session-end
# "exit" priced from a print long before 20:00 is not a real exit. Past this gap the exit is UNKNOWN.
SESSION_MARK_MAX_GAP = timedelta(minutes=15)
# Operator 10-31 round 4 (hot/cold STOCK at the buy): pullback sizes recorded side by side, and the
# time & sales windows before the decision. A trade is classified only against a quote <= 5 s old.
PULLBACK_SIZES = (0.02, 0.03, 0.05)
TAPE_WINDOWS_MIN = (5, 10)
QUOTE_MAX_AGE = timedelta(seconds=5)


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
# Live ESTABLISHED-SHORT gate (schwab_1m_v2 cw_v2_resting_entry_min_short_bars, default 3): the
# waiting buy is placed only after the line has been short for 3 closed bars (TNMG 10-30: SELL bar
# 09:13, rested at 09:17). A shorter short never gets a waiting buy (ONEG 10-30 10:50, 1 bar).
LIVE_MIN_SHORT_BARS = 3


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
    first = si + 1 + LIVE_MIN_SHORT_BARS
    if first > fi:
        return dict(base, status="SKIPPED", reason="short line lasted under 3 bars (live established-short rule)")
    if rows[first - 1]["trail"] is None:
        return dict(base, status="SKIPPED", reason="no resting bar before the flip")
    seg_start = max(at(first), open_from)
    # The live waiting buy stays after the flip bar closes until it fills, a sell flip, or 15:45.
    seg_end = min(sell_close or cutoff, cutoff)
    if seg_end <= seg_start:
        return dict(base, status="SKIPPED", reason="outside the 07:00-15:45 entry window")
    stream = trades(conn, symbol, seg_start, min(session_end, now))
    fill = None
    triggered, k, prev_ts, triggered_at = False, None, None, None
    index = {series[i].ts: i for i in range(first, fi + 1)}
    for ts, price in stream:
        if ts >= seg_end:
            break
        if triggered and prev_ts is not None and not continuous(symbol, prev_ts, ts):
            triggered, triggered_at = False, None  # a FADE between two prints cancelled the order (no print in the gap)
        minute = int(ts.timestamp() // 60) * 60_000
        if minute > series[fi].ts:
            bar_k = k  # after the flip: the last resting price is kept (no new short line)
        else:
            bar_k = index.get(minute)
        if bar_k is None or rows[bar_k - 1]["trail"] is None:
            continue  # minute without a stored bar: the bot had no line to rest on
        if not confirmed(symbol, ts):
            triggered, triggered_at = False, None
            continue  # not confirmed on the scanner at this print: no order is resting
        if bar_k != k:
            k, triggered, triggered_at = bar_k, False, None
            trigger = ceil_cent(rows[k - 1]["trail"] * (1 + LIVE_OFFSET))
            limit = ceil_cent(trigger * (1 + LIVE_BAND))
        prev_ts = ts
        if not triggered and price >= trigger:
            triggered, triggered_at = True, ts
        if triggered and price <= limit:
            fill = (ts, price, k, trigger, triggered_at)
            break
    if fill is None:
        return dict(base, status="SKIPPED" if now >= seg_end else "WAITING",
                    reason="live resting buy never filled" if now >= seg_end else "live resting buy waiting")
    f_ts, f_px, fk, trig, trig_at = fill
    # Coach 10-31: keep the order generation (the resting price in force since bar fk opened) and the
    # trigger print separate from the fill print.
    order_times = {"order_price_since": datetime.fromtimestamp(series[fk].ts / 1000, timezone.utc).astimezone(ET).strftime("%H:%M:%S"),
                   "triggered_at": trig_at.astimezone(ET).strftime("%H:%M:%S.%f")[:-3],
                   "filled_at": f_ts.astimezone(ET).strftime("%H:%M:%S.%f")[:-3]}
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
    last, last_ts = f_px, f_ts
    deadline, hit_one = f_ts + timedelta(seconds=120), False
    for ts, price in stream:
        last, last_ts = price, ts
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
        if session_end - last_ts > SESSION_MARK_MAX_GAP:
            return dict(base, trigger=trig, status="EXIT UNKNOWN", quantity=qty, order_times=order_times,
                        entry_time=f_ts.astimezone(ET).strftime("%H:%M:%S"), entry_price=round(f_px, 4),
                        exit_time="unknown", reason=("no trade print in the last "
                        f"{int((session_end - last_ts).total_seconds() // 60)} min before 20:00 (capture ended); exit UNKNOWN"))
        exit_ = (session_end, last, "session end")
    x_px = exit_[1] if exit_ else last
    return dict(base, trigger=trig, status="CLOSED" if exit_ else "OPEN", quantity=qty, order_times=order_times,
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


_CAPTURE_TRADES = trades  # launchers that read another source (Massive backfill) replace `trades`


def provenance():
    """Coach 10-31: every day row names the engine that produced it."""
    path = os.path.abspath(__file__)
    with open(path, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    commit = dirty = None
    try:
        repo = os.path.dirname(os.path.dirname(path))
        commit = subprocess.run(["git", "-C", repo, "rev-parse", "HEAD"], capture_output=True, text=True,
                                timeout=5).stdout.strip() or None
        if commit:
            dirty = bool(subprocess.run(["git", "-C", repo, "status", "--porcelain", "--", "tools/atrsim2_job.py"],
                                        capture_output=True, text=True, timeout=5).stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return {"engine_file": "tools/atrsim2_job.py", "engine_sha256": sha, "engine_commit": commit,
            "engine_file_dirty": dirty, "launcher": os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else None,
            "trade_source": "market_capture_trades" if trades is _CAPTURE_TRADES else getattr(trades, "__name__", "replaced")}


def _pullbacks(closes, size):
    """Down legs of at least `size` from a running high, on 1-minute CLOSES (zigzag). Closes, not
    highs/lows: inside one bar we cannot tell whether the high or the low came first."""
    if not closes:
        return 0
    count, peak, trough, down = 0, closes[0], None, False
    for c in closes:
        if not down:
            peak = max(peak, c)
            if c <= peak * (1 - size):
                count, down, trough = count + 1, True, c
        else:
            trough = min(trough, c)
            if c >= trough * (1 + size):
                down, peak = False, c
    return count


def tape_pressure(conn, symbol, at):
    """Time & sales before the decision: share of volume at/above the ask (buyers) vs at/below the bid
    (sellers), each trade against the last quote <= 5 s old. UNKNOWN when nothing was captured."""
    longest = timedelta(minutes=max(TAPE_WINDOWS_MIN))
    tape = conn.execute(text(
        "select event_ts, price, size, conditions from market_capture_trades where symbol=:s "
        "and event_ts >= :a and event_ts < :b and size > 0 order by event_ts, id"),
        {"s": symbol, "a": at - longest, "b": at}).all()
    quotes = conn.execute(text(
        "select event_ts, bid_price, ask_price from market_capture_quotes where symbol=:s "
        "and event_ts >= :a and event_ts < :b order by event_ts, id"),
        {"s": symbol, "a": at - longest - QUOTE_MAX_AGE, "b": at}).all()
    q_ts = [q[0] for q in quotes]
    out = {}
    for minutes in TAPE_WINDOWS_MIN:
        start = at - timedelta(minutes=minutes)
        v = dict(buy=0, sell=0, between=0, unknown_no_fresh_quote=0, unknown_locked_crossed=0,
                 unknown_invalid_quote=0, excluded_odd_lot=0, excluded_condition=0)
        for ts, price, size, cond in tape:
            if ts < start:
                continue
            if size < 100:  # coach 10-31: odd lots are excluded (and counted)
                v["excluded_odd_lot"] += size
                continue
            if not eligible(cond):
                v["excluded_condition"] += size
                continue
            k = bisect.bisect_right(q_ts, ts) - 1
            bid = float(quotes[k][1]) if k >= 0 and quotes[k][1] is not None else None
            ask = float(quotes[k][2]) if k >= 0 and quotes[k][2] is not None else None
            if k < 0 or ts - q_ts[k] > QUOTE_MAX_AGE:
                v["unknown_no_fresh_quote"] += size
            elif not bid or not ask or bid <= 0 or ask <= 0:
                v["unknown_invalid_quote"] += size
            elif bid >= ask:
                v["unknown_locked_crossed"] += size  # coach 10-31: side not knowable -> UNKNOWN, not "between"
            elif float(price) >= ask:
                v["buy"] += size
            elif float(price) <= bid:
                v["sell"] += size
            else:
                v["between"] += size
        v = {key: int(val) for key, val in v.items()}
        eligible_volume = sum(val for key, val in v.items() if not key.startswith("excluded"))
        classified = v["buy"] + v["sell"] + v["between"]
        row = {"volume": v, "eligible_volume": eligible_volume,
               "coverage": round(classified / eligible_volume, 4) if eligible_volume else None}
        if eligible_volume == 0:
            row.update(status="UNKNOWN", reason="no captured round-lot trades in the window")
        elif classified == 0:
            row.update(status="UNKNOWN", reason="no trade had a valid quote <= 5 s old")
        else:
            row.update(buy_share=round(v["buy"] / classified, 4), sell_share=round(v["sell"] / classified, 4),
                       between_share=round(v["between"] / classified, 4))
        out[f"{minutes}m"] = row
    return out


def stock_features(conn, symbol, flip_bar, at, price, previous):
    """Operator 10-31 round 4 — is THIS STOCK hot or cold at the buy? Inputs only, no rule yet."""
    series, rows = LINES[symbol]
    fi = next(i for i, b in enumerate(series) if b.ts == flip_bar.ts)
    si = max((i for i in range(fi) if rows[i]["flip"] == "SELL"), default=None)
    out = {"decision_at": at.astimezone(ET).strftime("%H:%M:%S"), "decision_price": round(price, 4)}
    # 1. the earlier trade on this stock today (same table) that started before this decision.
    # Coach 10-31: NONE / OPEN (still held at the decision) / UNKNOWN (exit unknown) / closed WIN-LOSS-FLAT.
    if previous is None:
        out["previous_trade"] = {"result": "none"}
    else:
        p_entry, p_exit, p_status, p_pct = previous
        if p_status == "EXIT UNKNOWN":
            out["previous_trade"] = {"result": "unknown", "reason": "earlier trade exit UNKNOWN"}
        elif p_exit is None or p_exit > at:
            out["previous_trade"] = {"result": "open", "minutes_since_entry": round((at - p_entry).total_seconds() / 60, 1)}
        else:
            out["previous_trade"] = {"result": "loss" if p_pct < 0 else "win" if p_pct > 0 else "flat",
                                     "pnl_pct": p_pct, "minutes_since_exit": round((at - p_exit).total_seconds() / 60, 1)}
    # 2. + 3. the purple (short) stretch: from the SELL flip bar to the bar before this BUY flip, but only
    # bars COMPLETED before the decision (codex 10-31: a resting buy can fill inside/before the flip bar).
    decision_ms = at.timestamp() * 1000
    end = fi
    while end > (si if si is not None else 0) and series[end - 1].ts + 60_000 > decision_ms:
        end -= 1
    if si is None:
        out["purple"] = {"status": "UNKNOWN", "reason": "no SELL flip earlier today"}
    elif end <= si:
        out["purple"] = {"status": "UNKNOWN", "reason": "no purple bar completed before the decision"}
    else:
        closes = [b.close for b in series[si:end]]
        low_so_far, high_so_far, up, down = math.inf, 0.0, 0.0, 0.0
        for c in closes:  # biggest rise off an earlier low / drop off an earlier high, in time order
            up = max(up, c / low_so_far - 1) if low_so_far < math.inf else up
            down = max(down, 1 - c / high_so_far) if high_so_far else down
            low_so_far, high_so_far = min(low_so_far, c), max(high_so_far, c)
        out["purple"] = {"minutes": round((min(series[fi].ts, decision_ms) - series[si].ts) / 60_000, 1),
                         "bars": len(closes),
                         "pullbacks": {f"{int(sz * 100)}pct": _pullbacks(closes, sz) for sz in PULLBACK_SIZES},
                         "max_swing_up_pct": round(up * 100, 2), "max_swing_down_pct": round(down * 100, 2)}
    # 4. time & sales before the decision
    out["tape"] = tape_pressure(conn, symbol, at)
    # 5. distance below the day high, from bars completed before the decision
    done = [b for b in series if b.ts + 60_000 <= at.timestamp() * 1000]
    day_high = max((b.high for b in done), default=None)
    out["below_day_high_pct"] = round((1 - price / day_high) * 100, 2) if day_high else None
    # Coach 10-31: say how complete the day-high history was (stored bars only; no bar = no trades or a gap).
    out["day_high_coverage"] = ({"first_bar_at": datetime.fromtimestamp(done[0].ts / 1000, timezone.utc).astimezone(ET).strftime("%H:%M"),
                                 "bars": len(done),
                                 "minutes_spanned": round((done[-1].ts - done[0].ts) / 60_000) + 1} if done else
                                {"status": "UNKNOWN", "reason": "no bar completed before the decision"})
    return out


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
    last, last_ts = e_px, e_ts
    for ts, price in stream:  # continues after the entry trade
        last, last_ts = price, ts
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
    stale_end = now >= session_end and session_end - last_ts > SESSION_MARK_MAX_GAP
    if now >= session_end and not stale_end:  # no trade printed at/after 20:00: close at the session's last trade
        for name in results:
            results[name] = results[name] or (session_end, last, "session end")
    out = []
    for name in ("FIRSTBAR", "FIRSTBAR+2min"):
        r = results[name]
        if r is None and stale_end:
            out.append(dict(base, status="EXIT UNKNOWN", quantity=qty, entry_time=e_ts.astimezone(ET).strftime("%H:%M:%S"),
                            entry_price=round(e_px, 4), exit_time="unknown",
                            reason="no trade print near 20:00 (capture ended); exit UNKNOWN"))
            continue
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
        last_trade = {"LIVE rules SIM": {}, "LIVE+2min SIM": {}}

        def at_clock(hms):
            hh, mm, ss = (int(x) for x in hms.split(":"))
            return datetime.combine(day, datetime.min.time(), ET).replace(hour=hh, minute=mm, second=ss)

        def record(name, symbol, bar, row):
            """Stock features at the buy decision: the simulated fill, else the flip bar's close."""
            if row.get("entry_time"):
                at, price = at_clock(row["entry_time"]), row["entry_price"]
            else:
                at, price = datetime.fromtimestamp(bar.ts / 1000 + 60, timezone.utc), bar.close
            prev = last_trade[name].get(symbol)
            row["stock"] = stock_features(conn, symbol, bar, at, price,
                                          prev if prev is not None and prev[0] < at else None)
            if row.get("entry_time"):
                exit_at = at_clock(row["exit_time"]) if row.get("exit_time") not in (None, "open", "unknown") else None
                last_trade[name][symbol] = (at, exit_at, row.get("status"), row.get("pnl_pct"))

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
                live1 = live_rules_sim(conn, symbol, bar, sell_close, day, now,
                    {"symbol": symbol, "signal": a["signal"], "trigger": a["trigger"]})
                record("LIVE rules SIM", symbol, bar, live1)
                tables["LIVE rules SIM"].append(live1)
                live2 = live_rules_sim(conn, symbol, bar, sell_close, day, now,
                    {"symbol": symbol, "signal": a["signal"], "trigger": a["trigger"]}, two_min=True)
                if live2.get("status") in ("CLOSED", "OPEN"):
                    # Operator 10-30: record the H2 inputs at every buy decision (here: the simulated fill).
                    hh, mm, ss = (int(x) for x in live2["entry_time"].split(":"))
                    fill_at = datetime.combine(day, datetime.min.time(), ET).replace(hour=hh, minute=mm, second=ss)
                    live2["h2"] = h2_check(symbol, fill_at)
                record("LIVE+2min SIM", symbol, bar, live2)
                tables["LIVE+2min SIM"].append(live2)
    for rows in tables.values():
        rows.sort(key=lambda r: (r.get("entry_time") or "99", r["signal"], r["symbol"]))
    doc = {"schema": "ATRSIM2_DAY_V1", "trading_day": day.isoformat(), "generated_at": now.isoformat(),
           "provenance": provenance(), "run_started_utc": datetime.now(timezone.utc).isoformat(),
           "elapsed_seconds": round(time.monotonic() - started, 2), "notional_usd": NOTIONAL, "tables": tables}
    dry = os.environ.get("ATRSIM_DRY_RUN_OUT")
    if dry:  # replay/test: write the day document to a file, never to the database
        with open(dry, "w") as fh:
            json.dump(doc, fh)
        print(json.dumps({"event": "ATRSIM2_DRY_RUN", "trading_day": doc["trading_day"], "out": dry}), flush=True)
        return 0
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

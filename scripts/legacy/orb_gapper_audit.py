"""(2) GAPPER-UNIVERSE SCANNER AUDIT for one flood day (07-01). Reconstruct the FULL >30% pre-open
gapper universe from POLYGON (independent of our scanner stream), match the scanner's gap definition
(todays_change_percent = (9:25 pre-open price - prev_close)/prev_close, using pre-market bars — NOT the
grouped_daily open gap), then per name: did our scanner STREAM/CONFIRM it, was it ORB-tradeable (PR
config, confirmed-window gate OFF), P&L, behavior tag. Headline: of N >30% gappers, how many tradeable,
how many the scanner MISSED. Read-only, no live-money risk.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import text

from project_mai_tai.backtest.data import Quote, Trade, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
DATE, PREV = "2026-07-01", "2026-06-30"
GATE, UNGATE_MIN, GAP_MIN = 4.3, 4, 30.0


def _et(h, m):
    y, mo, d = (int(x) for x in DATE.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def _ns(dt):
    return int(dt.timestamp() * 1e9)


def _ts_ns(o):
    for a in ("sip_timestamp", "participant_timestamp", "timestamp"):
        v = getattr(o, a, None)
        if v:
            return v
    return None


def poly_trades(c, sym, lo, hi):
    out = []
    for t in c.list_trades(sym, timestamp_gte=lo, timestamp_lte=hi, limit=50000):
        ts = _ts_ns(t)
        if ts and getattr(t, "price", None):
            out.append(Trade(ts=datetime.fromtimestamp(ts / 1e9, timezone.utc), price=t.price, size=getattr(t, "size", 0) or 0))
    out.sort(key=lambda t: t.ts)
    return out


def poly_quotes(c, sym, lo, hi):
    out = []
    for q in c.list_quotes(sym, timestamp_gte=lo, timestamp_lte=hi, limit=50000):
        ts = _ts_ns(q)
        b, a = getattr(q, "bid_price", 0) or 0, getattr(q, "ask_price", 0) or 0
        if ts and b > 0 and a > 0:
            out.append(Quote(ts=datetime.fromtimestamp(ts / 1e9, timezone.utc), bid=b, ask=a))
    out.sort(key=lambda q: q.ts)
    return out


def main():
    from massive import RESTClient
    st = get_settings()
    c = RESTClient(api_key=st.massive_api_key, retries=0, read_timeout=25)
    sf = build_session_factory(st)

    # streamed symbols (market_capture) + confirmed (scanner windows) on 07-01
    with sf() as s:
        streamed = {r[0] for r in s.execute(text(
            "SELECT DISTINCT symbol FROM market_capture_trades "
            "WHERE (event_ts AT TIME ZONE 'America/New_York')::date = :d"), {"d": DATE})}
    confirmed = set(load_windows(f"/home/trader/wt-atr-study/windows/windows_{DATE}.json").keys())
    print(f"scanner: {len(streamed)} streamed, {len(confirmed)} confirmed on {DATE}")

    prev = {a.ticker: a.close for a in c.get_grouped_daily_aggs(PREV) if a.close}
    day = c.get_grouped_daily_aggs(DATE)
    cands = []
    for a in day:
        pc = prev.get(a.ticker)
        if not pc or pc <= 0 or a.open is None or not (1.0 <= a.open <= 50) or (a.volume or 0) < 300000:
            continue
        if (a.high - pc) / pc > 0.25 or (a.open - pc) / pc > 0.20:      # coarse superset to bound fetches
            cands.append((a.ticker, pc))
    print(f"coarse candidates (RTH gap): {len(cands)} -> refining to >30% PRE-OPEN gap via pre-market bars...")

    lo925 = _ns(_et(9, 25))
    gappers = []
    for tkr, pc in cands:
        try:
            mins = list(c.list_aggs(tkr, 1, "minute", DATE, DATE, limit=50000))
        except Exception:
            continue
        pm = [m for m in mins if datetime.fromtimestamp(m.timestamp / 1000, timezone.utc).astimezone(_ET).time()
              < datetime(2000, 1, 1, 9, 30).time()
              and datetime.fromtimestamp(m.timestamp / 1000, timezone.utc).astimezone(_ET).time()
              >= datetime(2000, 1, 1, 7, 0).time()]
        px925 = pm[-1].close if pm else None
        if not px925:
            continue
        gap = (px925 - pc) / pc * 100
        if gap >= GAP_MIN:
            gappers.append((tkr, pc, gap))
    gappers.sort(key=lambda g: g[2], reverse=True)
    print(f">30% PRE-OPEN gappers = {len(gappers)}\n")

    obs, so, cut, end = _et(9, 25), _et(9, 30), _et(10, 0), _et(10, 10)
    lo, hi = _ns(obs), _ns(end)
    hdr = f"{'sym':<7}{'gap%':>7}{'streamed':>9}{'confirm':>8}{'trades':>7}{'entries':>8}{'pnl':>7}{'atr5':>6}{'er':>5}  behavior"
    print(hdr)
    print("-" * len(hdr))
    n_trade = n_miss = 0
    for tkr, pc, gap in gappers:
        strm = "YES" if tkr in streamed else "no"
        conf = "YES" if tkr in confirmed else "no"
        trades = poly_trades(c, tkr, lo, hi)
        quotes = poly_quotes(c, tkr, lo, hi)
        if len(trades) < 200 or len(quotes) < 50:
            print(f"{tkr:<7}{gap:>6.0f}%{strm:>9}{conf:>8}{len(trades):>7}   (thin — not tradeable)")
            continue
        bars = build_bars(trades, so)
        a5 = atr_pct5(bars)
        cl = [b.close for b in bars if b.close > 0]
        netm = abs(cl[-1] - cl[0]) if len(cl) > 1 else 0
        path = sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl)))
        er = netm / path if path > 0 else 0
        tag = "slow" if (a5 is None or a5 < GATE) else ("grinding" if er >= 0.10 else "volatile")
        tk = simulate_orb_tick_entry(trades, quotes, gap_cap_pct=1.5, trail_pct=2.0, qty=5,
                                     observe_open=obs, session_open=so, cutoff=cut, capped=False,
                                     latency_s=3.0, entry_windows=None, atr_gate_pct=GATE,
                                     gate_after_secs=UNGATE_MIN * 60, bars=bars)
        pnl = sum(t.pnl for t in tk)
        if tk:
            n_trade += 1
        if tk and tkr not in streamed:
            n_miss += 1
        a5s = f"{a5:.1f}" if a5 is not None else " -"
        print(f"{tkr:<7}{gap:>6.0f}%{strm:>9}{conf:>8}{len(trades):>7}{len(tk):>8}{pnl:>+7.2f}{a5s:>6}{er:>5.2f}  {tag}")

    print(f"\nHEADLINE: {len(gappers)} names gapped >30% pre-open on {DATE}. "
          f"{n_trade} produced ORB tick-entry trades. "
          f"Of those tradeable, {n_miss} were NOT streamed by our scanner (MISSED).")


if __name__ == "__main__":
    main()

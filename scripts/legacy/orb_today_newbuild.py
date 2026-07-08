"""What the NEW ORB build (PR #403: tick entry + ATR gate 4.3 + ungate-4min + first-bar liquidity
100K/1.0% + 2% trail) would have done on TODAY'S (07-08) qualified stocks, gated to the REAL Mai-Tai
confirmed windows (live scanner timing) and respecting the gap-cap — if the entry's ask is past the
gap-cap it's LEFT (rejected, like the live VTAK 09:31/09:32 abandons). Per-stock: trades or the reason
for no trade (liquidity-gated / gap-cap rejected / no confirmed break).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import DbMarketDataSource, build_bars
from project_mai_tai.backtest.orb_sim import simulate_orb_tick_entry
from project_mai_tai.backtest.scanner_windows import load_windows
from project_mai_tai.db.session import build_session_factory
from project_mai_tai.settings import get_settings
from project_mai_tai.strategy_core.orb_tick_entry import atr_pct5

_ET = ZoneInfo("America/New_York")
DATE = "2026-07-08"
GATE, UNGATE_MIN, GAP, TRAIL, LIQ_VOL, LIQ_SPR = 4.3, 4, 1.5, 2.0, 100000.0, 1.0


def _et(h, m):
    y, mo, d = (int(x) for x in DATE.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=_ET).astimezone(timezone.utc)


def hh(t):
    return t.astimezone(_ET).strftime("%H:%M:%S")


def _fb(tr, q, so):
    t1 = [t for t in tr if so <= t.ts < so + timedelta(seconds=60)]
    q1 = [x for x in q if so <= x.ts < so + timedelta(seconds=60)]
    vol = sum(t.size for t in t1)
    spr = [(x.ask - x.bid) / ((x.ask + x.bid) / 2) * 100 for x in q1 if x.ask > 0 and x.bid > 0 and x.ask >= x.bid]
    return vol, (median(spr) if spr else 99.0)


def main():
    src = DbMarketDataSource(build_session_factory(get_settings()))
    obs, so, cut, end = _et(9, 25), _et(9, 30), _et(10, 0), _et(10, 10)
    wins = load_windows(f"/home/trader/wt-atr-study/windows/windows_{DATE}.json")
    base = dict(gap_cap_pct=GAP, trail_pct=TRAIL, qty=5, observe_open=obs, session_open=so, cutoff=cut,
                capped=False, latency_s=3.0, atr_gate_pct=GATE, gate_after_secs=UNGATE_MIN * 60)
    print(f"NEW BUILD (PR #403) on {DATE} qualified+confirmed stocks — confirmed-window timing, gap-cap respected\n")
    traded = []
    for sym in src.orb_qualified_symbols(obs, end, min_trades=500):
        ewin = wins.get(sym, [])
        if not ewin:
            continue
        tr, q = src.trades(sym, obs, end), src.quotes(sym, obs, end)
        if len(tr) < 500 or len(q) < 50:
            continue
        bars = build_bars(tr, so)
        vol, spr = _fb(tr, q, so)
        a5 = atr_pct5(bars)
        cl = [b.close for b in bars if b.close > 0]
        er = abs(cl[-1] - cl[0]) / sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl))) if len(cl) > 1 and sum(abs(cl[i] - cl[i - 1]) for i in range(1, len(cl))) > 0 else 0
        tag = "slow" if (a5 is None or a5 < GATE) else ("grinding" if er >= 0.10 else "volatile")
        liq_ok = vol >= LIQ_VOL and spr <= LIQ_SPR
        hdr = f"{sym:<6} [{tag}] first-bar vol={vol/1000:.0f}K spr={spr:.2f}% -> LIQ-GATE {'PASS' if liq_ok else 'FAIL'}"
        if not liq_ok:
            print(f"{hdr}  |  SKIPPED (liquidity-gated)")
            continue
        # new-build trades (gap-cap respected)
        tk = simulate_orb_tick_entry(tr, q, entry_windows=ewin, bars=bars,
                                     liq_min_volume=LIQ_VOL, liq_max_spread_pct=LIQ_SPR, **base)
        # diagnostic: with a huge gap-cap, were there breaks the gap-cap rejected?
        loose = simulate_orb_tick_entry(tr, q, entry_windows=ewin, bars=bars,
                                        liq_min_volume=LIQ_VOL, liq_max_spread_pct=LIQ_SPR,
                                        **{**base, "gap_cap_pct": 99.0})
        if tk:
            traded.append((sym, sum(t.pnl for t in tk)))
            print(f"{hdr}  |  TRADED: {len(tk)} entries, net {sum(t.pnl for t in tk):+.2f}")
            for t in tk:
                print(f"    {hh(t.entry_ts)} {t.entry_price:.4f} -> {hh(t.exit_ts)} {t.exit_price:.4f} "
                      f"({(t.exit_price/t.entry_price-1)*100:+.1f}%) pnl{t.pnl:+.2f} {t.exit_reason}")
        elif loose:
            print(f"{hdr}  |  NO TRADE: {len(loose)} break(s) REJECTED by gap-cap (ask ran past +1.5% — left, like live VTAK)")
        else:
            print(f"{hdr}  |  NO TRADE: no qualifying break inside the confirmed window")
    print(f"\nSUMMARY: new build traded {len(traded)} stocks today, net {sum(p for _, p in traded):+.2f} "
          + "(" + ", ".join(f"{s} {p:+.2f}" for s, p in traded) + ")")


if __name__ == "__main__":
    main()

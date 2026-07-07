"""Daily-validation sheet rendering — enumerates the QUALIFIED universe per strategy so EVERY
qualified name appears with trades OR an explicit reason (SKIP-no-feed / 0t-no-signal). No silent
absence (the CLRO omission that motivated this). Read-only reporting; does not touch live trading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import build_bars
from project_mai_tai.backtest.orb_sim import simulate_bar_close, simulate_intrabar
from project_mai_tai.backtest.v2_sim import simulate_v2

_ET = ZoneInfo("America/New_York")


def _et(ts):
    return ts.astimezone(_ET).strftime("%H:%M:%S") if ts else "  --  "


def classify_v2_feed(n_bars: int, n_ticks: int, *, min_bars: int = 10, sparse_bars: int = 150):
    """(status_label, backtestable). ATR needs ~min_bars to define the trail; sparse = usable but
    thin. The reason string is what makes v2 coverage gaps visible on the sheet."""
    if n_bars == 0:
        return "SKIP — no Schwab bars (feed gap)", False
    if n_ticks == 0:
        return "SKIP — no Schwab ticks (feed gap)", False
    if n_bars < min_bars:
        return f"SKIP — insufficient bars ({n_bars}; ATR needs ~{min_bars})", False
    if n_bars < sparse_bars:
        return f"SPARSE ({n_bars} bars, {n_ticks} ticks)", True
    return f"full ({n_bars} bars, {n_ticks} ticks)", True


def render_v2_sheet(src, y: int, m: int, d: int) -> str:
    obs = datetime(y, m, d, 8, 0, tzinfo=timezone.utc)          # 04:00 ET
    end = datetime(y, m, d + 1, 0, 0, tzinfo=timezone.utc)      # 20:00 ET
    syms = src.v2_qualified_symbols(obs, end)
    out = [f"ATR/v2 sheet {y}-{m:02d}-{d:02d} — {len(syms)} qualified (tracked ∪ traded); "
           f"qty10, Schwab ~0s. Every name shown with a reason (no silent absence)."]
    for sym in syms:
        sb = src.schwab_bars(sym, obs, end)
        sq = src.schwab_quotes(sym, obs, end)
        mq = src.quotes(sym, obs, end)
        status, ok = classify_v2_feed(len(sb), len(sq))
        out.append(f"\n== {sym} [FEED: {status}] ==")
        if not ok:
            out.append("   -> not backtestable (coverage gap)")
            continue
        for mode in ("bar_close", "intrabar"):
            tr = simulate_v2(sb, sq, mq, qty=10, mode=mode)
            if not tr:
                out.append(f"  {mode.upper()}: 0t — no ATR entry (no confirmed touch / vol-floor / no fill)")
                continue
            out.append(f"  {mode.upper()} ({len(tr)}t net ${sum(t.pnl for t in tr):+.2f}):")
            for i, t in enumerate(tr, 1):
                out.append(f"     #{i} {_et(t.entry_ts)} touch {t.touch_price:.4f} @{t.entry_price:.4f} "
                           f"-> {_et(t.exit_ts)} @{(t.exit_price or 0):.4f} [{t.exit_reason}] "
                           f"legs={t.n_legs} ${t.pnl:+.3f}")
    return "\n".join(out)


def render_orb_sheet(src, y: int, m: int, d: int) -> str:
    def et(hh, mm):
        return datetime(y, m, d, hh, mm, tzinfo=_ET).astimezone(timezone.utc)

    obs, so, cut, end = et(9, 25), et(9, 30), et(10, 0), et(10, 10)
    syms = src.orb_qualified_symbols(obs, cut)
    out = [f"ORB sheet {y}-{m:02d}-{d:02d} — {len(syms)} qualified (ORB-window captured ∪ traded); "
           f"qty5, Webull band L3/L14."]
    for sym in syms:
        trades = src.trades(sym, obs, end)
        quotes = src.quotes(sym, obs, end)
        bars = build_bars(trades, so)
        out.append(f"\n== {sym} (trades={len(trades)}) ==")
        win = dict(observe_open=obs, session_open=so, cutoff=cut, capped=False)
        base = dict(gap_cap_pct=1.5, trail_pct=3.0, qty=5)
        for mode, fn, src_arg in (("BAR-CLOSE", simulate_bar_close, bars), ("INTRABAR", simulate_intrabar, trades)):
            a3 = fn(src_arg, quotes, latency_s=3.0, **base, **win)
            a14 = fn(src_arg, quotes, latency_s=14.0, **base, **win)
            if not a3 and not a14:
                out.append(f"  {mode}: 0t — no break / thin")
                continue
            for lat, tr in (("L3", a3), ("L14", a14)):
                out.append(f"  {mode} @{lat} ({len(tr)}t net ${sum(t.pnl for t in tr):+.2f}):")
                for i, t in enumerate(tr, 1):
                    lvl = f"{t.level:.4f}" if t.level is not None else "  --  "
                    out.append(f"     #{i} {_et(t.entry_ts)} lvl {lvl} @{t.entry_price:.4f} "
                               f"-> {_et(t.exit_ts)} @{(t.exit_price or 0):.4f} [{t.exit_reason}] ${t.pnl:+.3f}")
    return "\n".join(out)

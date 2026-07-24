"""Daily-validation sheet rendering — enumerates the QUALIFIED universe per strategy so EVERY
qualified name appears with trades OR an explicit reason (SKIP-no-feed / 0t-no-signal). No silent
absence (the CLRO omission that motivated this). Read-only reporting; does not touch live trading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.data import build_bars
from project_mai_tai.backtest.orb_sim import simulate_bar_close, simulate_intrabar

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


def render_v2_sheet(src, y: int, m: int, d: int, *, eh_enabled: bool = False) -> str:
    """The v2 daily sheet, now driven by the REPLAY engine (backtest/replay.py) — the REAL live
    strategy entry + the shared cw_exit_decision / static-OCO exit — instead of the retired
    re-implementing `simulate_v2` (docs/backtest-replay-engine-design.md P4: one replay, one truth).

    Coverage-honesty is preserved: every qualified name (tracked ∪ traded) appears with a REASON —
    a feed SKIP (`classify_v2_feed` / the replay's sparse-feed skip), a MISS (resting never filled /
    EH abandon), a no-signal note, or the replayed trade. Never a silent absence (the CLRO omission)."""
    from project_mai_tai.backtest.replay import build_replay_settings, replay_symbol_day
    from project_mai_tai.settings import get_settings

    obs = datetime(y, m, d, 8, 0, tzinfo=timezone.utc)          # 04:00 ET
    end = datetime(y, m, d + 1, 0, 0, tzinfo=timezone.utc)      # 20:00 ET
    syms = src.v2_qualified_symbols(obs, end)
    settings = build_replay_settings(base=get_settings(), eh_enabled=eh_enabled)
    day = f"{y}-{m:02d}-{d:02d}"
    out = [f"ATR/v2 sheet {day} — {len(syms)} qualified (tracked ∪ traded); REPLAY engine (real "
           f"strategy entry+exit){'; EH entry ON' if eh_enabled else ''}. Every name shown with a "
           f"reason (no silent absence)."]
    for sym in syms:
        res = replay_symbol_day(src, sym, day, settings)
        status, ok = classify_v2_feed(res.n_bars, res.n_quotes)
        out.append(f"\n== {sym} [FEED: {status}] ==")
        for sk in res.skips:
            out.append(f"  SKIP {sk.reason}: {sk.detail}")
        if not ok and not res.skips:
            out.append("   -> not backtestable (coverage gap)")
        for mi in res.misses:
            out.append(f"  MISS {mi.reason}: {mi.detail}")
        if ok and not res.entries and not res.misses:
            out.append("  0t — no ATR entry (no confirmed signal / vol-floor / no fill)")
        for e in res.entries:
            out.append(f"  ENTRY {e.mode}/{e.order_type} {_et(e.fill_ts)} @{e.fill_price:.4f} "
                       f"(ref {e.entry_ref:.4f})")
        for t in res.trades:
            out.append(f"     [{t.geometry}] -> {_et(t.exit_ts)} @{t.exit_px:.4f} "
                       f"[{t.exit_reason}] ret {t.ret_pct:+.2f}%")
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

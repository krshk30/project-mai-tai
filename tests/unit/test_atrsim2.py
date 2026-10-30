"""ATRSIM2 (operator 10-29): two tables, today only, one stored row per day, page never breaks."""
import importlib.util
import pathlib
from datetime import datetime, timezone

from project_mai_tai.atr_simulation.sim2_display import load_day, render_day

ROOT = pathlib.Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("atrsim2_job", ROOT / "tools" / "atrsim2_job.py")
job = importlib.util.module_from_spec(spec)
spec.loader.exec_module(job)


def row(**kw):
    base = dict(symbol="NCPL", signal="13:20", trigger=1.81, status="CLOSED", quantity=331, entry_time="13:20:13",
                entry_price=1.81, exit_time="13:26:00", exit_price=1.73, reason="sell flip", pnl=-26.48, pnl_pct=-4.42)
    base.update(kw)
    return base


from project_mai_tai.services.control_plane import _render_completed_position_row as render


def doc():
    return {"schema": "ATRSIM2_DAY_V1", "trading_day": "2026-10-29",
            "generated_at": datetime(2026, 10, 29, 21, 0, tzinfo=timezone.utc).isoformat(), "notional_usd": 600,
            "tables": {"FIRSTBAR": [row(), row(status="SKIPPED", reason="buy price never reached")],
                       "FIRSTBAR+2min": [row(exit_time="13:22:13", reason="not +1% in 2 min", pnl=-3.31, pnl_pct=-0.55)]}}


def test_page_shows_only_the_two_tables_with_traded_rows_and_totals():
    html = render_day(doc(), render_row=render, day="2026-10-29")
    assert html.count('data-sim-mode="FIRSTBAR"') == 1 and 'data-sim-mode="FIRSTBAR+2min"' in html
    assert "VOLBODY" not in html and "Live replay" not in html
    assert "1 trades, 1 signals without a trade" in html and "total $-26.48" in html
    assert "-4.42%" in html and "-0.55%" in html


def test_no_row_and_read_failure_never_break_the_page():
    assert "No simulation for 2026-10-30 yet" in render_day(None, render_row=render, day="2026-10-30")

    def broken():
        raise RuntimeError("db down")
    failed = load_day(broken, "2026-10-29")
    assert failed == {"unavailable": "RuntimeError"}
    assert "Simulation unavailable right now (RuntimeError)" in render_day(failed, render_row=render, day="2026-10-29")


def test_job_rules_target_cent_ceiling_and_round_lot_filter():
    assert job.ceil_cent(6.58 * 1.05) == 6.91
    assert job.ceil_cent(2.0 * 1.05) == 2.1
    assert job.eligible("") and job.eligible("14,41") and job.eligible(None)
    assert not job.eligible("37") and not job.eligible("12,37") and not job.eligible("16")


def test_scanner_filter_only_counts_signals_inside_confirm_fade_windows():
    t = lambda h, m: datetime(2026, 10, 29, h, m, tzinfo=timezone.utc)
    windows = {"PCLA": [(t(13, 5), t(13, 59)), (t(14, 21), None)], "NUWE": [(t(13, 3), t(13, 3))]}
    assert job.on_scanner(windows, "PCLA", t(13, 30))
    assert not job.on_scanner(windows, "PCLA", t(14, 0))
    assert job.on_scanner(windows, "PCLA", t(18, 0))
    assert not job.on_scanner(windows, "NUWE", t(14, 15))
    assert not job.on_scanner(windows, "JFIN", t(14, 15))


def test_live_row_uses_real_fills_in_the_signal_segment_including_inside_the_flip_bar():
    t = lambda h, m, s=0: datetime(2026, 10, 29, h, m, s, tzinfo=timezone.utc)
    bar = job.Bar(int(t(15, 33).timestamp() * 1000), 8.3, 8.42, 8.3, 8.4, 1000)
    fills = {"PCLA": [("buy", 36.0, 8.42, t(15, 33, 10)), ("sell", 36.0, 8.85, t(15, 54, 10))]}
    base = {"symbol": "PCLA", "signal": "11:34", "trigger": 8.43}
    row = job.live_row(fills, "PCLA", bar, t(17, 53), t(23, 59), base)
    assert row["status"] == "CLOSED" and row["entry_price"] == 8.42 and row["exit_price"] == 8.85
    assert row["pnl"] == 15.48 and row["pnl_pct"] == 5.11
    later = job.Bar(int(t(18, 14).timestamp() * 1000), 10.5, 10.6, 10.4, 10.55, 1000)
    assert job.live_row(fills, "PCLA", later, None, t(23, 59), base)["status"] == "NO LIVE TRADE"


def test_no_entry_print_counts_before_the_scanner_confirms():
    # Coach 10-29 check h: VEEA 10-01 modeled fill 09:07:21 vs CONFIRM 09:07:30.417 — must not fill.
    t = lambda h, m, s=0: datetime(2026, 10, 1, h, m, s, tzinfo=timezone.utc)
    job.WINDOWS = {"VEEA": [(t(13, 7, 30), None)]}
    try:
        assert not job.confirmed("VEEA", t(13, 7, 21))
        assert job.confirmed("VEEA", t(13, 7, 31))
        assert not job.confirmed("OTHER", t(13, 7, 31))
    finally:
        job.WINDOWS = None
    assert job.confirmed("VEEA", t(13, 7, 21))  # outside a run (no windows loaded) nothing is filtered


def test_flip_query_is_bounded_to_the_trading_day():
    seen = {}

    class Conn:
        def execute(self, sql, params):
            seen.update(sql=str(sql), params=params)

            class R:
                def all(self):
                    return []
            return R()
    anchor = datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc)
    assert job.flips_by_symbol(Conn(), anchor) == {}
    assert "bar_time < :b" in seen["sql"] and (seen["params"]["b"] - anchor).days == 1

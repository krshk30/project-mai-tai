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

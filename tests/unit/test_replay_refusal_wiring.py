"""P1 — R4 wiring: the replay engine must honour the broker-refusal model.

⛔⭐⭐ THE DEFECT. `broker_refusal.py` shipped complete and with ZERO importers — a refusal model
nothing consulted. The engine's implicit broker model stayed "orders fill", which is how it books
P&L from trades that could never have happened.

⛔⭐ THE ORDER NEVER EXISTS. A refused name is not a fill priced differently; it is an order the
broker will not accept at all. So the check runs BEFORE any tape is loaded, and
`test_refused_symbol_never_touches_the_tape` pins that — if the model were consulted after the fill
model, a future edit could quietly start pricing a trade that cannot occur.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from project_mai_tai.backtest.broker_refusal import build_refusal_model
from project_mai_tai.backtest.replay import (
    RealEntry,
    build_replay_settings,
    load_refusal_model,
    reconcile_day,
    replay_symbol_day,
)

EASTERN = ZoneInfo("America/New_York")
DAY = "2026-08-12"

REFUSED_REASON = "Opening transactions for this security must be placed with a broker"


class _CountingSource:
    """Records whether the tape was ever asked for. A refused name must never reach it."""

    def __init__(self):
        self.bar_calls = 0
        self.quote_calls = 0

    def schwab_bars(self, symbol, start, end):
        self.bar_calls += 1
        return []

    def schwab_quotes(self, symbol, start, end):
        self.quote_calls += 1
        return []

    def trades(self, symbol, start, end):
        return []


def _model(rows):
    return build_refusal_model(rows)


def test_refused_symbol_is_skipped_with_a_named_reason():
    model = _model([("CRWU", REFUSED_REASON)])
    res = replay_symbol_day(
        _CountingSource(), "CRWU", DAY, build_replay_settings(), refusal_model=model
    )
    assert res.entries == []
    assert [s.reason for s in res.skips] == ["broker_refused"]
    # ⛔ "no entry" must never be indistinguishable from "no signal".
    assert "never exists" in res.skips[0].detail


def test_refused_symbol_never_touches_the_tape():
    """⛔⭐ The order does not exist, so the engine must not even load the day's data for it."""
    src = _CountingSource()
    replay_symbol_day(
        src, "CRWU", DAY, build_replay_settings(), refusal_model=_model([("CRWU", REFUSED_REASON)])
    )
    assert src.bar_calls == 0
    assert src.quote_calls == 0


def test_symbol_not_in_the_model_still_loads_the_tape():
    """The refusal model must not become a blanket off-switch for the engine."""
    src = _CountingSource()
    replay_symbol_day(
        src, "IPST", DAY, build_replay_settings(), refusal_model=_model([("CRWU", REFUSED_REASON)])
    )
    assert src.bar_calls == 1


def test_case_insensitive_symbol_match():
    model = _model([("crwu", REFUSED_REASON)])
    res = replay_symbol_day(
        _CountingSource(), "CRWU", DAY, build_replay_settings(), refusal_model=model
    )
    assert [s.reason for s in res.skips] == ["broker_refused"]


def test_only_the_symbol_level_class_excludes_the_name():
    """⛔ The four classes are modelled DIFFERENTLY and must not collapse into one refusal rate.

    A trigger-not-above-ask reject is a submit-time event that depends on where the tape was; it
    does NOT make the name untradeable. Excluding it would delete legitimate trades.
    """
    src = _CountingSource()
    model = _model([("SCKT", "Stop price must be above the current ask")])
    replay_symbol_day(src, "SCKT", DAY, build_replay_settings(), refusal_model=model)
    assert src.bar_calls == 1  # NOT excluded


def test_no_model_means_the_header_says_so_out_loud():
    """⛔ A silently absent refusal model looks exactly like one that found nothing."""
    report = reconcile_day(_CountingSource(), DAY, build_replay_settings(), [])
    assert "NONE APPLIED" in report
    assert "R4 off" in report


def test_supplied_header_is_printed_before_any_number():
    report = reconcile_day(
        _CountingSource(),
        DAY,
        build_replay_settings(),
        [],
        refusal_header="[causal] REFUSAL MODEL | x",
    )
    body = report.splitlines()
    assert "[causal]" in body[1]
    assert body.index([ln for ln in body if ln.startswith("real v2 entries")][0]) > 1


def test_refused_name_with_a_real_entry_reports_no_replay_entry():
    """The reconciliation still lists the real trade — it just cannot replay one."""
    real = [RealEntry("CRWU", 1.23, datetime(2026, 8, 12, 10, 0, tzinfo=EASTERN), "resting")]
    report = reconcile_day(
        _CountingSource(),
        DAY,
        build_replay_settings(),
        real,
        refusal_model=_model([("CRWU", REFUSED_REASON)]),
    )
    assert "SKIP  broker_refused" in report
    assert "REPLAY: (no entry)" in report


# ---------------------------------------------------------------------------
# ⛔⭐⭐ The window is a LOOK-AHEAD decision — pinned in both directions.
# ---------------------------------------------------------------------------


class _CapturingFactory:
    """Captures the bind params the loader used, so the window can be asserted exactly."""

    def __init__(self):
        self.params = None

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, _stmt, params):
        self.params = params
        return self

    def all(self):
        return []


def test_causal_window_ends_at_the_replayed_day():
    """⛔ A reject observed AFTER the replayed day is knowledge the engine did not have."""
    f = _CapturingFactory()
    _, mode = load_refusal_model(
        f, account="live:schwab_1m_v2", session_day_et=DAY, lookback_days=30
    )
    assert mode == "causal"
    assert f.params["end"] == datetime(2026, 8, 12, tzinfo=EASTERN)
    assert f.params["start"] == datetime(2026, 8, 12, tzinfo=EASTERN) - timedelta(days=30)


def test_hindsight_window_extends_past_the_replayed_day():
    f = _CapturingFactory()
    _, mode = load_refusal_model(
        f,
        account="live:schwab_1m_v2",
        session_day_et=DAY,
        lookback_days=30,
        include_same_day_and_later=True,
    )
    assert mode == "hindsight"
    assert f.params["end"] == datetime(2026, 8, 12, tzinfo=EASTERN) + timedelta(days=30)


def test_the_account_is_passed_through_not_assumed():
    """⛔ Every reject query states which account it can see."""
    f = _CapturingFactory()
    load_refusal_model(f, account="live:orb", session_day_et=DAY)
    assert f.params["account"] == "live:orb"

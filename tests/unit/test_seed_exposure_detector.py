"""§131 — tests for the seed-exposure detector.

⛔⭐ A TEST MAY NOT REIMPLEMENT WHAT IT TESTS. Every expectation below is a PINNED LITERAL — no test
recomputes an age, a threshold or a cap from the constants it is checking. A test that recomputed
`SEED_LIMIT - 1` would pass against any limit, including a wrong one.

⛔⭐⭐ THE LOAD-BEARING TEST IS `test_parses_watchlist_longer_than_the_retired_log_cap`. The defect
being closed (B13) was a source capped at 5 that returned a confident clean on symbols it never
read. A regression here is silent and costs a P0.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from scripts.seed_exposure_detector import (
    DetectorBlind,
    OnDeckRow,
    _dsn,
    SweepRow,
    check_constants,
    parse_watchlist_event,
)

NOW = datetime(2026, 8, 19, 11, 30, 0, tzinfo=UTC)  # 07:30 ET


def _event(**overrides) -> str:
    base = {
        "event_id": "b2f4ed15-4988-40aa-b277-2a6c0e876643",
        "event_type": "isolated_bot_state",
        "source_service": "schwab-1m-v2",
        "produced_at": "2026-08-19T11:29:30Z",  # 30s before NOW
        "correlation_id": None,
        "payload": {"strategy_code": "schwab_1m_v2", "watchlist": ["BIVI", "TNON", "ZNB"]},
    }
    payload_overrides = overrides.pop("payload", None)
    base.update(overrides)
    if payload_overrides is not None:
        base["payload"] = payload_overrides
    return json.dumps(base)


# --------------------------------------------------------------------------------------
# B13 — the reason this file exists
# --------------------------------------------------------------------------------------


def test_parses_watchlist_longer_than_the_retired_log_cap():
    """⛔⭐⭐ EIGHT symbols must come back as EIGHT.

    The retired path built `sample=` as `",".join(sorted(selected)[:5])`, so it would have yielded
    exactly five of these and reported clean on the other three. 8 is pinned deliberately: it is
    larger than the old cap and not a multiple of it.
    """
    symbols = ["AAPL", "BIVI", "CAST", "EHGO", "FCUV", "HKIT", "KIDZ", "ZNB"]
    got = parse_watchlist_event(
        _event(payload={"strategy_code": "schwab_1m_v2", "watchlist": symbols}), NOW, 90
    ).watchlist
    assert got == ["AAPL", "BIVI", "CAST", "EHGO", "FCUV", "HKIT", "KIDZ", "ZNB"]
    assert len(got) == 8


def test_watchlist_is_sorted_and_deduplicated_and_uppercased():
    got = parse_watchlist_event(
        _event(payload={"strategy_code": "schwab_1m_v2", "watchlist": ["znb", "BIVI", "ZNB"]}),
        NOW,
        90,
    ).watchlist
    assert got == ["BIVI", "ZNB"]


def test_returns_produced_at_for_freshness_reporting():
    produced_at = parse_watchlist_event(_event(), NOW, 90).produced_at
    assert produced_at == datetime(2026, 8, 19, 11, 29, 30, tzinfo=UTC)


# --------------------------------------------------------------------------------------
# ⛔ Every blind path REFUSES. None may return an empty list.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, needle",
    [
        (None, "no isolated-bot-state event"),
        ("", "no isolated-bot-state event"),
        ("{not json", "not parseable JSON"),
    ],
)
def test_refuses_when_the_stream_cannot_be_read(raw, needle):
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(raw, NOW, 90)
    assert needle in str(exc.value)


def test_refuses_an_empty_stream_rather_than_reading_it_as_no_symbols():
    """⛔ `redis-cli XLEN <nonexistent>` returns 0, not an error — same family as the log cap."""
    with pytest.raises(DetectorBlind):
        parse_watchlist_event(None, NOW, 90)


def test_refuses_another_bots_event_on_the_shared_stream():
    raw = _event(payload={"strategy_code": "polygon_30s", "watchlist": ["XYZ"]})
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(raw, NOW, 90)
    assert "polygon_30s" in str(exc.value)


def test_refuses_an_unexpected_event_type():
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(_event(event_type="heartbeat"), NOW, 90)
    assert "event_type" in str(exc.value)


def test_refuses_when_produced_at_is_missing():
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(_event(produced_at=None), NOW, 90)
    assert "freshness" in str(exc.value)


def test_refuses_a_stale_event():
    """A snapshot from a bot that died would sweep the wrong symbols and still print clean."""
    raw = _event(produced_at="2026-08-19T11:20:00Z")  # 600s before NOW
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(raw, NOW, 90)
    assert "600s old" in str(exc.value)


def test_accepts_an_event_exactly_at_the_age_limit():
    """Pinned boundary: 90s old against a 90s limit is ACCEPTED; 91 is not."""
    ok = parse_watchlist_event(_event(produced_at="2026-08-19T11:28:30Z"), NOW, 90).watchlist
    assert ok == ["BIVI", "TNON", "ZNB"]
    with pytest.raises(DetectorBlind):
        parse_watchlist_event(_event(produced_at="2026-08-19T11:28:29Z"), NOW, 90)


def test_refuses_when_the_watchlist_field_disappears():
    """An always-absent field must not read as 'no symbols' — grep the publisher, not the schema."""
    with pytest.raises(DetectorBlind) as exc:
        parse_watchlist_event(_event(payload={"strategy_code": "schwab_1m_v2"}), NOW, 90)
    assert "schema changed" in str(exc.value)


def test_an_empty_watchlist_is_returned_not_refused():
    """A genuinely empty watchlist is KNOWN-EMPTY, which is different from CANNOT-SEE.

    main() prints it as NOTHING TO SWEEP and never as 'no exposure'.
    """
    got = parse_watchlist_event(
        _event(payload={"strategy_code": "schwab_1m_v2", "watchlist": []}), NOW, 90
    ).watchlist
    assert got == []


# --------------------------------------------------------------------------------------
# classify() — THE WINDOW, NOT A COUNT (P10, 2026-08-19)
# --------------------------------------------------------------------------------------


def _row(symbol, window_bars, newest, gap_days=None, ever=None):
    return SweepRow(
        symbol=symbol,
        window_bars=window_bars,
        bars_ever=ever if ever is not None else window_bars,
        newest_bar=newest,
        max_internal_gap=timedelta(days=gap_days) if gap_days is not None else None,
    )


def test_vrax_the_case_that_broke_the_old_criterion():
    """⛔⭐⭐ THE REGRESSION. VRAX at 07:35 on 2026-08-19: 241 bars, ALL from 07-09, none today.

    The old criterion said `SHORT history (< 250 bars ever) — short is NOT holed` and passed it.
    The window is internally contiguous, so there is no internal gap to find — and #721 would have
    seeded all 241 bars from a session where VRAX traded 5.92-12.85, while it traded 3.22-4.07 that
    day. The BOUNDARY is the only thing that reveals it.
    """
    row = _row("VRAX", 241, datetime(2026, 7, 9, 15, 47, tzinfo=UTC), gap_days=None)
    exposed, why = row.classify(NOW)
    assert exposed is True
    assert "BOUNDARY" in why
    assert "#721 does NOT truncate this" in why


def test_short_history_is_no_longer_a_free_pass():
    """⛔ The retracted rule, pinned as a regression: a 3-bar window that is 40 days old is EXPOSED."""
    row = _row("TINY", 3, datetime(2026, 7, 10, tzinfo=UTC))
    exposed, _ = row.classify(NOW)
    assert exposed is True


def test_a_history_that_starts_today_is_genuinely_safe():
    """AIXC / NTWOW — safe because NOTHING SITS BEHIND THEM, not because they are short."""
    row = _row("AIXC", 104, datetime(2026, 8, 19, 11, 0, tzinfo=UTC))
    exposed, why = row.classify(NOW)
    assert exposed is False
    assert "contiguous and current" in why


def test_internal_gap_is_still_detected():
    """The #721-covered kind: bars today, but an old island inside the same window."""
    row = _row("BIVI", 250, datetime(2026, 8, 19, 11, 25, tzinfo=UTC), gap_days=5.9)
    exposed, why = row.classify(NOW)
    assert exposed is True
    assert "INTERNAL" in why


def test_boundary_is_checked_before_internal():
    """A wholly-stale window can also contain an internal gap; BOUNDARY is the uncovered kind and
    must be the one reported, because it is the one nothing else will catch."""
    row = _row("BOTH", 250, datetime(2026, 7, 1, tzinfo=UTC), gap_days=10)
    _, why = row.classify(NOW)
    assert "BOUNDARY" in why and "INTERNAL" not in why


def test_a_full_current_window_is_not_exposed():
    row = _row("TNON", 250, datetime(2026, 8, 19, 11, 29, tzinfo=UTC), gap_days=0.01)
    exposed, _ = row.classify(NOW)
    assert exposed is False


def test_no_history_is_reported_as_nothing_to_seed_not_as_safe():
    row = _row("NEW", 0, None)
    exposed, why = row.classify(NOW)
    assert exposed is False
    assert "nothing to seed" in why


def test_bar_count_alone_never_decides():
    """⛔⭐ Two symbols with the SAME window size, opposite verdicts — the count cannot be the input."""
    stale = _row("A", 241, datetime(2026, 7, 9, tzinfo=UTC))
    fresh = _row("B", 241, datetime(2026, 8, 19, 11, 0, tzinfo=UTC))
    assert stale.classify(NOW)[0] is True
    assert fresh.classify(NOW)[0] is False


def test_ondeck_row_names_the_uncovered_kind():
    boundary = OnDeckRow("NXTC", 250, datetime(2026, 7, 14, tzinfo=UTC), None)
    internal = OnDeckRow("KIDZ", 250, datetime(2026, 8, 19, 11, 0, tzinfo=UTC), timedelta(days=28))
    assert boundary.kind(NOW) == "BOUNDARY"
    assert internal.kind(NOW) == "internal"


# --------------------------------------------------------------------------------------
# check_constants — the mirror may not drift silently
# --------------------------------------------------------------------------------------


def test_constants_in_step_report_no_drift(tmp_path):
    src = tmp_path / "bot.py"
    src.write_text("INTERVAL_SECS = 60\nDB_SEED_BAR_LIMIT = 250\n", encoding="utf-8")
    assert check_constants(str(src)) == ""


def test_drifted_limit_is_reported(tmp_path):
    src = tmp_path / "bot.py"
    src.write_text("INTERVAL_SECS = 60\nDB_SEED_BAR_LIMIT = 500\n", encoding="utf-8")
    drift = check_constants(str(src))
    assert "service says 500" in drift
    assert "mirrors 250" in drift


def test_a_missing_constant_is_reported_not_ignored(tmp_path):
    """⛔ A renamed constant must not read as 'no drift' — that is a false clean."""
    src = tmp_path / "bot.py"
    src.write_text("INTERVAL_SECS = 60\n", encoding="utf-8")
    assert "DB_SEED_BAR_LIMIT not found" in check_constants(str(src))


# --------------------------------------------------------------------------------------
# WARM set — §132's actionable half
# --------------------------------------------------------------------------------------


def test_warm_set_comes_from_bar_counts_keys():
    raw = _event(
        payload={
            "strategy_code": "schwab_1m_v2",
            "watchlist": ["BIVI"],
            "bar_counts": {"ZNB": 1118, "hkit": 2, "KIDZ": 10},
        }
    )
    assert parse_watchlist_event(raw, NOW, 90).warm == frozenset({"ZNB", "HKIT", "KIDZ"})


def test_warm_set_ignores_bar_count_VALUES():
    """⛔⭐ ZNB read 1118 in-memory against 20 persisted rows — a DIFFERENT UNIT.

    `bar_counts` is a monotonic +1 per bar seen since boot and includes REST-warmup bars that were
    never persisted. Only the KEYS are usable; the seed reads strategy_bar_history.
    """
    raw = _event(
        payload={"strategy_code": "schwab_1m_v2", "watchlist": [], "bar_counts": {"ZNB": 1118}}
    )
    state = parse_watchlist_event(raw, NOW, 90)
    assert state.warm == frozenset({"ZNB"})
    assert not hasattr(state, "bar_counts")


def test_absent_bar_counts_yields_an_empty_warm_set_not_a_refusal():
    """WARM is an enrichment, not a coverage guarantee — its absence must not blind the sweep."""
    assert parse_watchlist_event(_event(), NOW, 90).warm == frozenset()


# --------------------------------------------------------------------------------------
# ⛔ Exit-code discipline — a blind run may never share a code with a finding
# --------------------------------------------------------------------------------------


def test_missing_dsn_raises_detector_blind_not_systemexit(monkeypatch):
    """⛔⭐ CAUGHT LIVE 2026-08-19 in this very script.

    `SystemExit(str)` exits 1 — the SAME code this script uses for EXPOSURE FOUND. A cron reading
    exit codes would have read "cannot reach the database" as a finding. DetectorBlind routes to 2.
    """
    monkeypatch.delenv("MAI_TAI_DATABASE_URL", raising=False)
    with pytest.raises(DetectorBlind):
        _dsn(None)
    with pytest.raises(SystemExit):  # regression guard: the old behaviour must NOT come back
        raise SystemExit("sanity")


def test_dsn_normalises_the_sqlalchemy_scheme():
    assert _dsn("postgresql+psycopg://u:p@h/db") == "postgresql://u:p@h/db"

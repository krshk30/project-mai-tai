"""F3 fleet function-health — unit-tests the pure verdict logic of the independent
check script (ops/health/fleet_health_check.py). Loaded by path (the script is stdlib-only
and imports NO app code, so it stays independent/unhangable); we test only its decision
functions, not the psql/redis I/O. The load-bearing property proven here is the
NO-FALSE-ALARM discipline: stale bars are RED only when the upstream feed is simultaneously
live (a frozen loop) — never on a quiet market / feed outage."""
from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

_MOD_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "fleet_health_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("fleet_health_check", _MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fhc = _load()


def test_fresh_bars_with_live_feed_is_green():
    level, _ = fhc.classify_bar_freshness(30, 3)
    assert level == "GREEN"


def test_stale_bars_with_LIVE_feed_is_red_frozen_loop():
    level, detail = fhc.classify_bar_freshness(300, 5)
    assert level == "RED"
    assert "FROZEN" in detail


def test_stale_bars_with_QUIET_feed_is_green_no_false_alarm():
    # THE no-false-alarm guarantee: bars stale but the upstream feed is quiet/stale is a
    # quiet market or a feed outage — NOT a strategy fault. Must never RED.
    assert fhc.classify_bar_freshness(600, 400)[0] == "GREEN"   # feed stale
    assert fhc.classify_bar_freshness(600, None)[0] == "GREEN"  # no recent trades at all


def test_slowing_bars_with_live_feed_is_amber():
    assert fhc.classify_bar_freshness(150, 5)[0] == "AMBER"


def test_no_bars_is_amber_not_red():
    # Can't assess (no data) is AMBER (look), never RED (don't cry wolf).
    assert fhc.classify_bar_freshness(None, 5)[0] == "AMBER"


# --- check #2: oms-order-lifecycle (alive-but-not-executing) ------------------ #

def test_no_stuck_intents_is_green_quiet_or_executing():
    # THE no-false-alarm guard: no stuck intents -> GREEN, whether the market is quiet
    # (no intents) or the OMS is executing normally.
    assert fhc.classify_order_lifecycle(0, None)[0] == "GREEN"


def test_stuck_intents_is_red_alive_but_not_executing():
    level, detail = fhc.classify_order_lifecycle(3, 12)
    assert level == "RED"
    assert "not-executing" in detail or "not executing" in detail


def test_unreadable_intents_is_amber_not_red():
    assert fhc.classify_order_lifecycle(None, None)[0] == "AMBER"


# --- check #5: independently watch the D6 scheduler's durable success marker ------------------ #


def test_d6_current_success_is_green() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-28 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )
    assert level == "GREEN"
    assert "session=2026-08-28" in detail


def test_d6_yesterdays_success_is_red_when_a_new_session_is_due() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-27 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )
    assert level == "RED"
    assert "stale" in detail


def test_d6_future_success_is_red_instead_of_blessing_the_wrong_session() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-29 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )

    assert level == "RED"
    assert "future" in detail


def test_d6_current_nonpass_and_missing_status_are_red() -> None:
    expected = date(2026, 8, 28)
    assert fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-NONPASS] session=2026-08-28 verdict=FAIL\n",
        expected_session=expected,
    )[0] == "RED"
    assert fhc.classify_d6_status(None, expected_session=expected)[0] == "RED"


def test_d6_malformed_session_is_red_not_an_amber_check_exception() -> None:
    level, detail = fhc.classify_d6_status(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-99-99 verdict=PASS\n",
        expected_session=date(2026, 8, 28),
    )

    assert level == "RED"
    assert "malformed" in detail


def test_d6_binary_status_is_red_not_an_amber_check_exception(
    monkeypatch, tmp_path
) -> None:
    status = tmp_path / "STATUS.txt"
    status.write_bytes(b"\xff\xfe\x00\x80")
    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", status)
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert level == "RED"
    assert name == "d6-outcome-acceptance"
    assert "encoding error=UnicodeDecodeError" in detail


def test_d6_freshness_green_path_reads_the_current_success(
    monkeypatch, tmp_path
) -> None:
    status = tmp_path / "STATUS.txt"
    status.write_text(
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS] session=2026-08-28 verdict=PASS\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", status)
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("GREEN", "d6-outcome-acceptance")
    assert "D6 SUCCESS current" in detail


def test_d6_permission_failure_is_red_and_distinct_from_missing(monkeypatch) -> None:
    class PermissionDeniedStatus:
        def read_text(self, *, encoding: str) -> str:
            raise PermissionError("root-only")

    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", PermissionDeniedStatus())
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("RED", "d6-outcome-acceptance")
    assert "permission error=PermissionError" in detail
    assert "missing" not in detail


def test_d6_other_oserror_is_red_and_names_the_io_failure(monkeypatch) -> None:
    class BrokenStatus:
        def read_text(self, *, encoding: str) -> str:
            raise OSError("I/O failure")

    monkeypatch.setattr(fhc, "_D6_STATUS_PATH", BrokenStatus())
    monkeypatch.setattr(
        fhc,
        "_last_completed_session_day",
        lambda _today: date(2026, 8, 28),
    )

    level, name, detail = fhc.check_d6_status_freshness()

    assert (level, name) == ("RED", "d6-outcome-acceptance")
    assert "I/O error=OSError" in detail
    assert "missing" not in detail


def test_d6_freshness_check_is_registered_in_the_executed_check_list() -> None:
    assert fhc.check_d6_status_freshness in fhc.CHECKS


def test_d6_expected_session_skips_weekend_and_full_closure() -> None:
    assert fhc._last_completed_session_day(date(2026, 9, 8)) == date(2026, 9, 4)


# --- check #3: stops-armed (every OMS-owned open position has an armed stop) --- #

def test_owned_position_with_stop_is_green():
    # 2 OMS-owned open, 0 unprotected → all armed → GREEN.
    assert fhc.classify_stops_armed(0, 2)[0] == "GREEN"


def test_owned_position_without_stop_is_red_naked():
    level, detail = fhc.classify_stops_armed(1, 1)
    assert level == "RED"
    assert "NAKED" in detail


def test_flat_is_green_nothing_to_protect():
    # No OMS-owned open positions → nothing to protect → GREEN, never RED.
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_manual_position_is_ignored_green():
    # SCOPING INVARIANT: a manual holding has no virtual_positions row, so the query never
    # counts it → unprotected stays 0 → GREEN. (The virtual_positions-only source is what
    # enforces this; live-validated. Here we assert the verdict for that count state.)
    assert fhc.classify_stops_armed(0, 0)[0] == "GREEN"


def test_unreadable_stops_is_amber_not_red():
    assert fhc.classify_stops_armed(None, None)[0] == "AMBER"


# ---------------------------------------------------------------------------------------------
# 2026-08-03 — PAPER CONTAMINATION. Unscoped checks let polygon_30s (PAPER/sim) drive a page.
# It had already reached the entries counter, the sawtooth check and #628 before this sweep.
# This pager guards NAKED POSITIONS; teaching the operator to ignore it is the real damage.
# ---------------------------------------------------------------------------------------------


def test_real_money_accounts_are_pinned_by_VALUE() -> None:
    """⛔ `name LIKE 'live:%'` is NOT a safe proxy — `live:polygon_30s` and `live:webull_30s`
    both exist as broker_accounts rows. Verified 2026-08-03: only live:schwab_1m_v2, live:orb
    and paper:polygon_30s had any order in 30 days. Pin the VALUE, not just its use."""
    assert fhc.REAL_MONEY_ACCOUNTS == ("live:schwab_1m_v2", "live:orb")
    assert not any("polygon" in a for a in fhc.REAL_MONEY_ACCOUNTS)
    assert not any(a.startswith("paper:") for a in fhc.REAL_MONEY_ACCOUNTS)


def test_the_sql_list_quotes_every_account() -> None:
    for acct in fhc.REAL_MONEY_ACCOUNTS:
        assert f"'{acct}'" in fhc._REAL_MONEY_SQL_LIST


def test_a_paper_note_never_changes_the_level() -> None:
    """Paper stays VISIBLE (polygon_30s really does reject every STOP sell) but can never page."""
    base_level, base_detail = fhc.classify_stops_armed(0, 0)
    noted = fhc._with_paper_note(base_detail, 3, "unprotected sim position")
    assert base_level == "GREEN"
    assert "paper/sim: 3 unprotected sim positions" in noted
    assert "not paged" in noted


def test_zero_paper_adds_nothing() -> None:
    assert fhc._with_paper_note("all clear", 0, "x") == "all clear"
    assert fhc._with_paper_note("all clear", None, "x") == "all clear"


def test_scoping_did_NOT_make_the_check_always_green() -> None:
    """⛔⭐ THE ACCEPTANCE CRITERION. Scoping must not silence the real condition — a REAL
    unprotected real-money position and a REAL lifecycle stall must still go RED. Always-green
    is strictly worse than always-red: it reads as health."""
    assert fhc.classify_stops_armed(1, 1)[0] == "RED"
    assert fhc.classify_order_lifecycle(3, 12)[0] == "RED"
    # ...and a paper note must not soften either one.
    red_level, red_detail = fhc.classify_stops_armed(1, 1)
    assert red_level == "RED"
    assert "NAKED" in fhc._with_paper_note(red_detail, 5, "unprotected sim position")


def test_the_bar_hole_verdict_no_longer_prescribes_a_restart() -> None:
    """⛔ The alert used to say "Restart v2 so the REST warmup refetches a contiguous series."
    #620 already guards live ATR, and a restart PUNCHES A FRESH HOLE — the very condition this
    check detects. An alert that reliably recommends the wrong action is worse than no alert."""
    level, detail = fhc.classify_bar_continuity(worst_gap_min=10, gap_symbols=1, bars_seen=50)
    assert level == "RED"
    assert "Restart v2" not in detail
    assert "Do NOT restart on this alert alone" in detail
    assert "#620" in detail, "must point at the guard that actually protects live ATR"


def test_the_halt_downgrade_cannot_fire_on_a_REST_FAILURE() -> None:
    """⛔⭐ "INSERTED 0" is equally true when the REST call ERRORED. Downgrading on that would
    turn a DUAL-SOURCE OUTAGE (streamer dead AND REST dead) into an informational note — the
    worst outcome for this pager. Not hypothetical: Schwab REST 401'd for 2h41m on 2026-08-03.
    Pins that the wrapper's downgrade is gated on REST_FAILED == 0."""
    from pathlib import Path
    wrapper = Path(__file__).resolve().parents[2] / "ops" / "health" / "bar_gap_watch_cron.sh"
    src = wrapper.read_text(encoding="utf-8")
    assert "REST fetch FAILED" in src, "the failure signal must be parsed at all"
    assert '[ "${REST_FAILED:-0}" -eq 0 ]' in src, "downgrade must require ZERO REST failures"
    assert "HALT_DOWNGRADE=0" in src, "must default to NOT downgrading"

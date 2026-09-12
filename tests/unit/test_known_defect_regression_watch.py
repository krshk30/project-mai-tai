from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
import importlib.util
import os
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "known_defect_regression_watch.py"
SPEC = importlib.util.spec_from_file_location("known_defect_regression_watch", SCRIPT)
assert SPEC and SPEC.loader
watch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = watch
SPEC.loader.exec_module(watch)

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
SINCE = NOW - timedelta(hours=8)


def lines(*payloads: tuple[int, str]):
    raw = [f"2026-09-11 {hour:02d}:00:00,000 INFO test {payload}" for hour, payload in payloads]
    return watch.parse_log_lines(raw, since=SINCE, until=NOW)


def atr_sell(symbol: str, hour: int) -> str:
    bar_at = datetime(2026, 9, 11, hour, tzinfo=UTC)
    return (
        f"[V2-ATR-PROBE] sym={symbol} ts_ms={int(bar_at.timestamp() * 1000)} "
        "state=short flip=SELL fired_seg=false"
    )


def reading(key: str, verdict: str) -> object:
    recurrence = int(verdict == watch.RECURRENCE)
    return watch.Reading(
        key, verdict, 1, int(not recurrence), recurrence, recurrence > 0, "evidence"
    )


def database_metrics(**overrides: int) -> dict[str, int]:
    result = {
        "managed_exit_orders_schwab": 0,
        "managed_exit_orders_webull": 0,
        "confirmed_exit_releases_schwab": 0,
        "confirmed_exit_releases_webull": 0,
        "reserved_share_reject_orders_webull": 0,
        "owned_open_rows_schwab": 0,
        "owned_open_rows_webull": 0,
        "owned_fresh_rows_schwab": 0,
        "owned_fresh_rows_webull": 0,
        "virtual_zero_held_rows_schwab": 0,
        "virtual_zero_held_rows_webull": 0,
    }
    result.update(overrides)
    return result


def test_catalog_has_every_requested_defect_and_every_row_declares_both_polarities() -> None:
    assert len(watch.CATALOG) == 19
    assert len({row.key for row in watch.CATALOG}) == 19
    assert {row.mode for row in watch.CATALOG} == {"ARMED", "DELEGATED", "UNARMED"}
    assert {row.key for row in watch.CATALOG if row.mode == "ARMED"} == {
        "BOOT1",
        "ROLL1",
        "OWNERROLL1",
        "SLOTCLEAR1",
        "LIQPULL1",
        "PHANTOM1",
        "RESERVE1",
        "W4291",
        "VPZERO1",
        "SEED1",
    }
    assert {row.key for row in watch.CATALOG if row.mode == "UNARMED"} == {
        "DISARM1",
        "REDIS1",
        "SAW1",
        "CEILING1",
        "FALSEFLAT1",
    }
    for row in watch.CATALOG:
        assert row.evidence
        assert row.guard_working_shape
        assert row.recurrence_shape
        if row.mode == "UNARMED":
            assert row.recurrence_shape.startswith("UNARMED:")
        if row.mode == "DELEGATED":
            assert row.delegated_to


def test_boot_release_after_completion_is_guard_working() -> None:
    result = watch.evaluate_boot(
        lines(
            (5, "[V2-BOOT-HOLD] HELD restoration_complete=0"),
            (6, "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=4 confirmed=4"),
            (7, "[V2-BOOT-HOLD] released restoration_complete=1"),
        ),
        now=NOW,
    )
    assert result.verdict == watch.GUARD_WORKING
    assert (result.evaluated, result.guard_working, result.recurrence) == (1, 1, 0)


def test_boot_release_before_completion_is_recurrence() -> None:
    result = watch.evaluate_boot(
        lines((5, "[V2-BOOT-HOLD] released restoration_complete=1")), now=NOW
    )
    assert result.verdict == watch.RECURRENCE
    assert result.recurrence == 1


def test_completed_boot_that_stays_held_is_recurrence() -> None:
    result = watch.evaluate_boot(
        lines((6, "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=4 confirmed=4")),
        now=NOW,
    )
    assert result.verdict == watch.RECURRENCE
    assert "completion_still_held" in result.detail


def test_session_roll_requires_the_exact_current_anchor() -> None:
    anchor = watch.session_anchor(NOW)
    current = int(anchor.timestamp() * 1000)
    old = current - 86_400_000
    stale = lines((5, f"[V2-SESSION-ROLL] boundary_crossed=True rolled=0 (anchor={old}"))
    good = lines((5, f"[V2-SESSION-ROLL] boundary_crossed=True rolled=0 (anchor={current}"))
    assert watch.evaluate_session_roll(stale, now=NOW, market_day=True).verdict == watch.RECURRENCE
    assert (
        watch.evaluate_session_roll(good, now=NOW, market_day=True).verdict == watch.GUARD_WORKING
    )


def test_session_roll_is_unexercised_before_its_due_time() -> None:
    before = datetime(2026, 9, 11, 8, 5, tzinfo=UTC)  # 04:05 ET
    assert watch.evaluate_session_roll([], now=before, market_day=True).verdict == watch.UNEXERCISED


def test_owner_roll_reports_guard_working_only_when_rolled_owners_stay_cleared() -> None:
    anchor = watch.session_anchor(NOW)
    anchor_ms = int(anchor.timestamp() * 1000)
    observed = watch.parse_log_lines(
        [
            "2026-09-11 08:00:02,000 INFO x "
            f"[V2-SESSION-ROLL] boundary_crossed=True rolled=2 symbols=DBGI,TNON "
            f"(anchor={anchor_ms} watchlist=2)",
        ],
        since=SINCE,
        until=NOW,
    )

    result = watch.evaluate_owner_roll(observed, now=NOW, market_day=True)

    assert result.verdict == watch.GUARD_WORKING
    assert (result.evaluated, result.guard_working, result.recurrence) == (2, 2, 0)


def test_owner_roll_detects_the_live_pre_fix_repopulation_sequence() -> None:
    anchor = watch.session_anchor(NOW)
    anchor_ms = int(anchor.timestamp() * 1000)
    observed = watch.parse_log_lines(
        [
            "2026-09-11 08:00:02,126 INFO x "
            f"[V2-SESSION-ROLL] boundary_crossed=True rolled=2 symbols=DBGI,TNON "
            f"(anchor={anchor_ms} watchlist=2)",
            "2026-09-11 08:00:02,226 ERROR x "
            "[V2-FLIP-OWNER-UNKNOWN] DBGI opportunity_id=1 entry_allowed=0 "
            "reason=watchlist_removed_before_position_episode_ended",
            "2026-09-11 08:00:07,235 INFO x "
            "[V2-FLIP-OWNER-RECOVERY] TNON evaluated=1 recovered=1 pending=0 "
            "entry_allowed=0 reason=flat_owner_kept_consumed",
        ],
        since=SINCE,
        until=NOW,
    )

    result = watch.evaluate_owner_roll(observed, now=NOW, market_day=True)

    assert result.verdict == watch.RECURRENCE
    assert (result.evaluated, result.guard_working, result.recurrence) == (2, 0, 2)
    assert "recurred=DBGI,TNON" in result.detail


def test_owner_roll_is_unexercised_when_no_stale_owner_needed_rolling() -> None:
    anchor = watch.session_anchor(NOW)
    anchor_ms = int(anchor.timestamp() * 1000)
    observed = lines(
        (5, f"[V2-SESSION-ROLL] boundary_crossed=True rolled=0 symbols=- (anchor={anchor_ms}"),
    )

    result = watch.evaluate_owner_roll(observed, now=NOW, market_day=True)

    assert result.verdict == watch.UNEXERCISED
    assert result.evaluated == 0


def test_fresh_sell_with_no_entry_then_slot_consumed_is_recurrence() -> None:
    result = watch.evaluate_fresh_sell_slot_clear(
        lines(
            (5, atr_sell("TNON", 5)),
            (
                6,
                "[V2-RESTING-SLOT-CONSUMED] TNON attempted=1 suppressed=1 "
                "reason=first_slot_already_consumed",
            ),
        )
    )

    assert result.verdict == watch.RECURRENCE
    assert (result.evaluated, result.guard_working, result.recurrence) == (1, 0, 1)
    assert "TNON@" in result.detail


def test_fresh_sell_without_suppression_is_guard_working() -> None:
    result = watch.evaluate_fresh_sell_slot_clear(lines((5, atr_sell("TNON", 5))))

    assert result.verdict == watch.GUARD_WORKING
    assert (result.evaluated, result.guard_working, result.recurrence) == (1, 1, 0)


def test_slot_consumed_after_a_real_entry_is_benign() -> None:
    result = watch.evaluate_fresh_sell_slot_clear(
        lines(
            (5, atr_sell("TNON", 5)),
            (6, "[V2-FLIP-OWNER-FILL] TNON opportunity_id=123 account=live:orb"),
            (
                7,
                "[V2-RESTING-SLOT-CONSUMED] TNON attempted=1 suppressed=1 "
                "reason=first_slot_already_consumed",
            ),
        )
    )

    assert result.verdict == watch.GUARD_WORKING
    assert result.recurrence == 0
    assert "consumed_after_fill=1" in result.detail


def test_replayed_sell_probe_does_not_count_as_a_fresh_segment() -> None:
    stale_bar_ms = int(datetime(2026, 9, 10, 19, 59, tzinfo=UTC).timestamp() * 1000)
    result = watch.evaluate_fresh_sell_slot_clear(
        lines(
            (
                5,
                f"[V2-ATR-PROBE] sym=TNON ts_ms={stale_bar_ms} "
                "state=short flip=SELL fired_seg=false",
            )
        )
    )

    assert result.verdict == watch.UNEXERCISED
    assert result.evaluated == 0


def test_fresh_sell_window_matches_the_live_entry_bar_horizon() -> None:
    from project_mai_tai.strategy_core.schwab_1m_v2 import MAX_BAR_AGE_SECONDS_FOR_EMIT

    assert watch.FRESH_SELL_MAX_BAR_AGE_SECONDS == MAX_BAR_AGE_SECONDS_FOR_EMIT


@pytest.mark.parametrize("count", [1, 2])
def test_liquidity_cancel_before_three_thin_bars_is_recurrence(count: int) -> None:
    result = watch.evaluate_liquidity_pull(
        lines(
            (
                5,
                "[V2-RESTING-CANCEL] FTFT slot=first reason=liquidity_floor "
                f"resting_below_floor_bars={count} level=2.8843",
            )
        )
    )

    assert result.verdict == watch.RECURRENCE
    assert (result.evaluated, result.guard_working, result.recurrence) == (1, 0, 1)


@pytest.mark.parametrize("marker", ["V2-RESTING-CANCEL", "V2-RESTING-EH-DISARM"])
def test_liquidity_cancel_at_three_bars_is_guard_working(marker: str) -> None:
    result = watch.evaluate_liquidity_pull(
        lines(
            (
                5,
                f"[{marker}] FTFT slot=first reason=liquidity_floor "
                "resting_below_floor_bars=3 level=2.8843",
            )
        )
    )

    assert result.verdict == watch.GUARD_WORKING
    assert (result.evaluated, result.guard_working, result.recurrence) == (1, 1, 0)


def test_good_bar_resetting_a_short_liquidity_streak_is_guard_working() -> None:
    result = watch.evaluate_liquidity_pull(
        lines(
            (
                5,
                "[V2-CW-STATE-PROBE] sym=FTFT resting_below_floor_bars=2 resting_active=True",
            ),
            (
                6,
                "[V2-CW-STATE-PROBE] sym=FTFT resting_below_floor_bars=0 resting_active=True",
            ),
        )
    )

    assert result.verdict == watch.GUARD_WORKING
    assert "good_bar_resets=FTFT:2->0" in result.detail


def test_liquidity_cancel_without_its_streak_is_cannot_tell() -> None:
    result = watch.evaluate_liquidity_pull(
        lines(
            (
                5,
                "[V2-RESTING-CANCEL] FTFT slot=first reason=liquidity_floor level=2.8843",
            )
        )
    )

    assert result.verdict == watch.COULD_NOT_TELL
    assert "unmeasured_cancels=1" in result.detail


def test_seed_guard_marker_is_benign_and_fail_open_is_recurrence() -> None:
    guarded = lines(
        (5, "[V2-DB-SEED-GAP-CENSUS] truncations=1 of 4 seed evaluations since boot"),
        (6, "[V2-DB-SEED-GAP] ABC dropped ALL 250 seed bars"),
    )
    failed = lines(
        (5, "[V2-DB-SEED-GAP-CENSUS] truncations=0 of 4 seed evaluations since boot"),
        (6, "[V2-DB-SEED-GAP] session-calendar lookup failed; treating the gap as CONTIGUOUS"),
    )
    assert watch.evaluate_seed(guarded).verdict == watch.GUARD_WORKING
    assert watch.evaluate_seed(failed).verdict == watch.RECURRENCE


def test_webull_429_needs_a_denominator_and_a_real_burst() -> None:
    raw = [
        f"2026-09-11 12:00:{second:02d},000 WARNING x "
        "Webull position sync rate-limited (429) for live:orb -> backing off"
        for second in range(10)
    ]
    raw.append(
        "2026-09-11 11:59:50,000 INFO x [BROKER-SYNC-CENSUS] "
        "live:orb: ok=100 failed=10 consecutive_now=0"
    )
    parsed = watch.parse_log_lines(raw, since=SINCE, until=NOW + timedelta(minutes=1))
    result = watch.evaluate_webull_429(parsed)
    assert result.verdict == watch.RECURRENCE
    assert result.evaluated == 110
    assert "peak_60s=10" in result.detail


def test_one_webull_429_is_the_backoff_guard_working() -> None:
    result = watch.evaluate_webull_429(
        lines(
            (5, "[BROKER-SYNC-CENSUS] live:orb: ok=20 failed=1 consecutive_now=0"),
            (6, "Webull position sync rate-limited (429) for live:orb -> backing off"),
        )
    )
    assert result.verdict == watch.GUARD_WORKING
    assert result.recurrence == 0


def test_confirmed_exit_release_markers_stay_bound_to_their_account() -> None:
    observed = lines(
        (5, "[OMS-EXIT-RELEASE] AAA live:schwab_1m_v2 base=x release_confirmed=1"),
        (6, "[OMS-EXIT-PAIR-RESOLVED] BBB live:orb base=y"),
        (7, "[OMS-EXIT-RELEASE] CCC paper:orb base=z release_confirmed=1"),
    )
    assert watch.confirmed_exit_releases(observed, "live:schwab_1m_v2") == 1
    assert watch.confirmed_exit_releases(observed, "live:orb") == 1


def test_database_classifiers_keep_the_accounts_and_denominators_visible() -> None:
    rows = {
        row.key: row
        for row in watch.evaluate_database(
            database_metrics(
                managed_exit_orders_schwab=5,
                managed_exit_orders_webull=7,
                confirmed_exit_releases_schwab=1,
                confirmed_exit_releases_webull=2,
                reserved_share_reject_orders_webull=1,
                owned_open_rows_schwab=1,
                owned_open_rows_webull=1,
                owned_fresh_rows_schwab=1,
                owned_fresh_rows_webull=1,
                virtual_zero_held_rows_webull=1,
            )
        )
    }
    assert rows["RESERVE1"].verdict == watch.RECURRENCE
    assert "schwab_managed_exits=5 releases=1" in rows["RESERVE1"].detail
    assert "webull_managed_exits=7 releases=2" in rows["RESERVE1"].detail
    assert rows["VPZERO1"].verdict == watch.RECURRENCE
    assert "schwab_owned=1 fresh=1 virtual_zero=0" in rows["VPZERO1"].detail
    assert "webull_owned=1 fresh=1 virtual_zero=1" in rows["VPZERO1"].detail


def test_stale_broker_truth_is_unknown_not_a_virtual_zero() -> None:
    rows = {
        row.key: row for row in watch.evaluate_database(database_metrics(owned_open_rows_schwab=1))
    }
    assert rows["VPZERO1"].verdict == watch.COULD_NOT_TELL


def test_positive_recurrence_evidence_outranks_a_missing_supporting_log() -> None:
    rows = {
        row.key: row
        for row in watch.evaluate_database(
            database_metrics(
                managed_exit_orders_webull=1,
                reserved_share_reject_orders_webull=1,
            ),
            oms_logs_readable=False,
        )
    }
    assert rows["RESERVE1"].verdict == watch.RECURRENCE


def test_phantom_guard_working_and_recurrence_are_opposite() -> None:
    clean_result = SimpleNamespace(
        row=SimpleNamespace(account="live:schwab_1m_v2"), verdict="BACKED"
    )
    bad_result = SimpleNamespace(
        row=SimpleNamespace(account="live:orb"), verdict="CONFIRMED_PHANTOM"
    )
    clean = SimpleNamespace(
        results=(clean_result,), backed=1, phantoms=0, unknown=0, population_error=""
    )
    bad = SimpleNamespace(
        results=(bad_result,), backed=0, phantoms=1, unknown=0, population_error=""
    )
    assert watch.evaluate_phantom(clean).verdict == watch.GUARD_WORKING
    bad_reading = watch.evaluate_phantom(bad)
    assert bad_reading.verdict == watch.RECURRENCE
    assert "live:orb evaluated=1 backed=0 phantoms=1 unknown=0" in bad_reading.detail


def test_phantom_population_is_live_only() -> None:
    rows = [
        SimpleNamespace(account="live:schwab_1m_v2"),
        SimpleNamespace(account="live:orb"),
        SimpleNamespace(account="paper:orb"),
        SimpleNamespace(account="paper:macd_30s"),
    ]
    assert [row.account for row in watch.live_phantom_population(rows)] == [
        "live:schwab_1m_v2",
        "live:orb",
    ]


def test_static_rows_are_never_reported_as_clean() -> None:
    rows = watch.static_readings()
    assert {row.verdict for row in rows} == {watch.UNARMED, watch.DELEGATED}
    assert all(row.evaluated == 0 for row in rows)


def test_failed_evidence_keeps_every_catalog_row_visible() -> None:
    rows = watch.failed_evidence_readings("database unavailable")
    assert len(rows) == len(watch.CATALOG) == 19
    assert {row.key for row in rows} == {row.key for row in watch.CATALOG}
    assert all(
        row.verdict == watch.COULD_NOT_TELL
        for row in rows
        if watch.SPEC_BY_KEY[row.key].mode == "ARMED"
    )


def test_reading_contract_requires_every_catalog_row_exactly_once() -> None:
    rows = watch.failed_evidence_readings("database unavailable")

    with pytest.raises(RuntimeError, match="missing=BOOT1"):
        watch.validate_readings([row for row in rows if row.key != "BOOT1"])
    with pytest.raises(RuntimeError, match="duplicates=BOOT1"):
        watch.validate_readings([*rows, rows[0]])


@pytest.mark.parametrize("recurred", [None, "yes", 0, 1, [], {}])
def test_reading_contract_requires_recurred_to_be_an_explicit_boolean(recurred) -> None:
    rows = watch.failed_evidence_readings("database unavailable")
    original = next(row for row in rows if row.key == "BOOT1")
    malformed = watch.Reading(
        original.key,
        original.verdict,
        original.evaluated,
        original.guard_working,
        original.recurrence,
        recurred,
        original.detail,
    )

    with pytest.raises(RuntimeError, match="recurred must be present and boolean"):
        watch.validate_readings([malformed if row.key == malformed.key else row for row in rows])


def test_main_turns_a_malformed_collector_answer_into_cannot_tell(
    monkeypatch, tmp_path, capsys
) -> None:
    rows = watch.failed_evidence_readings("unused")
    original = next(row for row in rows if row.key == "BOOT1")
    malformed = watch.Reading(
        original.key,
        original.verdict,
        original.evaluated,
        original.guard_working,
        original.recurrence,
        None,
        original.detail,
    )
    monkeypatch.setattr(
        watch,
        "collect_readings",
        lambda _now: [malformed if row.key == malformed.key else row for row in rows],
    )

    rc = watch.main(
        [
            "--state",
            str(tmp_path / "state.json"),
            "--status",
            str(tmp_path / "STATUS.txt"),
            "--no-page",
        ]
    )

    assert rc == 2
    output = capsys.readouterr().out
    assert "[COULD_NOT_TELL] BOOT1" in output
    assert "recurred must be present and boolean" in output


def test_one_failed_source_does_not_poison_independent_rows(monkeypatch) -> None:
    def logs(service: str, since: datetime):
        del since
        if service == "schwab-1m-v2":
            raise RuntimeError("v2 permission denied")
        return [
            "2026-09-11 11:00:00,000 INFO x [BROKER-SYNC-CENSUS] "
            "live:orb: ok=10 failed=0 consecutive_now=0"
        ]

    monkeypatch.setattr(watch, "_read_recent_logs", logs)
    monkeypatch.setattr(
        watch,
        "_query_database",
        lambda _since: database_metrics(),
    )
    monkeypatch.setattr(
        watch,
        "_load_phantom_report",
        lambda _now: SimpleNamespace(
            results=(), backed=0, phantoms=0, unknown=0, population_error=""
        ),
    )

    rows = {row.key: row for row in watch.collect_readings(NOW)}

    assert len(rows) == 19
    assert rows["BOOT1"].verdict == watch.COULD_NOT_TELL
    assert rows["ROLL1"].verdict == watch.COULD_NOT_TELL
    assert rows["OWNERROLL1"].verdict == watch.COULD_NOT_TELL
    assert rows["SLOTCLEAR1"].verdict == watch.COULD_NOT_TELL
    assert rows["LIQPULL1"].verdict == watch.COULD_NOT_TELL
    assert rows["SEED1"].verdict == watch.COULD_NOT_TELL
    assert rows["W4291"].verdict == watch.OBSERVED_CLEAN
    assert rows["RESERVE1"].verdict == watch.UNEXERCISED
    assert rows["PHANTOM1"].verdict == watch.UNEXERCISED


def test_database_census_is_read_only_broker_origin_and_live_account_scoped(monkeypatch) -> None:
    payload = {
        key: value
        for key, value in database_metrics().items()
        if not key.startswith("confirmed_exit_releases_")
    }

    def run(command, **kwargs):
        sql = kwargs["input"]
        assert command[:4] == ["sudo", "-n", "-u", "postgres"]
        assert "BEGIN READ ONLY;" in sql
        assert "e.event_source = 'broker'" in sql
        assert "ba.name IN ('live:orb', 'live:schwab_1m_v2')" in sql
        assert "paper:orb" not in sql
        return SimpleNamespace(returncode=0, stderr="", stdout=f"{watch.json.dumps(payload)}\n")

    monkeypatch.setattr(watch.subprocess, "run", run)
    assert watch._query_database(SINCE) == payload


@pytest.mark.parametrize("malformation", ["missing", "string", "bool", "negative"])
def test_database_census_refuses_a_malformed_metric_payload(monkeypatch, malformation: str) -> None:
    payload = {
        key: value
        for key, value in database_metrics().items()
        if not key.startswith("confirmed_exit_releases_")
    }
    key = "managed_exit_orders_schwab"
    if malformation == "missing":
        payload.pop(key)
    elif malformation == "string":
        payload[key] = "0"
    elif malformation == "bool":
        payload[key] = True
    else:
        payload[key] = -1

    monkeypatch.setattr(
        watch.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stderr="", stdout=f"{watch.json.dumps(payload)}\n"
        ),
    )

    with pytest.raises(RuntimeError, match="metric set or value types are invalid"):
        watch._query_database(SINCE)


def test_pre_anchor_boot_is_kept_without_polluting_current_session_counts(monkeypatch) -> None:
    anchor = watch.session_anchor(NOW)
    anchor_ms = int(anchor.timestamp() * 1000)
    calls: list[datetime] = []

    def logs(service: str, since: datetime):
        calls.append(since)
        if service == "schwab-1m-v2":
            return [
                "2026-09-11 07:50:00,000 INFO x "
                "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=4 confirmed=4",
                "2026-09-11 07:51:00,000 INFO x [V2-BOOT-HOLD] released restoration_complete=1",
                f"2026-09-11 08:01:00,000 INFO x "
                f"[V2-SESSION-ROLL] boundary_crossed=True rolled=0 symbols=- "
                f"(anchor={anchor_ms}",
                "2026-09-11 08:02:00,000 INFO x "
                "[V2-DB-SEED-GAP-CENSUS] truncations=0 of 3 seed evaluations since boot",
            ]
        return [
            "2026-09-11 11:00:00,000 INFO x [BROKER-SYNC-CENSUS] "
            "live:orb: ok=10 failed=0 consecutive_now=0"
        ]

    monkeypatch.setattr(watch, "_read_recent_logs", logs)
    monkeypatch.setattr(
        watch,
        "_query_database",
        lambda _since: database_metrics(),
    )
    monkeypatch.setattr(
        watch,
        "_load_phantom_report",
        lambda _now: SimpleNamespace(
            results=(), backed=0, phantoms=0, unknown=0, population_error=""
        ),
    )

    rows = {row.key: row for row in watch.collect_readings(NOW)}

    assert calls[0] == anchor - timedelta(hours=6)
    assert rows["BOOT1"].verdict == watch.GUARD_WORKING
    assert rows["ROLL1"].verdict == watch.GUARD_WORKING
    assert rows["OWNERROLL1"].verdict == watch.UNEXERCISED
    assert rows["SEED1"].evaluated == 3


def test_recurrence_pages_once_failed_delivery_retries_and_recovery_rearms(tmp_path) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "STATUS.txt"
    attempts: list[str] = []
    outcomes = iter((False, True, True))

    def page(title: str, _body: str) -> bool:
        attempts.append(title)
        return next(outcomes)

    bad = [reading("BOOT1", watch.RECURRENCE)]
    clean = [reading("BOOT1", watch.GUARD_WORKING)]
    watch._run_watch(
        bad, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    watch._run_watch(
        bad, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    watch._run_watch(
        bad, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    assert len(attempts) == 2, "delivery success must silence the continuing episode"

    watch._run_watch(
        clean, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    watch._run_watch(
        bad, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    assert len(attempts) == 3, "a cleared episode must re-arm"


def test_state_is_durable_before_page_is_attempted(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "STATUS.txt"

    def assert_state_exists(_title: str, _body: str) -> bool:
        assert state.exists()
        assert '"alert_kind": "recurrence"' in state.read_text(encoding="utf-8")
        return True

    watch._run_watch(
        [reading("BOOT1", watch.RECURRENCE)],
        now=NOW,
        state_path=state,
        status_path=status,
        no_page=False,
        page_fn=assert_state_exists,
    )


def test_corrupt_state_pages_state_loss_without_replaying_recurrence(tmp_path) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "STATUS.txt"
    state.write_text("{truncated", encoding="utf-8")
    pages: list[str] = []
    watch._run_watch(
        [reading("BOOT1", watch.RECURRENCE)],
        now=NOW,
        state_path=state,
        status_path=status,
        no_page=False,
        page_fn=lambda title, _body: pages.append(title) or True,
    )
    assert pages == ["STATE LOST - known defect regression watch"]


def test_refused_state_loss_page_retries_until_delivery(tmp_path) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "STATUS.txt"
    state.write_text("{truncated", encoding="utf-8")
    pages: list[str] = []
    outcomes = iter((False, True))

    def page(title: str, _body: str) -> bool:
        pages.append(title)
        return next(outcomes)

    row = [reading("BOOT1", watch.GUARD_WORKING)]
    watch._run_watch(
        row, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    watch._run_watch(
        row, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    watch._run_watch(
        row, now=NOW, state_path=state, status_path=status, no_page=False, page_fn=page
    )
    assert pages == [
        "STATE LOST - known defect regression watch",
        "STATE LOST - known defect regression watch",
    ]


def test_could_not_tell_pages_and_is_not_rendered_as_clean(tmp_path) -> None:
    state = tmp_path / "state.json"
    status = tmp_path / "STATUS.txt"
    pages: list[str] = []
    row = watch.Reading("BOOT1", watch.COULD_NOT_TELL, 0, 0, 0, False, "log unreadable")
    rc = watch._run_watch(
        [row],
        now=NOW,
        state_path=state,
        status_path=status,
        no_page=False,
        page_fn=lambda title, _body: pages.append(title) or True,
    )
    assert rc == 2
    assert pages == ["CANNOT TELL BOOT1 - regression watch is blind"]
    assert watch.OBSERVED_CLEAN not in status.read_text(encoding="utf-8")


def test_delivery_is_imported_and_wrapper_has_no_second_notifier() -> None:
    wrapper = SCRIPT.with_name("known_defect_regression_watch_cron.sh").read_text(encoding="utf-8")
    source = SCRIPT.read_text(encoding="utf-8")
    assert watch.delivery.NTFY_URL == "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Constant) and node.value == "curl" for node in ast.walk(tree)
    ), "the Python watch must import delivery.page, not invoke curl itself"
    wrapper_code = "\n".join(
        line for line in wrapper.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )
    assert not re.search(r"(^|[;&|]\s*)curl\b", wrapper_code)
    assert "ntfy.sh" not in wrapper_code


def test_repository_has_one_authoritative_regression_watch() -> None:
    assert SCRIPT.exists()
    assert not SCRIPT.with_name("regression_watch.py").exists()


def test_cron_install_owns_one_file_and_preserves_unrelated_cron(tmp_path) -> None:
    health = SCRIPT.parent
    source = health / "known_defect_regression_watch.cron"
    installer = health / "install_known_defect_regression_watch.sh"
    target_dir = tmp_path / "cron.d"
    target = target_dir / "project-mai-tai-known-defect-regression-watch"
    unrelated = target_dir / "another-watch"
    target_dir.mkdir()
    unrelated.write_text("existing schedule\n", encoding="utf-8")
    env = {
        **os.environ,
        "KNOWN_DEFECT_INSTALL_TEST_MODE": "1",
        "KNOWN_DEFECT_INSTALL_SOURCE": str(source),
        "KNOWN_DEFECT_INSTALL_TARGET": str(target),
    }

    result = subprocess.run(
        ["bash", str(installer)], capture_output=True, text=True, env=env, check=False
    )

    assert result.returncode == 0, result.stderr
    assert target.read_bytes() == source.read_bytes()
    assert unrelated.read_text(encoding="utf-8") == "existing schedule\n"
    assert "shared_crontab_untouched=1" in result.stdout
    schedule = [
        line
        for line in source.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith(("SHELL=", "PATH=", "#"))
    ]
    assert schedule == [
        "*/5 * * * * root "
        "/home/trader/project-mai-tai/ops/health/known_defect_regression_watch_cron.sh "
        ">> /var/log/project-mai-tai/known-defect-regression-watch.log 2>&1"
    ]


def test_cron_installer_refuses_an_unreviewed_schedule(tmp_path) -> None:
    health = SCRIPT.parent
    bad_source = tmp_path / "bad.cron"
    target = tmp_path / "cron.d" / "watch"
    bad_source.write_text("* * * * * root unreviewed-command\n", encoding="utf-8")
    env = {
        **os.environ,
        "KNOWN_DEFECT_INSTALL_TEST_MODE": "1",
        "KNOWN_DEFECT_INSTALL_SOURCE": str(bad_source),
        "KNOWN_DEFECT_INSTALL_TARGET": str(target),
    }

    result = subprocess.run(
        ["bash", str(health / "install_known_defect_regression_watch.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 2
    assert not target.exists()
    assert "exactly one schedule" in result.stderr


def test_wrapper_runs_only_in_the_et_window_and_selftest_bypasses_it(tmp_path) -> None:
    wrapper = SCRIPT.with_name("known_defect_regression_watch_cron.sh")
    fake = tmp_path / "check.py"
    calls = tmp_path / "calls"
    fake.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$KNOWN_DEFECT_TEST_CALLS"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
    base_env = {
        **os.environ,
        "KNOWN_DEFECT_WATCH_PYTHON": "/bin/sh",
        "KNOWN_DEFECT_WATCH_SCRIPT": str(fake),
        "KNOWN_DEFECT_WATCH_OUT": str(tmp_path / "out"),
        "KNOWN_DEFECT_TEST_CALLS": str(calls),
    }

    closed = subprocess.run(
        ["bash", str(wrapper)],
        env={**base_env, "KNOWN_DEFECT_WATCH_NOW_ET": "7 1200"},
        check=False,
    )
    assert closed.returncode == 0
    assert not calls.exists()

    open_run = subprocess.run(
        ["bash", str(wrapper)],
        env={**base_env, "KNOWN_DEFECT_WATCH_NOW_ET": "1 0350"},
        check=False,
    )
    assert open_run.returncode == 0
    assert "--state" in calls.read_text(encoding="utf-8")

    calls.unlink()
    selftest = subprocess.run(
        ["bash", str(wrapper), "--selftest"],
        env={**base_env, "KNOWN_DEFECT_WATCH_NOW_ET": "7 1200"},
        check=False,
    )
    assert selftest.returncode == 0
    assert calls.read_text(encoding="utf-8").strip() == "--selftest"


def test_selftest_wording_never_claims_a_live_defect() -> None:
    sent: list[tuple[str, str]] = []
    rc = watch._selftest(lambda title, body: sent.append((title, body)) or True)
    assert rc == 0
    assert "SELFTEST" in sent[0][0]
    assert "No production defect was observed" in sent[0][1]

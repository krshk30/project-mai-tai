from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "marker_census.py"
SPEC = importlib.util.spec_from_file_location("marker_census", SCRIPT)
assert SPEC and SPEC.loader
census = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = census
SPEC.loader.exec_module(census)

SINCE = datetime(2026, 8, 28, 11, 0, tzinfo=UTC)
UNTIL = datetime(2026, 8, 28, 20, 0, tzinfo=UTC)


def line(at: str, payload: str) -> str:
    return f"{at},123 INFO project_mai_tai.test {payload}"


def metrics(
    v2=(),
    oms=(),
    db=None,
    identity=(
        "UNEXERCISED",
        {
            "queued": 0,
            "submitted": 0,
            "intent_identity_complete": 0,
            "roots": 0,
            "max_chain_depth": 0,
            "chain_errors": 0,
        },
    ),
):
    rows = census.build_lines(
        v2=census.window_lines(v2, since=SINCE, until=UNTIL),
        oms=census.window_lines(oms, since=SINCE, until=UNTIL),
        db=db,
        identity=identity,
    )
    by_name = {}
    for row in rows:
        by_name.setdefault(row.name, []).append(row)
    return by_name


def fields(row) -> dict[str, str]:
    return dict(row.fields)


def complete_db(**overrides: int) -> dict[str, int]:
    result = {
        "order_event_rows": 0,
        "terminal_target_episodes": 0,
        "strategy_cancel_followups": 0,
        "db3_probe_symbols": 0,
        "db3_fanout_only_excluded": 0,
    }
    result.update(overrides)
    return result


def status_for_denominator(case_name: str, denominator: int):
    """Vary only one metric's opportunity denominator; keep its failure count at zero."""
    metric_name = case_name
    if case_name == "w1_reactive_suppression":
        v2 = (
            [line("2026-08-28 12:00:00", "[V2-FANOUT-REACTIVE-LATCHED] AAA")] if denominator else []
        )
        return metrics(v2=v2)[metric_name][0]
    if case_name == "w2_fanout_identity":
        status = "UNEXERCISED" if denominator == 0 else "OBSERVED"
        identity = (
            status,
            {
                "queued": denominator,
                "submitted": denominator,
                "intent_identity_complete": denominator,
                "roots": denominator,
                "max_chain_depth": denominator,
                "chain_errors": 0,
            },
        )
        return metrics(identity=identity)[metric_name][0]
    if case_name == "w4_no_account_population":
        metric_name = "w4_broker_sync"
        oms = (
            [
                line(
                    "2026-08-28 12:00:00",
                    "[BROKER-SYNC-CENSUS] live:orb: ok=1 failed=0 consecutive_now=0",
                )
            ]
            if denominator
            else []
        )
        return metrics(oms=oms)[metric_name][0]
    if case_name == "w4_per_account_reads":
        metric_name = "w4_broker_sync"
        oms = [
            line(
                "2026-08-28 12:00:00",
                f"[BROKER-SYNC-CENSUS] live:orb: ok={denominator} failed=0 consecutive_now=0",
            )
        ]
        return metrics(oms=oms)[metric_name][0]
    if case_name == "w5_order_event_savepoint":
        return metrics(db=complete_db(order_event_rows=denominator))[metric_name][0]
    if case_name == "pr824_positive_zero_hold":
        v2 = [line("2026-08-28 12:00:00", "[V2-FANOUT-CLAIM-ZERO-HOLD] AAA")] if denominator else []
        return metrics(v2=v2)[metric_name][0]
    if case_name == "pr825_virtual_clear_deferred":
        oms = (
            [
                line(
                    "2026-08-28 12:00:00",
                    "[VIRTUAL-CLEAR-DEFERRED] deferred=1 of unbacked_positive=1 clear_allowed=0",
                )
            ]
            if denominator
            else []
        )
        return metrics(oms=oms)[metric_name][0]
    if case_name == "pr829_intent_cancel_bound":
        db = complete_db(strategy_cancel_followups=denominator)
        return metrics(db=db)[metric_name][0]
    if case_name == "boot_gate_1_population":
        v2 = (
            [
                line(
                    "2026-08-28 12:00:00",
                    "[V2-BOOT-RESTORE] restoration_complete=0 evaluated=1 confirmed=0 "
                    "reason=state_seed_incomplete",
                )
            ]
            if denominator
            else []
        )
        return metrics(v2=v2)[metric_name][0]
    if case_name == "boot_gate_2_state_seed":
        v2 = (
            [
                line(
                    "2026-08-28 12:00:00",
                    "[V2-BOOT-RESTORE] restoration_complete=0 evaluated=1 confirmed=1 "
                    "rest_warmed=0 reason=rest_warmup_incomplete",
                )
            ]
            if denominator
            else []
        )
        return metrics(v2=v2)[metric_name][0]
    if case_name == "boot_gate_3_rest_warmup":
        v2 = (
            [
                line(
                    "2026-08-28 12:00:00",
                    "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=1 confirmed=1 rest_warmed=1",
                )
            ]
            if denominator
            else []
        )
        return metrics(v2=v2)[metric_name][0]
    if case_name == "boot_hold_release":
        v2 = [
            line(
                "2026-08-28 12:00:01",
                "[V2-BOOT-HOLD] released restoration_complete=1 reconstructed_uncapped=0",
            )
        ]
        if denominator:
            v2.insert(
                0,
                line(
                    "2026-08-28 12:00:00",
                    "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=1 confirmed=1 rest_warmed=1",
                ),
            )
        return metrics(v2=v2)[metric_name][0]
    raise AssertionError(f"unhandled census denominator: {case_name}")


@pytest.mark.parametrize(
    "case_name",
    (
        "w1_reactive_suppression",
        "w2_fanout_identity",
        "w4_no_account_population",
        "w4_per_account_reads",
        "w5_order_event_savepoint",
        "pr824_positive_zero_hold",
        "pr825_virtual_clear_deferred",
        "pr829_intent_cancel_bound",
        "boot_gate_1_population",
        "boot_gate_2_state_seed",
        "boot_gate_3_rest_warmup",
        "boot_hold_release",
    ),
)
def test_every_reported_zero_denominator_is_unexercised(case_name: str) -> None:
    zero = status_for_denominator(case_name, 0)
    exercised = status_for_denominator(case_name, 1)

    assert zero.status == "UNEXERCISED"
    assert exercised.status == "OBSERVED"


def test_window_is_half_open_and_marker_matching_is_exact() -> None:
    raw = [
        line("2026-08-28 11:00:00", "[V2-FANOUT-CLAIM-ZERO-HOLD] started"),
        line("2026-08-28 19:59:59", "[V2-FANOUT-CLAIM-ZERO-HOLD-CANCELLED] done"),
        line("2026-08-28 20:00:00", "[V2-FANOUT-CLAIM-ZERO-HOLD] outside"),
    ]
    selected = census.window_lines(raw, since=SINCE, until=UNTIL)
    assert len(census.exact_marker(selected, "V2-FANOUT-CLAIM-ZERO-HOLD")) == 1
    assert len(census.exact_marker(selected, "V2-FANOUT-CLAIM-ZERO-HOLD-CANCELLED")) == 1


def test_w1_uses_both_polarities_as_its_opportunity_denominator() -> None:
    rows = metrics(
        v2=[
            line("2026-08-28 12:00:00", "[V2-FANOUT-REACTIVE-LATCHED] AAA"),
            line("2026-08-28 12:01:00", "[V2-FANOUT-REACTIVE-SUPPRESSED] BBB"),
            line("2026-08-28 12:02:00", "prose naming V2-FANOUT-REACTIVE-SUPPRESSED"),
        ]
    )
    row = rows["w1_reactive_suppression"][0]
    assert row.status == "OBSERVED"
    assert fields(row) == {"suppressed": "1", "opportunities": "2"}


def test_w4_keeps_broker_denominators_per_account() -> None:
    rows = metrics(
        oms=[
            line(
                "2026-08-28 12:00:00",
                "[BROKER-SYNC-CENSUS] live:orb: ok=3 failed=1 consecutive_now=1 | "
                "live:schwab_1m_v2: ok=7 failed=0 consecutive_now=0",
            ),
            line(
                "2026-08-28 12:05:00",
                "[BROKER-SYNC-CENSUS] live:orb: ok=2 failed=0 consecutive_now=0 | "
                "live:schwab_1m_v2: ok=8 failed=0 consecutive_now=0",
            ),
        ]
    )
    observed = {fields(row)["account"]: row for row in rows["w4_broker_sync"]}
    assert fields(observed["live:orb"]) == {
        "account": "live:orb",
        "reads": "6",
        "failed": "1",
    }
    assert observed["live:orb"].status == "FAIL"
    assert fields(observed["live:schwab_1m_v2"])["reads"] == "15"
    assert observed["live:schwab_1m_v2"].status == "OBSERVED"


def test_w5_zero_is_readable_only_with_event_row_denominator() -> None:
    with_db = metrics(db=complete_db(order_event_rows=9))["w5_order_event_savepoint"][0]
    without_db = metrics(db=None)["w5_order_event_savepoint"][0]
    assert with_db.status == "OBSERVED"
    assert fields(with_db) == {"dropped": "0", "attempted": "9"}
    assert without_db.status == "COULD_NOT_TELL"
    assert fields(without_db)["attempted"] == "unknown"


def test_824_does_not_force_cross_window_outcomes_to_reconcile() -> None:
    rows = metrics(v2=[line("2026-08-28 12:00:00", "[V2-FANOUT-CLAIM-ZERO-HOLD] AAA")])
    row = rows["pr824_positive_zero_hold"][0]
    assert row.status == "OBSERVED"
    assert fields(row) == {"started": "1", "cancelled": "0", "expired": "0"}
    assert "cross_the_window" in row.zero_means


def test_825_reports_deferred_over_all_unbacked_candidates() -> None:
    rows = metrics(
        oms=[
            line(
                "2026-08-28 12:00:00",
                "[VIRTUAL-CLEAR-DEFERRED] deferred=2 of unbacked_positive=3 clear_allowed=0",
            ),
            line(
                "2026-08-28 12:00:00",
                "[VIRTUAL-CLEAR] zeroed 1 virtual position(s) with no broker backing "
                "clear_allowed=1 evaluated_unbacked=3",
            ),
        ]
    )
    row = rows["pr825_virtual_clear_deferred"][0]
    assert fields(row) == {"deferred": "2", "unbacked_positive": "3"}


def test_cancel_bounds_state_exact_and_upper_bound_denominators_separately() -> None:
    rows = metrics(
        oms=[
            line("2026-08-28 12:00:00", "[OMS-CANCEL-DEAD-TARGET-BOUND] outcome=refused"),
            line(
                "2026-08-28 12:01:00",
                "[OMS-DIRECT-CANCEL-DEAD-TARGET-BOUND] outcome=refused",
            ),
        ],
        db=complete_db(strategy_cancel_followups=1, terminal_target_episodes=4),
    )
    intent = rows["pr829_intent_cancel_bound"][0]
    direct = rows["pr832_direct_cancel_bound"][0]
    assert fields(intent) == {"refused": "1", "followup_intents": "1"}
    assert fields(direct) == {
        "refused": "1",
        "durable_terminal_targets_before_until": "4",
        "exact_path_denominator": "absent_by_instrumentation",
    }


@pytest.mark.parametrize("terminal_targets", (0, 7))
def test_832_missing_exact_denominator_is_permanent_until_instrumented(
    terminal_targets: int,
) -> None:
    row = metrics(db=complete_db(terminal_target_episodes=terminal_targets))[
        "pr832_direct_cancel_bound"
    ][0]
    assert row.status == "COULD_NOT_TELL"
    assert fields(row)["exact_path_denominator"] == "absent_by_instrumentation"
    assert "permanently_could_not_tell" in row.zero_means


def test_all_three_boot_gates_preserve_never_reached_false_zeros() -> None:
    rows = metrics()
    population = rows["boot_gate_1_population"][0]
    seed = rows["boot_gate_2_state_seed"][0]
    rest = rows["boot_gate_3_rest_warmup"][0]
    release = rows["boot_hold_release"][0]
    assert population.status == seed.status == rest.status == release.status == "UNEXERCISED"
    assert "FALSE_ZERO" in population.zero_means
    assert "FALSE_ZERO" in seed.zero_means
    assert "FALSE_ZERO" in rest.zero_means
    assert "FALSE_ZERO" in release.zero_means


def test_boot_gate_progression_makes_each_polarity_reachable() -> None:
    blocked = metrics(
        v2=[
            line(
                "2026-08-28 12:00:00",
                "[V2-BOOT-RESTORE] restoration_complete=0 evaluated=3 confirmed=3 "
                "rest_warmed=1 reason=rest_warmup_incomplete",
            )
        ]
    )
    assert blocked["boot_gate_1_population"][0].status == "OBSERVED"
    assert blocked["boot_gate_2_state_seed"][0].status == "OBSERVED"
    assert blocked["boot_gate_3_rest_warmup"][0].status == "BLOCKED"

    released = metrics(
        v2=[
            line(
                "2026-08-28 12:00:00",
                "[V2-BOOT-RESTORE] restoration_complete=1 evaluated=3 confirmed=3 rest_warmed=3",
            ),
            line(
                "2026-08-28 12:00:01",
                "[V2-BOOT-HOLD] released — restoration_complete=1 reconstructed_uncapped=0",
            ),
        ]
    )
    assert released["boot_gate_3_rest_warmup"][0].status == "OBSERVED"
    assert released["boot_hold_release"][0].status == "OBSERVED"


def test_post_latch_addition_does_not_fabricate_initial_boot_completion() -> None:
    rows = metrics(
        v2=[
            line(
                "2026-08-28 12:00:00",
                "[V2-BOOT-RESTORE] restoration_complete=1 post_latch_additions=2 "
                "seed_confirmed=2 could_not_tell=0",
            )
        ]
    )
    release = rows["boot_hold_release"][0]
    assert release.status == "UNEXERCISED"
    assert fields(release)["restoration_complete"] == "0"


def test_db3_zero_with_a_real_population_is_count_only_with_its_denominator() -> None:
    row = metrics(db=complete_db(db3_probe_symbols=17, db3_fanout_only_excluded=0))[
        "db3_fanout_only_excluded"
    ][0]
    assert row.status == "COUNT_ONLY"
    assert fields(row) == {"excluded": "0", "probe_symbols": "17"}
    assert "no_correctness_verdict" in row.zero_means


def test_db3_zero_without_a_probe_population_is_still_count_only() -> None:
    row = metrics(db=complete_db())["db3_fanout_only_excluded"][0]
    assert row.status == "COUNT_ONLY"
    assert fields(row) == {"excluded": "0", "probe_symbols": "0"}
    assert "no_correctness_verdict" in row.zero_means


def test_db3_nonzero_is_count_only() -> None:
    row = metrics(db=complete_db(db3_probe_symbols=17, db3_fanout_only_excluded=4))[
        "db3_fanout_only_excluded"
    ][0]
    assert row.status == "COUNT_ONLY"
    assert fields(row) == {"excluded": "4", "probe_symbols": "17"}
    assert "no_correctness_verdict" in row.zero_means


def test_database_count_parser_refuses_missing_or_impossible_rows() -> None:
    valid = "metric,value\n" + "\n".join(
        (
            "order_event_rows,3",
            "terminal_target_episodes,2",
            "strategy_cancel_followups,1",
            "db3_probe_symbols,5",
            "db3_fanout_only_excluded,1",
        )
    )
    assert census.parse_database_counts(valid)["order_event_rows"] == 3
    with pytest.raises(census.EvidenceFailure):
        census.parse_database_counts(valid.replace("\ndb3_fanout_only_excluded,1", ""))
    with pytest.raises(census.EvidenceFailure):
        census.parse_database_counts(
            valid.replace("db3_fanout_only_excluded,1", "db3_fanout_only_excluded,6")
        )


def test_database_query_uses_stdin_and_real_window_variables(monkeypatch) -> None:
    captured = {}
    raw = (
        "metric,value\n"
        "order_event_rows,0\n"
        "terminal_target_episodes,0\n"
        "strategy_cancel_followups,0\n"
        "db3_probe_symbols,0\n"
        "db3_fanout_only_excluded,0\n"
    )

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return SimpleNamespace(returncode=0, stdout=raw, stderr="")

    monkeypatch.setattr(census.subprocess, "run", fake_run)
    census.query_database_counts(SINCE, UNTIL)
    command = captured["command"]
    assert command[-2:] == ["-f", "-"]
    assert "-c" not in command
    assert f"window_since={SINCE.isoformat()}" in command
    assert f"window_until={UNTIL.isoformat()}" in command
    assert captured["input"] is census.DATABASE_SQL
    assert "time '07:00'" in census.DATABASE_SQL
    assert "time '13:00'" in census.DATABASE_SQL


def test_identity_module_loads_with_dataclass_metadata_registered() -> None:
    module = census._load_identity_module()
    assert module.Report.__module__ in census.sys.modules


def test_report_keeps_partial_results_but_fails_closed_on_unreadable_source() -> None:
    def logs(service: str) -> list[str]:
        if service == census.OMS_SERVICE:
            raise census.EvidenceFailure("denied")
        return []

    def db(_since, _until):
        raise census.EvidenceFailure("database down")

    def identity(_since, _until, _lines):
        raise census.EvidenceFailure("identity down")

    report = census.run_report(
        since=SINCE,
        until=UNTIL,
        log_reader=logs,
        db_reader=db,
        identity_reader=identity,
    )
    assert report.exit_code == census.COULD_NOT_TELL
    assert report.verdict == "COULD_NOT_TELL"
    assert any("metric=boot_gate_1_population" in line for line in report.lines)
    assert any("evidence_error=oms_logs=denied" in line for line in report.lines)


def test_structurally_unexercisable_w3_is_not_a_metric() -> None:
    rows = metrics()
    assert not any("w3" in name for name in rows)


def test_malformed_or_empty_window_refuses() -> None:
    assert census.main(["--since", "not-a-date", "--until", UNTIL.isoformat()]) == census.REFUSED
    report = census.run_report(since=UNTIL, until=UNTIL)
    assert report.exit_code == census.REFUSED

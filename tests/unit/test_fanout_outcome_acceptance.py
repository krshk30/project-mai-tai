from __future__ import annotations

from datetime import UTC, datetime
import importlib.util
from pathlib import Path
import subprocess
import sys

from project_mai_tai.fanout_identity import fanout_slot_id


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "health"
    / "fanout_outcome_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location("fanout_outcome_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)

SINCE = datetime(2026, 8, 27, 11, 0, tzinfo=UTC)
UNTIL = datetime(2026, 8, 27, 20, 0, tzinfo=UTC)
SEGMENT = "1787830200000"


def _row(kind: str, *values: object):
    padded = tuple(str(value) for value in values) + ("",) * (9 - len(values))
    return tool.RawRow(kind, padded)


def _controls():
    return [
        _row("CONTROL_PAIR", 53, 16, 37, 7, 9),
        _row("CONTROL_FILL", 292, 18, 368, 34, 12),
        _row("CONTROL_DUP", 22, 22, "4.58"),
        _row("CONTROL_REFUSED", "2026-08-24", 37, 9, 2, 28),
        _row("CONTROL_REFUSED", "2026-08-25", 25, 9, 2, 16),
        _row("CONTROL_REFUSED", "2026-08-26", 49, 49, 11, 0),
    ]


def _fill(
    account: str,
    order_id: str,
    *,
    price: float = 2.0,
    segment: str = SEGMENT,
    slot: str = "resting",
    valid_identity: bool = True,
):
    slot_id = (
        fanout_slot_id(
            strategy_code="schwab_1m_v2",
            symbol="YYGH",
            segment_id=segment,
            slot=slot,
        )
        if valid_identity
        else ""
    )
    return _row(
        "FANOUT_FILL",
        account,
        order_id,
        "YYGH",
        "2026-08-27 15:00:00+00",
        price,
        segment,
        slot,
        slot_id,
        "rth_resting" if account == "live:orb" else "",
    )


def _orders(*, mirror_orders: int, mirror_fills: int, schwab_orders: int, schwab_fills: int):
    rows = []
    for index in range(mirror_orders):
        rows.append(
            _row(
                "MATCHED_ORDER",
                "live:orb",
                f"wb-{index}",
                "YYGH",
                "filled" if index < mirror_fills else "cancelled",
                str(index < mirror_fills).lower(),
                "rth_resting_mirror",
            )
        )
    for index in range(schwab_orders):
        rows.append(
            _row(
                "MATCHED_ORDER",
                "live:schwab_1m_v2",
                f"sw-{index}",
                "YYGH",
                "filled" if index < schwab_fills else "cancelled",
                str(index < schwab_fills).lower(),
                "",
            )
        )
    return rows


def test_known_bad_outcomes_fail_even_when_consumer_markers_are_not_read() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1", price=2.00),
        _fill("live:orb", "wb-fill-2", price=2.10),
        _fill("live:schwab_1m_v2", "sw-fill-1", price=2.00),
        *_orders(
            mirror_orders=tool.BASE_MIRROR_ORDERS,
            mirror_fills=tool.BASE_MIRROR_FILLS,
            schwab_orders=tool.BASE_SCHWAB_ORDERS,
            schwab_fills=tool.BASE_SCHWAB_FILLS,
        ),
        _row("REFUSAL", "event-1", "2026-08-27", "sell-fill-1"),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert report.exit_code == tool.FAIL
    output = "\n".join(report.lines)
    assert "metric=fill_rate verdict=FAIL" in output
    assert "metric=duplicate_legs verdict=FAIL duplicate_legs=1 of 2" in output
    assert "metric=refused_exits verdict=FAIL refused_exits=1 post_exit_episodes=1" in output
    assert "consumer markers are not acceptance" in output


def test_known_good_population_passes_all_four_polarities() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1"),
        _fill("live:schwab_1m_v2", "sw-fill-1"),
        *_orders(mirror_orders=10, mirror_fills=1, schwab_orders=10, schwab_fills=1),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert report.exit_code == tool.PASS
    output = "\n".join(report.lines)
    assert "metric=paired_legs verdict=PASS paired_legs=1 usable=1 of 1" in output
    assert "metric=fill_rate verdict=PASS" in output
    assert "metric=duplicate_legs verdict=PASS duplicate_legs=0 of 1" in output
    assert "metric=refused_exits verdict=PASS refused_exits=0 post_exit_episodes=1" in output


def test_zero_denominators_are_unexercised_not_a_clean_zero() -> None:
    report = tool.evaluate(_controls(), since=SINCE, until=UNTIL)

    assert report.exit_code == tool.UNEXERCISED
    output = "\n".join(report.lines)
    assert "paired_legs=0 usable=0 of 0" in output
    assert "mirror=0/0 schwab=0/0" in output
    assert "duplicate_legs=0 of 0" in output
    assert "refused_exits=0 post_exit_episodes=0" in output


def test_missing_shared_identity_refuses_pair_and_duplicate_grades() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1", valid_identity=False),
        *_orders(mirror_orders=1, mirror_fills=1, schwab_orders=1, schwab_fills=1),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert report.exit_code == tool.COULD_NOT_TELL
    output = "\n".join(report.lines)
    assert "metric=paired_legs verdict=COULD_NOT_TELL" in output
    assert "metric=duplicate_legs verdict=COULD_NOT_TELL" in output


def test_cross_venue_pair_is_not_a_duplicate() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1"),
        _fill("live:schwab_1m_v2", "sw-fill-1"),
        *_orders(mirror_orders=1, mirror_fills=1, schwab_orders=1, schwab_fills=1),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert "duplicate_legs=0 of 1" in "\n".join(report.lines)


def test_higher_mirror_rate_does_not_pass_when_the_broker_gap_widens() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1"),
        _fill("live:schwab_1m_v2", "sw-fill-1"),
        # Mirror is 10%, above 6.2%; Schwab is 20%, so the gap is worse than the 3.1pp baseline.
        *_orders(mirror_orders=10, mirror_fills=1, schwab_orders=10, schwab_fills=2),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert "metric=fill_rate verdict=FAIL" in "\n".join(report.lines)


def test_baseline_control_mismatch_voids_every_target_number() -> None:
    rows = _controls()
    rows[0] = _row("CONTROL_PAIR", 52, 16, 36, 7, 9)

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert report.exit_code == tool.COULD_NOT_TELL
    assert "control=FAILED" in "\n".join(report.lines)


def test_refusal_without_preceding_sell_fill_is_not_classified_as_post_exit() -> None:
    rows = [
        *_controls(),
        _fill("live:orb", "wb-fill-1"),
        _fill("live:schwab_1m_v2", "sw-fill-1"),
        *_orders(mirror_orders=1, mirror_fills=1, schwab_orders=1, schwab_fills=1),
        _row("REFUSAL", "event-1", "2026-08-27", ""),
        _row("EXIT_EPISODE", "sell-order-1", "2026-08-27"),
    ]

    report = tool.evaluate(rows, since=SINCE, until=UNTIL)

    assert "classified_post_exit=0 no_preceding_sell_fill=1" in "\n".join(report.lines)


def test_query_uses_psql_stdin_for_real_window_interpolation(monkeypatch) -> None:
    captured = {}
    header = "kind,c1,c2,c3,c4,c5,c6,c7,c8,c9\n"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=header, stderr="")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    assert tool.query_database(SINCE, UNTIL) == ()
    assert captured["command"][-2:] == ["-f", "-"]
    assert captured["input"] == tool.SQL
    assert ":'window_since'" in tool.SQL and ":'window_until'" in tool.SQL


def test_malformed_window_refuses_before_querying() -> None:
    report = tool.evaluate((), since=UNTIL, until=SINCE)

    assert report.exit_code == tool.COULD_NOT_TELL
    assert "window start is not before end" in "\n".join(report.lines)

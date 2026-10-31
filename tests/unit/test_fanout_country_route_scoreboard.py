"""Controlled route proofs, not receipts for the seven requested real sessions."""
from __future__ import annotations

from datetime import UTC, date, datetime
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from project_mai_tai.fanout_identity import ATR_COUNTRY_ROUTE_KEYS, fanout_slot_id


ROOT = Path(__file__).resolve().parents[2]


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "ops/health" / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


tool = _load("d2_country_report", "fanout_outcome_acceptance.py")
cron = _load("d2_country_cron", "fanout_outcome_acceptance_cron.py")
fleet = _load("d2_country_fleet", "fleet_health_check.py")
SINCE = datetime(2026, 10, 19, 4, tzinfo=UTC)
UNTIL = datetime(2026, 10, 20, 4, tzinfo=UTC)
FILL_AT = "2026-10-19T15:00:00+00:00"
SEGMENT = "1792418400000"


def _row(kind, *values):
    return tool.RawRow(kind, tuple(str(value) for value in values) + ("",) * (9 - len(values)))


def _controls():
    return [
        _row("CONTROL_PAIR", 53, 16, 37, 7, 9),
        _row("CONTROL_FILL", 292, 18, 368, 34, 12),
        _row("CONTROL_DUP", 22, 22, "4.58"),
        _row("CONTROL_REFUSED", "2026-08-24", 37, 9, 2, 28),
        _row("CONTROL_REFUSED", "2026-08-25", 25, 9, 2, 16),
        _row("CONTROL_REFUSED", "2026-08-26", 49, 49, 11, 0),
    ]


def _fill(account="live:orb", order="wb-1", segment=SEGMENT):
    return _row("FANOUT_FILL", account, order, "D2CONTROL", FILL_AT, "2.00", segment,
                "resting", fanout_slot_id(strategy_code="schwab_1m_v2", symbol="D2CONTROL",
                                          segment_id=segment, slot="resting"),
                "rth_resting_mirror" if account == "live:orb" else "")


def _payload(fill, *, country=True):
    account, _, symbol, _, _, segment, slot, slot_id, _ = fill.values
    payload = {"fanout_segment_id": segment, "fanout_slot": slot, "fanout_slot_id": slot_id,
               "fanout_source": fill.values[8]}
    if country:
        schwab = account == "live:schwab_1m_v2"
        payload.update({
            "atr_country_schema": "1", "atr_country_symbol": symbol,
            "atr_country_segment_id": segment, "atr_country_broker": "schwab" if schwab else "webull",
            "atr_country_account": account, "atr_country_notional_usd": "600" if schwab else "300",
            "atr_country_reason": "sec_us" if schwab else "sec_non_us", "atr_country_status": "READY",
            "atr_country_as_of": "2026-10-19T14:55:00+00:00",
            "atr_country_sealed_at": "2026-10-19T14:58:00+00:00",
        })
    return payload


def _route(fill, payload=None, *, first_fill=FILL_AT, submitted="2026-10-19T14:59:00+00:00"):
    return _row("ROUTE_ORDER", *fill.values[:3], json.dumps(_payload(fill) if payload is None else payload),
                "webull" if fill.values[0] == "live:orb" else "schwab", submitted, first_fill)


def _single_rows():
    fill = _fill()
    return [*_controls(), fill, _route(fill), _row("EXIT_EPISODE", "sell-1", "2026-10-19")]


def _report(rows):
    return tool.evaluate(rows, since=SINCE, until=UNTIL)


def _text(report):
    return "\n".join(report.lines)


def test_sealed_webull_single_venue_is_na_without_inventing_pair_or_bakeoff():
    report = _report(_single_rows())
    assert report.exit_code == tool.PASS
    assert "metric=paired_legs verdict=NOT_APPLICABLE paired_legs=0" in _text(report)
    assert "metric=fill_rate verdict=NOT_APPLICABLE" in _text(report)
    assert "metric=duplicate_legs verdict=PASS" in _text(report)
    assert "metric=refused_exits verdict=PASS" in _text(report)


@pytest.mark.parametrize("field", ATR_COUNTRY_ROUTE_KEYS)
def test_each_missing_seal_field_is_unknown_not_na(field):
    rows = _single_rows()
    payload = _payload(rows[6])
    del payload[field]
    rows[7] = _route(rows[6], payload)
    report = _report(rows)
    assert report.exit_code == tool.COULD_NOT_TELL
    assert "metric=paired_legs verdict=COULD_NOT_TELL" in _text(report)


@pytest.mark.parametrize(("field", "value"), [
    ("atr_country_schema", "2"), ("atr_country_symbol", "OTHER"),
    ("atr_country_segment_id", "123"), ("atr_country_account", "live:other"),
    ("atr_country_broker", "schwab"), ("atr_country_notional_usd", "600"),
    ("atr_country_reason", ""), ("atr_country_status", ""),
    ("atr_country_sealed_at", "2026-10-19T15:00:01+00:00"),
    ("atr_country_sealed_at", "2026-10-19T14:58:00"),
    ("atr_country_as_of", "2026-10-19T14:59:00+00:00"),
    ("atr_country_status", ["READY"]),
])
def test_contradictory_or_malformed_seal_cannot_exempt_pairing(field, value):
    rows = _single_rows()
    payload = _payload(rows[6])
    payload[field] = value
    rows[7] = _route(rows[6], payload)
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


def test_country_payload_must_match_filled_order_identity_and_clock():
    rows = _single_rows()
    rows[7] = _route(_fill(segment="123"))
    assert _report(rows).exit_code == tool.COULD_NOT_TELL
    rows[7] = _route(rows[6], first_fill="2026-10-19T15:00:01+00:00")
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


def test_duplicate_route_rows_are_unknown_not_last_row_wins():
    rows = _single_rows()
    rows.append(rows[7])
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


@pytest.mark.parametrize("provider", ["schwab", ""])
def test_actual_provider_mismatch_or_missing_is_unknown(provider):
    rows = _single_rows()
    values = list(rows[7].values)
    values[4] = provider
    rows[7] = tool.RawRow("ROUTE_ORDER", tuple(values))
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


def test_single_venue_is_not_inferred_from_post_oct16_date_or_absent_peer():
    rows = _single_rows()
    del rows[7]
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


def test_conflicting_single_country_seals_on_both_venues_are_unknown():
    rows = _single_rows()
    peer = _fill("live:schwab_1m_v2", "sw-1")
    rows.extend([peer, _route(peer)])
    assert _report(rows).exit_code == tool.COULD_NOT_TELL


def test_proven_single_schwab_does_not_green_unexercised_webull_metrics():
    fill = _fill("live:schwab_1m_v2", "sw-1")
    report = _report([*_controls(), fill, _route(fill)])
    assert report.exit_code == tool.UNEXERCISED
    assert "metric=paired_legs verdict=NOT_APPLICABLE" in _text(report)
    assert "metric=duplicate_legs verdict=UNEXERCISED" in _text(report)
    assert "metric=refused_exits verdict=UNEXERCISED" in _text(report)


def _dual_missing_rows():
    wb = _fill()
    sw = _fill("live:schwab_1m_v2", "sw-unfilled")
    return [*_controls(), wb, _route(wb, _payload(wb, country=False)),
            _route(sw, _payload(sw, country=False), first_fill=""),
            _row("MATCHED_ORDER", "live:orb", "wb-1", "D2CONTROL", "filled", "true", "rth_resting_mirror"),
            _row("MATCHED_ORDER", "live:schwab_1m_v2", "sw-unfilled", "D2CONTROL", "cancelled", "false", ""),
            _row("EXIT_EPISODE", "sell-1", "2026-10-19")]


def test_actual_two_venue_submissions_with_one_missing_fill_stay_fail():
    report = _report(_dual_missing_rows())
    assert report.exit_code == tool.FAIL
    assert "metric=paired_legs verdict=FAIL" in _text(report)
    assert "webull_only=1" in _text(report)


def test_expected_missing_webull_fill_is_not_hidden_by_no_webull_denominator():
    rows = _dual_missing_rows()
    wb, sw = _fill(), _fill("live:schwab_1m_v2", "sw-unfilled")
    rows[6] = sw
    rows[7] = _route(wb, _payload(wb, country=False), first_fill="")
    rows[8] = _route(sw, _payload(sw, country=False))
    rows[9] = _row("MATCHED_ORDER", "live:orb", "wb-1", "D2CONTROL", "cancelled", "false", "rth_resting_mirror")
    rows[10] = _row("MATCHED_ORDER", "live:schwab_1m_v2", "sw-unfilled", "D2CONTROL", "filled", "true", "")
    report = _report(rows)
    assert "metric=paired_legs verdict=FAIL" in _text(report)
    assert "schwab_only=1" in _text(report)
    assert report.exit_code != tool.PASS


def test_one_good_pair_cannot_hide_another_expected_missing_counterpart():
    rows = _dual_missing_rows()
    wb = _fill(order="wb-good", segment="123")
    sw = _fill("live:schwab_1m_v2", "sw-good", segment="123")
    rows.extend([wb, sw, _route(wb, _payload(wb, country=False)),
                 _route(sw, _payload(sw, country=False))])
    report = _report(rows)
    assert "metric=paired_legs verdict=FAIL paired_legs=1" in _text(report)
    assert "webull_only=1" in _text(report)


def test_country_scope_does_not_hide_duplicate_or_refused_exit():
    rows = _single_rows()
    duplicate = _fill(order="wb-2")
    rows.extend([duplicate, _route(duplicate), _row("REFUSAL", "event-1", "2026-10-19", "sell-1")])
    report = _report(rows)
    assert report.exit_code == tool.FAIL
    assert "metric=duplicate_legs verdict=FAIL" in _text(report)
    assert "metric=refused_exits verdict=FAIL" in _text(report)


@pytest.mark.parametrize("adverse", [False, True])
def test_route_grade_flows_through_original_cron_and_fleet_reader(tmp_path, adverse):
    report = _report(_dual_missing_rows() if adverse else _single_rows())
    notifications = []
    cron.run_once(now=datetime(2026, 10, 20, 8, tzinfo=UTC),
                  acceptance=SimpleNamespace(run_report=lambda **kwargs: report),
                  out_dir=tmp_path,
                  notify=lambda title, body: notifications.append((title, body)) or True)
    status = (tmp_path / "STATUS.txt").read_text()
    color, _ = fleet.classify_d6_status(status, expected_session=date(2026, 10, 19))
    assert color == ("RED" if adverse else "GREEN")
    assert bool(notifications) is adverse


def test_pair_unknown_not_silenced_by_another_unexercised_metric():
    lines = ["metric=paired_legs verdict=COULD_NOT_TELL usable=0 of 1",
             "metric=fill_rate verdict=UNEXERCISED mirror=0/0 schwab=0/0",
             "metric=duplicate_legs verdict=PASS duplicate_legs=0 of 1",
             "metric=refused_exits verdict=PASS post_exit_episodes=1"]
    assert cron.actionable_nonpass("COULD_NOT_TELL", lines, True)


def test_query_projects_original_denominators_and_raw_route_evidence():
    for token in ["target_route_orders AS", "SELECT 'ROUTE_ORDER'", "bo.payload::text",
                  "ba.provider::text", "base_pair_control", "base_fill_control", "base_dup_control",
                  "base_refused_control", "target_refused_classified", "target_exit_episodes"]:
        assert token in tool.SQL
    assert "BEGIN READ ONLY;" in tool.SQL
    assert "UPDATE " not in tool.SQL

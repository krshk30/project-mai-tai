"""CONTROLLED SHADOW/source parity only; no historical calibration/phone proof."""

from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import sys
from types import ModuleType
from zoneinfo import ZoneInfo

import pytest

from ops.smartalert1.system import evaluate_system
from ops.smartalert1.system_shadow import evaluate_system_shadow
from project_mai_tai.services import scanner_confirmed_capture as proposed
from tests.unit.test_scanner_confirmed_capture import _FakeSessionFactory, _sample_confirmed
from tests.unit.test_smartalert1_system import frame, AS_OF


ET = ZoneInfo("America/New_York")
OLD_SHA = "204a5725100c04b4d5bf99cd11c8ad34aaf14db805f3110c156a39682f95721b"


def literal_old():
    path = Path(__file__).resolve().parents[1] / "fixtures/smartalert1/scanner_capture_07eb.py.txt"
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == OLD_SHA
    name = "smartalert_literal_07eb_capture"
    if name not in sys.modules:
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


@pytest.mark.parametrize("mode", ["CANDIDATE_REPLAY", "REPORT_ONLY", "QUALIFIED_LIVE"])
def test_shadow_keeps_conclusions_and_forbids_delivery_even_with_qualified_live_policy(mode):
    evidence, policy = frame()
    policy.update(mode=mode, qualified=True, approval_ref="controlled:C1", calibration_ref="controlled:proof")
    before = deepcopy((evidence, policy))
    original = evaluate_system(evidence, policy, as_of=AS_OF)
    shadow = evaluate_system_shadow(evidence, policy, as_of=AS_OF)
    assert len(shadow) == len(original)
    if mode == "QUALIFIED_LIVE":
        assert any(row["paging_eligible"] for row in original)
    if mode == "CANDIDATE_REPLAY":
        assert any(row["shadow"]["candidate_detection"] for row in shadow)
    for old, new in zip(original, shadow):
        assert new["paging_eligible"] is False
        assert new["shadow"]["delivery_action"] == "NONE"
        assert new["shadow"]["would_page_under_supplied_policy"] == old["paging_eligible"]
        assert {k: v for k, v in new.items() if k not in {"shadow", "paging_eligible"}} == {
            k: v for k, v in old.items() if k != "paging_eligible"}
    assert (evidence, policy) == before


def test_shadow_off_remains_inert_and_missing_evidence_stays_unknown():
    evidence, policy = frame()
    policy["enabled"] = False
    assert evaluate_system_shadow(evidence, policy, as_of=AS_OF) == []
    policy["enabled"] = True
    evidence["complete"] = False
    rows = evaluate_system_shadow(evidence, policy, as_of=AS_OF)
    assert rows and all(row["status"] == "UNKNOWN" and not row["paging_eligible"] for row in rows)


@pytest.mark.parametrize("enabled", [None, False])
def test_default_and_explicit_off_writer_match_entire_literal_07eb_params(enabled):
    old, new = _FakeSessionFactory(), _FakeSessionFactory()
    now = datetime(2026, 10, 27, 0, 2, tzinfo=ET)
    args = dict(trade_date=now.date(), now=now, all_confirmed=_sample_confirmed(),
                faded_symbols=["CCCC"], dropped_retention_symbols=["DDDD"])
    literal_old().capture_events(old, **deepcopy(args))
    kwargs = {} if enabled is None else {"session_day_enabled": enabled}
    proposed.capture_events(new, **deepcopy(args), **kwargs)
    assert old.executed_params == new.executed_params and old.commits == new.commits


def inserted(factory):
    return [value for value in factory.executed_params if value and "event_type" in value]


@pytest.mark.parametrize("raw", ["09:36:55 AM ET", "01:46:06 PM ET"])
def test_carried_confirm_at_midnight_and_0359_keeps_previous_session_natural_key(raw):
    keys = []
    for now in (datetime(2026, 10, 27, 0, 0, 6, tzinfo=ET), datetime(2026, 10, 27, 3, 59, 59, tzinfo=ET)):
        factory = _FakeSessionFactory()
        proposed.capture_events(factory, trade_date=now.date(), now=now,
            all_confirmed=[dict(ticker="AAAA", confirmed_at=raw)], faded_symbols=[],
            dropped_retention_symbols=[], session_day_enabled=True)
        row, = inserted(factory)
        keys.append((row["trade_date"], row["symbol"], row["event_type"], row["event_at"]))
        assert row["event_at"].date() == date(2026, 10, 26) and row["event_at"] != now
    assert keys[0] == keys[1]  # Actual existing SQL ON CONFLICT natural-key tuple.


def test_genuine_new_overnight_confirmation_fade_and_drop_are_not_blanket_skipped():
    factory = _FakeSessionFactory()
    now = datetime(2026, 10, 27, 0, 31, tzinfo=ET)
    proposed.capture_events(factory, trade_date=now.date(), now=now,
        all_confirmed=[dict(ticker="AAAA", confirmed_at="00:30")], faded_symbols=["CCCC"],
        dropped_retention_symbols=["DDDD"], session_day_enabled=True)
    rows = inserted(factory)
    assert len(rows) == 3 and all(row["trade_date"] == date(2026, 10, 26) for row in rows)
    assert rows[0]["event_at"] == datetime(2026, 10, 27, 0, 30, tzinfo=ET)
    assert rows[1]["event_at"] == rows[2]["event_at"] == now


@pytest.mark.parametrize("hour,minute,day", [(3, 59, 26), (4, 0, 27), (9, 30, 27), (16, 0, 27), (20, 0, 27), (23, 59, 27)])
def test_session_day_rolls_only_at_04_not_calendar_rth_ah_or_20(hour, minute, day):
    now = datetime(2026, 10, 27, hour, minute, tzinfo=ET)
    result = proposed.session_confirm_clock(f"{hour:02}:{minute:02}", now)
    assert result["status"] == "PROVEN_CLOCK"
    assert result["trade_date"] == date(2026, 10, day)
    assert result["event_at"] == now


@pytest.mark.parametrize("raw", [None, "", "not-a-time", 123, "29:59"])
def test_unknown_timestamp_is_logged_not_retimed_or_certified_absent(raw, caplog):
    now = datetime(2026, 10, 27, 12, tzinfo=ET)
    factory = _FakeSessionFactory()
    proposed.capture_events(factory, trade_date=now.date(), now=now,
        all_confirmed=[dict(ticker="AAAA", confirmed_at=raw, force_watchlist=True)],
        faded_symbols=[], dropped_retention_symbols=[], session_day_enabled=True)
    assert inserted(factory) == [] and "SCANNER-CAPTURE-UNKNOWN" in caplog.text
    shadow = proposed.shadow_session_keys(now=now, all_confirmed=[dict(ticker="AAAA", confirmed_at=raw)])
    assert shadow["unknown_is_not_absence"] and shadow["rows"][0]["status"] == "UNKNOWN"
    assert shadow["rows"][0]["event_at"] is None and shadow["writes"] == 0


@pytest.mark.parametrize("seconds,status", [(60, "PROVEN_CLOCK"), (61, "UNKNOWN")])
def test_future_tolerance_is_existing_60_seconds_never_now_fallback(seconds, status):
    now = datetime(2026, 10, 27, 12, tzinfo=ET)
    result = proposed.session_confirm_clock(f"12:01:{seconds-60:02}", now)
    assert result["status"] == status
    if status == "UNKNOWN":
        assert result["event_at"] is None


@pytest.mark.parametrize("now,raw,reason", [
    (datetime(2026, 11, 1, 2, 30, tzinfo=ET), "01:30", "ambiguous_local_confirm_clock"),
    (datetime(2026, 3, 8, 3, 30, tzinfo=ET), "02:30", "nonexistent_local_confirm_clock"),
    (datetime(2026, 10, 27, 9), "09:00", "observation_clock_unknown"),
])
def test_dst_or_missing_observation_clock_never_fabricates_causal_epoch(now, raw, reason):
    result = proposed.session_confirm_clock(raw, now)
    assert result["status"] == "UNKNOWN" and result["reason"] == reason and result["event_at"] is None


def test_shadow_keys_are_pure_json_ready_with_original_order_and_no_database(monkeypatch):
    stocks = [dict(ticker="AAAA", confirmed_at="09:36:55 AM ET"), dict(ticker="BBBB", confirmed_at="13:46:06")]
    before = deepcopy(stocks)
    now = datetime(2026, 10, 27, 0, 5, tzinfo=ET)
    monkeypatch.setattr(proposed, "capture_events", lambda *a, **k: pytest.fail("shadow must not write"))
    result = proposed.shadow_session_keys(now=now, all_confirmed=stocks)
    assert stocks == before and result["writes"] == 0 and result["shadow_only"]
    assert [row["symbol"] for row in result["rows"]] == ["AAAA", "BBBB"]
    assert all(row["trade_date"] == "2026-10-26" for row in result["rows"])
    assert json.loads(json.dumps(result)) == result and result["observed_at"] == now.isoformat()


def test_corrected_writer_retains_original_natural_key_sql_and_other_operands():
    assert str(proposed._INSERT_SQL) == str(literal_old()._INSERT_SQL)
    old, new = _FakeSessionFactory(), _FakeSessionFactory()
    now = datetime(2026, 10, 27, 14, tzinfo=ET)
    args = dict(trade_date=now.date(), now=now, all_confirmed=_sample_confirmed(),
                faded_symbols=["CCCC"], dropped_retention_symbols=["DDDD"])
    literal_old().capture_events(old, **deepcopy(args))
    proposed.capture_events(new, **deepcopy(args), session_day_enabled=True)
    assert old.executed_params == new.executed_params and old.commits == new.commits


def test_shadow_overflow_refuses_without_clean_truncation():
    now = datetime(2026, 10, 27, 14, tzinfo=ET)
    with pytest.raises(ValueError, match="no_truncation"):
        proposed.shadow_session_keys(now=now, all_confirmed=[dict(ticker="AAAA", confirmed_at="13:00")]*1001)


@pytest.mark.parametrize("symbol", ["lowercase", "AAAA\nERROR", "X"*33])
def test_shadow_symbol_shape_is_explicit_refusal_not_hidden_mutation(symbol):
    with pytest.raises(ValueError, match="symbol_unknown"):
        proposed.shadow_session_keys(now=datetime(2026, 10, 27, 14, tzinfo=ET),
                                    all_confirmed=[dict(ticker=symbol, confirmed_at="13:00")])


def test_unbounded_confirm_text_stays_unknown_without_logging_raw_body():
    result = proposed.session_confirm_clock("x"*10000, datetime(2026, 10, 27, 14, tzinfo=ET))
    assert result["status"] == "UNKNOWN" and result["event_at"] is None

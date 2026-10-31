"""UNRUN source proposal: controlled real methods, never historical permission."""

from collections import deque
from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, datetime
import base64
import gzip
import hashlib
import logging
from pathlib import Path
import sys
from types import ModuleType

import pytest

from project_mai_tai.atr_country_route import AtrCountryRouteBook, CountryRouteRefusal
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as proposed
from tools.nochaselog1_supplied_state_replay import compare_supplied_state


OLD = Path(__file__).resolve().parents[1] / "fixtures/nochaselog1/old32_core.py.gz.b64"
OLD_FIXTURE_SHA = "4902377cdbcc1ca412cead7fa0aba42f12f0ee331bb98560d40bbbb2e39cde0b"
OLD_SHA = "abf79c2b2fd8a0f9e9f946d6b461f44e924cfdc4d38e92ee4a08d1c7400414bc"


def literal_old():
    assert not OLD.is_symlink()
    raw = OLD.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == OLD_FIXTURE_SHA
    source = gzip.decompress(base64.b64decode(b"".join(raw.splitlines()), validate=True))
    assert len(source) == 545135
    assert hashlib.sha256(source).hexdigest() == OLD_SHA
    name = "nochase_literal_32_core"
    if name not in sys.modules:
        # Execute exact committed OLD source, never target feature-OFF or a
        # rewritten decision function. Entire class retains its actual helpers.
        module = ModuleType(name)
        module.__file__ = str(OLD)
        sys.modules[name] = module
        try:
            exec(compile(source, str(OLD), "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def strategy(module, *, hour=14, dual=True, strict=True):
    settings = Settings(
        strategy_schwab_1m_v2_atr_flip_enabled=True,
        strategy_schwab_1m_v2_flip_owned_first_entry_enabled=strict,
        strategy_schwab_1m_v2_retry_one_enabled=True,
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=dual,
        strategy_schwab_1m_v2_webull_resting_mirror_enabled=dual,
        strategy_schwab_1m_v2_country_route_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
    )
    value = module.SchwabV2Strategy(settings)
    now = int(datetime(2026, 10, 28, hour, 54, tzinfo=UTC).timestamp() * 1000)
    value._now_ms = lambda: now
    value.configure_flip_entry_ownership(lambda *_: None, restore_readable=True,
                                         retry_budget_restore_readable=True)

    def no_country_read(*_):
        raise AssertionError("country selection must refuse before route lookup")

    value.configure_country_routing(AtrCountryRouteBook(settings, no_country_read))
    return value, now


def controlled_buy(value, module, now, *, state_only=True):
    state = value.watchlist_state("POM")
    anchor = module.session_start_ts_ms(now)
    state.atr_session_anchor_ms = anchor
    state.atr_hl.extend([0.10] * 4)
    state.atr_prev_bar = module.OHLCVBar(now - 60000, 9.75, 9.8, 9.7, 9.75, 25000)
    state.atr_wilders = 0.10
    state.atr_state = "short"
    state.atr_trail = 10.0
    state.atr_short_flip_bar_ts = now - 60000
    state.retry_one_segment_id = now - 60000
    state.retry_one_budget_readable = True
    bar = module.OHLCVBar(now, 10.25, 10.3, 10.2, 10.25, 25000)
    state.bars.append(bar)
    signal = value._update_atr_state(state, bar, state_only=state_only)
    assert signal["flip"] == "BUY"
    return state


def public_values(value):
    """Compare every field without treating the OLD namespace as economic data.

    asdict alone does not recurse into deque, so OLD OHLCVBar instances there
    would compare unequal solely because their original module was loaded under
    a distinct name. Keep class name, every field, order, deque maxlen and exact
    numeric/ID values; never retime, round, filter keys or normalize volume.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return (type(value).__qualname__, {f.name: public_values(getattr(value, f.name))
                                         for f in fields(value)})
    if isinstance(value, deque):
        return ("deque", value.maxlen, tuple(public_values(item) for item in value))
    if isinstance(value, dict):
        return {key: public_values(item) for key, item in value.items()}
    if isinstance(value, list):
        return [public_values(item) for item in value]
    if isinstance(value, tuple):
        return tuple(public_values(item) for item in value)
    return value


def capture(value):
    return public_values({"states": dict(value._symbol_states),
            "counts": dict(value._flip_owner_counts),
            "pending": list(value._pending_intents),
            "owner_records": {symbol: value._flip_owner_record(state)
                              for symbol, state in value._symbol_states.items()}})


@pytest.mark.parametrize("hour,dual", [(12, False), (12, True), (18, False), (18, True)])
@pytest.mark.parametrize("state_only", [False, True])
def test_literal_two_sources_same_real_gate_and_country_refusal(hour, dual, state_only, caplog):
    old_module = literal_old()
    old, now = strategy(old_module, hour=hour, dual=dual)
    new, _ = strategy(proposed, hour=hour, dual=dual)
    controlled_buy(old, old_module, now, state_only=state_only)
    state = controlled_buy(new, proposed, now, state_only=state_only)
    caplog.set_level(logging.INFO)

    def apply(value, event):
        state = value.watchlist_state("POM")
        if event["operation"] == "country":
            with pytest.raises(CountryRouteRefusal) as exc:
                value._country_entry_selection(state)
            return str(exc.value)
        return value._strict_first_rest_admitted(state, slot="first")

    events = [dict(origin="CONTROLLED_STATE", source_ref="controlled:real-BUY-transition",
                   operation=op) for op in ("gate", "country", "gate")]
    result = compare_supplied_state(events, old=old, proposed=new,
                                   apply_event=apply, capture=capture)
    assert result["supplied_state_equal"]
    assert result["historical_replay_qualified"] is False
    assert [r["proposed_result"] for r in result["rows"]] == [False, "country_owner_admission_unproven", False]
    lines = [r.getMessage() for r in caplog.records if "[V2-POSTFLIP-NOCHASE]" in r.getMessage()]
    assert len(lines) == 1
    assert f"flip_bar={now}" in lines[0]
    assert new._flip_owner_counts["admission_evaluated"] == 3
    assert "_postflip_nochase_log" not in asdict(state)
    assert all(f.name != "_postflip_nochase_log" for f in fields(state))
    assert state._postflip_nochase_log.current == (state.atr_session_anchor_ms, now)


def test_real_sell_and_indicator_reset_clear_only_current_diagnostic_identity(caplog):
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)
    observer = state._postflip_nochase_log
    old_owner = value._flip_owner_record(state)
    caplog.set_level(logging.INFO)
    assert value._strict_first_rest_admitted(state, slot="first") is False
    sell = proposed.OHLCVBar(now + 60000, 1.0, 1.05, 0.95, 1.0, 25000)
    assert value._update_atr_state(state, sell, state_only=True)["flip"] == "SELL"
    assert observer.current == (state.atr_session_anchor_ms, 0)
    assert value._flip_owner_record(state) == old_owner
    buy = proposed.OHLCVBar(now + 120000, 20.0, 20.05, 19.95, 20.0, 25000)
    assert value._update_atr_state(state, buy, state_only=True)["flip"] == "BUY"
    assert value._strict_first_rest_admitted(state, slot="first") is False
    lines = [r.getMessage() for r in caplog.records if "[V2-POSTFLIP-NOCHASE]" in r.getMessage()]
    assert len(lines) == 2
    assert f"flip_bar={now + 120000}" in lines[1]
    value._reset_atr_indicator_state(state, state.atr_session_anchor_ms)
    assert observer.current == (0, 0)
    assert value._flip_owner_record(state) == old_owner


def test_indicator_rebuild_reuses_observer_but_new_symbolstate_does_not():
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)
    assert value._strict_first_rest_admitted(state, slot="first") is False
    observer = state._postflip_nochase_log
    value._reset_atr_indicator_state(state, state.atr_session_anchor_ms)
    controlled_buy(value, proposed, now)
    assert state._postflip_nochase_log is observer
    assert observer.last_logged_flip_bar_ms == now
    other = proposed.SymbolState(symbol="POM")
    assert "_postflip_nochase_log" not in other.__dict__


def test_seeded_anchor_temporary_reset_restores_observed_buy_identity():
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)
    observer = state._postflip_nochase_log
    value._atr_seeded_session_anchors[state.symbol] = state.atr_session_anchor_ms
    following = proposed.OHLCVBar(now + 60000, 11.0, 11.05, 10.95, 11.0, 25000)
    value._update_atr_state(state, following)
    assert observer.current == (state.atr_session_anchor_ms, now)


def test_missing_buy_identity_not_invented_from_retry_or_current_time(caplog):
    value, now = strategy(proposed)
    state = value.watchlist_state("POM")
    state.atr_state = "long"
    state.retry_one_segment_id = now - 60000
    state.retry_one_budget_readable = True
    caplog.set_level(logging.INFO)
    assert value._strict_first_rest_admitted(state, slot="first") is False
    assert not any("[V2-POSTFLIP-NOCHASE]" in r.getMessage() for r in caplog.records)
    assert any("reason=retry_segment_mismatch" in r.getMessage() for r in caplog.records)


def test_unknown_error_marker_and_level_unchanged(caplog):
    value, _ = strategy(proposed)
    state = value.watchlist_state("SPRC")
    value._set_flip_owner_unknown(state, reason="controlled_unanswered_submit", persist=False)
    records = [r for r in caplog.records if "[V2-FLIP-OWNER-UNKNOWN]" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.ERROR
    assert state.flip_owner_phase == "unknown"


def test_same_source_class_cannot_be_presented_as_literal_old():
    value, _ = strategy(proposed)
    with pytest.raises(ValueError, match="literal_distinct_source"):
        compare_supplied_state([{}], old=value, proposed=value,
                               apply_event=None, capture=None)


def test_strict_off_does_not_add_nochase_or_change_admission(caplog):
    old_module = literal_old()
    old, now = strategy(old_module, strict=False)
    new, _ = strategy(proposed, strict=False)
    controlled_buy(old, old_module, now)
    state = controlled_buy(new, proposed, now)
    caplog.set_level(logging.INFO)
    assert old._strict_first_rest_admitted(old.watchlist_state("POM"), slot="first") is True
    assert new._strict_first_rest_admitted(state, slot="first") is True
    assert not any("[V2-POSTFLIP-NOCHASE]" in r.getMessage() for r in caplog.records)


def test_same_buy_bar_override_is_not_misclassified_as_nochase(caplog):
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)
    state.slotclear_fresh_buy_bar_ms = now
    state.slotclear_fresh_buy_segment_id = state.retry_one_segment_id
    caplog.set_level(logging.INFO)
    # Later evidence may legitimately refuse; the fresh-BUY override must not
    # be mislabeled as the earlier retry mismatch, or granted fabricated proof.
    assert value._strict_first_rest_admitted(state, slot="first") is False
    assert any("position_evidence_missing_or_stale" in r.getMessage()
               for r in caplog.records)
    assert not any("[V2-POSTFLIP-NOCHASE]" in r.getMessage() for r in caplog.records)


def test_old_markers_levels_and_now_call_count_match_literal_old(caplog):
    old_module = literal_old()
    old, now = strategy(old_module)
    new, _ = strategy(proposed)
    controlled_buy(old, old_module, now)
    controlled_buy(new, proposed, now)
    logs, clock_counts = [], []
    caplog.set_level(logging.INFO)
    for value in (old, new):
        calls = []
        value._now_ms = lambda: calls.append(now) or now
        caplog.clear()
        for _ in range(3):
            assert value._strict_first_rest_admitted(value.watchlist_state("POM"), slot="first") is False
        logs.append([(r.levelno, r.getMessage()) for r in caplog.records
                     if "[V2-POSTFLIP-NOCHASE]" not in r.getMessage()])
        clock_counts.append(len(calls))
    assert logs[0] == logs[1]
    assert clock_counts[0] == clock_counts[1]


def test_delayed_classification_not_implemented_as_future_severity_guess():
    value, _ = strategy(proposed)
    state = value.watchlist_state("SPRC")
    before = set(state.__dict__)
    value._set_flip_owner_unknown(state, reason="controlled_unanswered_submit", persist=False)
    assert set(state.__dict__) == before
    assert "_postflip_nochase_log" not in state.__dict__


@pytest.mark.parametrize("stage", ["allocate", "observe", "format", "new_log"])
def test_new_annotation_failures_cannot_change_real_gate_result(stage, monkeypatch, caplog):
    from project_mai_tai.strategy_core import postflip_nochase_log as observer_module

    value, now = strategy(proposed)
    old_module = literal_old()
    old, _ = strategy(old_module)

    def broken(*args, **kwargs):
        raise RuntimeError("controlled new annotation failure")

    if stage == "allocate":
        monkeypatch.setattr(observer_module, "PostFlipNoChaseLog", broken)
    elif stage == "observe":
        monkeypatch.setattr(observer_module.PostFlipNoChaseLog, "observe_transition", broken)
    old_state = controlled_buy(old, old_module, now)
    state = controlled_buy(value, proposed, now)
    if stage == "format":
        monkeypatch.setattr(state._postflip_nochase_log, "observed_line", broken)
    elif stage == "new_log":
        original = proposed.logger.info

        def log_only_new_failure(message, *args, **kwargs):
            if message == "%s" and args and "[V2-POSTFLIP-NOCHASE]" in str(args[0]):
                raise RuntimeError("controlled new log sink failure")
            return original(message, *args, **kwargs)

        monkeypatch.setattr(proposed.logger, "info", log_only_new_failure)
    caplog.set_level(logging.INFO)
    assert old._strict_first_rest_admitted(old_state, slot="first") is False
    assert value._strict_first_rest_admitted(state, slot="first") is False
    assert capture(old) == capture(value)
    assert any("[V2-FLIP-OWNER-ADMISSION]" in r.getMessage() for r in caplog.records)


def test_existing_logger_failure_is_not_swallowed_by_new_exception_boundary(monkeypatch):
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)
    original = proposed.logger.info

    def fail_old_marker(message, *args, **kwargs):
        if "[V2-FLIP-OWNER-ADMISSION]" in message:
            raise RuntimeError("controlled original logger failure")
        return original(message, *args, **kwargs)

    monkeypatch.setattr(proposed.logger, "info", fail_old_marker)
    with pytest.raises(RuntimeError, match="original logger failure"):
        value._strict_first_rest_admitted(state, slot="first")


def test_diagnostic_reset_failure_does_not_prevent_real_indicator_reset(monkeypatch):
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)

    def broken():
        raise RuntimeError("controlled observation reset failure")

    monkeypatch.setattr(state._postflip_nochase_log, "reset_indicator", broken)
    value._reset_atr_indicator_state(state, state.atr_session_anchor_ms)
    assert state.atr_state is None and state.atr_short_flip_bar_ts == 0


def test_broken_diagnostic_restore_does_not_prevent_seeded_math_restore():
    value, now = strategy(proposed)
    state = controlled_buy(value, proposed, now)

    class BrokenSnapshot:
        @property
        def current(self):
            raise RuntimeError("controlled observation snapshot failure")

        def reset_indicator(self):
            raise RuntimeError("controlled observation reset failure")

    state.__dict__["_postflip_nochase_log"] = BrokenSnapshot()
    value._atr_seeded_session_anchors[state.symbol] = state.atr_session_anchor_ms
    following = proposed.OHLCVBar(now + 60000, 11.0, 11.05, 10.95, 11.0, 25000)
    value._update_atr_state(state, following)
    assert state.atr_state == "long" and state.atr_prev_bar.timestamp_ms == now + 60000

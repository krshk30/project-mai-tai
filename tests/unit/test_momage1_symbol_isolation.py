from dataclasses import replace
from decimal import Decimal
import inspect
import textwrap

import pytest

from project_mai_tai.momentum_paper import warning_consumer as module
from project_mai_tai.momentum_paper.warning_consumer import MomentumWarningConsumer
from tests.unit.test_momentum_warning import START, bar


def observation(symbol, sequence, *, stale=False):
    item = bar(sequence, symbol=symbol, sequence=sequence,
               high="12" if symbol == "YYYY" else "10")
    observed = (START + (sequence + 3) * 1000) * 1_000_000
    if stale:
        item = replace(item, start_ms=item.start_ms - 10_000,
                       end_ms=item.end_ms - 10_000)
    return replace(item, received_utc_ns=observed, published_utc_ns=observed), observed


def model_with_live_y():
    model = MomentumWarningConsumer(prior_closes={"XXXX": Decimal("10"), "YYYY": Decimal("10")},
                                   condition_version="CONTROLLED", coverage_started_ms=START)
    for symbol, sequence in (("YYYY", 1), ("XXXX", 2)):
        item, observed = observation(symbol, sequence)
        model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
        model.complete_read(f"{observed // 1_000_000}-0", observed_ns=observed)
    assert model.engine.active_events
    return model


def assert_symbol_only_refusal():
    model = model_with_live_y()
    y_window = model.windows["YYYY"]
    y_active = model.engine.active_events
    y_warning = model.last_warning_ns["YYYY"]
    stale, now = observation("XXXX", 3, stale=True)
    outcome, records = model.observe_aggregate(stale, observed_ns=now, legacy_active=set())
    assert outcome.reason == "aggregate_source_end_stale"
    assert not records
    assert model._sequence == 3
    assert "XXXX" not in model.windows and "XXXX" not in model._latest
    assert "XXXX" not in model.last_warning_ns
    assert model.windows["YYYY"] is y_window
    assert model.last_warning_ns["YYYY"] == y_warning
    model.complete_read(f"{now // 1_000_000}-0", observed_ns=now)
    good, now = observation("YYYY", 4)
    _, records = model.observe_aggregate(good, observed_ns=now, legacy_active=set())
    assert not records
    assert model.windows["YYYY"] is y_window
    assert model.engine.active_events == y_active
    assert model._sequence == 4


def test_g_stale_x_preserves_y_window_and_active_warning_path():
    assert_symbol_only_refusal()


def test_h_real_sequence_gap_still_resets_fleet():
    model = model_with_live_y()
    item, observed = observation("XXXX", 4)
    _, records = model.observe_aggregate(item, observed_ns=observed, legacy_active=set())
    assert "YYYY" not in model.windows
    assert "YYYY" not in model._latest
    assert "YYYY" not in model.last_warning_ns
    assert records


def test_i_mutation_refusal_before_sequence_advance_breaks_isolation(monkeypatch):
    source = textwrap.dedent(inspect.getsource(MomentumWarningConsumer.observe_aggregate))
    block = '''    if age_reason is not None:
        # An in-order late bucket is a symbol hole, not a transport gap.
        self.windows.pop(bar.symbol, None)
        self._latest.pop(bar.symbol, None)
        self.last_warning_ns.pop(bar.symbol, None)
        self.last_status = age_reason
        return CandidateOutcome(bar.symbol, "REFUSED", age_reason), records
'''
    assert source.count(block) == 1
    early = '''    if age_reason is not None:
        self.invalidate_transport(age_reason)
        return CandidateOutcome(bar.symbol, "REFUSED", age_reason), ()
'''
    marker = "    if bar.epoch == self._stream_epoch and bar.sequence <= self._sequence:\n"
    mutated = source.replace(block, "").replace(marker, early + marker)
    namespace = dict(vars(module))
    exec(compile(mutated, "<controlled-old-order-mutation>", "exec"), namespace)
    monkeypatch.setattr(MomentumWarningConsumer, "observe_aggregate", namespace["observe_aggregate"])
    with pytest.raises(AssertionError):
        assert_symbol_only_refusal()

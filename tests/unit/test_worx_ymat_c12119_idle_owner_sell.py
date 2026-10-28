"""C12119 controlled owner-state check / AUTHOR UNRUN, not historical replay.

Both flags are explicit True. CLOSED prior false-flip and unused current owner
are reviewer-provided modeled premises, not reconstructed native receipts.
Native state remains UNKNOWN; the fresh local book is the existing fixture seam.
The exact OLD core is loaded as a separate test module, never installed or used
to replace/refactor an APP method. All other imported dependency types are20dc;
this is a pure owner-state control, NOT a full a823 runtime/world comparison.
"""

from dataclasses import asdict
import hashlib
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType

import pytest

from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar, SchwabV2Strategy
from project_mai_tai.v2_flip_entry_ownership import FlipPositionBook
from tests.unit import test_l11_c10719_sell_current_obligation as owner_fixture


ROOT = Path(__file__).resolve().parents[2]
OLD = "a823995951e89be85282c572be05340aefe348c5"
CORE = "src/project_mai_tai/strategy_core/schwab_1m_v2.py"
OLD_SHA = "fea95fcca8b94f5c043620cd440d5bf335fdf51b36f36e2bef6f9d5b86fba0f4"
OLD_BLOB = b"da84e3c6a78cd7e6c31b7ec17d1df7ed33f801ef"
CASES = [
    # SELL bar precedes its published observation by one minute. Next BUY below
    # is a CONTROLLED gate clock, not a reproduced original ARM/fill claim.
    ("WORX", 1792498927430, 1792504260000, 1792504322710, 1792506360000),
    ("YMAT", 1792758487033, 1792765080000, 1792765145213, 1792765200000),
]


@pytest.fixture(scope="module")
def exact_old_core():
    # One immutable local Git read, for Main's explicit existing test fence.
    # No Git state changes, shell, provider, child APP or new test runner.
    framed = subprocess.check_output(
        ["/usr/bin/git", "--no-replace-objects", "-C", str(ROOT), "cat-file", "--batch"],
        input=f"{OLD}:{CORE}\n".encode(), timeout=5,
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1",
             "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
             "GIT_NO_REPLACE_OBJECTS": "1"})
    assert len(framed) <= 2 * 1024 * 1024
    header, content = framed.split(b"\n", 1)
    oid, kind, size = header.split()
    assert oid == OLD_BLOB and kind == b"blob"
    raw = content[:int(size)]
    assert content[int(size):] == b"\n"
    assert hashlib.sha256(raw).hexdigest() == OLD_SHA
    name = "_c12119_exact_a823_owner_core"
    assert name not in sys.modules
    module = ModuleType(name)
    module.__file__ = f"{OLD}:{CORE}"
    sys.modules[name] = module
    try:
        exec(compile(raw, module.__file__, "exec", dont_inherit=True), module.__dict__)
        yield module.SchwabV2Strategy
    finally:
        assert sys.modules.pop(name) is module


def controlled_owner(monkeypatch, strategy_type, incident):
    symbol, opportunity, sell_bar, observed, _ = incident

    def explicit_settings(**values):
        return Settings(_env_file=None, **{
            **values,
            "strategy_schwab_1m_v2_keepwatch1_enabled": True,
            "strategy_schwab_1m_v2_brokerdecides1_enabled": True,
        })

    # Only the EXISTING fixture's constructors are selected here. No APP guard,
    # admission method, broker proof or retirement method is mocked.
    with monkeypatch.context() as seam:
        seam.setattr(owner_fixture, "Settings", explicit_settings)
        seam.setattr(owner_fixture, "SchwabV2Strategy", strategy_type)
        seam.setattr(owner_fixture, "NOW", observed)
        seam.setattr(owner_fixture, "SELL", sell_bar)
        seam.setattr(owner_fixture, "IDENTITY", opportunity)
        strategy, state, _ = owner_fixture.probe(keepwatch=True, card=False, symbol=symbol)
    strategy._now_ms = lambda: observed
    state.atr_state, state.atr_short_flip_bar_ts = "short", sell_bar
    state.bars = [OHLCVBar(sell_bar, 4.0, 4.1, 3.7, 3.8, 1000)]
    state.flip_owner_opportunity_id = state.fanout_segment_id = opportunity
    strategy.apply_flip_position_book(FlipPositionBook(observed, True, {}))
    writes = []
    strategy.configure_fanout_identity_persistence(
        lambda symbol, identity, active, reason: writes.append(("identity", identity, active, reason)))
    strategy.configure_flip_entry_ownership(
        lambda record, active, reason: writes.append(("owner", record.opportunity_id, active, reason)),
        restore_readable=True)
    assert strategy.settings.strategy_schwab_1m_v2_keepwatch1_enabled is True
    assert strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled is True
    assert getattr(strategy, "_brokerdecides1_memory", None) is None
    assert state.flip_owner_phase == "idle" and not state.flip_owner_first_rest_placed
    assert not state.flip_owner_fill_accounts and not state.flip_owner_position_ids
    assert not state.flip_owner_position_entry_ms and not state.flip_owner_open_positions
    assert not state.position_qty and not state.position_qty_held
    assert not state.resting_active and not state.webull_resting_active
    assert not state.cw_v2_emit_claimed and not state.fanout_webull_claimed
    assert not state.fanout_claim_slot_id and not state.fanout_claim_attempt_id
    assert not strategy._falseflip_entry_blocked(state)
    return strategy, state, writes


def input_identity(strategy, state):
    return (state.symbol, state.flip_owner_phase, state.flip_owner_opportunity_id,
        state.fanout_segment_id, state.atr_state, state.atr_short_flip_bar_ts,
        [asdict(bar) for bar in state.bars], state.flip_owner_evidence_at_ms,
        state.flip_owner_evidence_readable, state.flip_owner_first_rest_placed,
        set(state.flip_owner_fill_accounts), dict(state.flip_owner_position_ids),
        dict(state.flip_owner_position_entry_ms), dict(state.flip_owner_open_positions),
        state.position_qty, state.position_qty_held, state.resting_active,
        state.webull_resting_active, state.cw_v2_emit_claimed, state.fanout_webull_claimed,
        state.retry_one_segment_id, state.retry_one_closes_in_segment,
        state.retry_one_budget_readable, strategy._flip_owner_restore_readable,
        strategy.settings.strategy_schwab_1m_v2_keepwatch1_enabled,
        strategy.settings.strategy_schwab_1m_v2_brokerdecides1_enabled)


@pytest.mark.parametrize("incident", CASES, ids=["WORX1020-at0952-controlled-nativeUNKNOWN", "YMAT1023-at1019-controlled-nativeUNKNOWN"])
def test_unused_idle_owner_sell_old_unknown_new_retires_then_next_gate(
        monkeypatch, exact_old_core, incident):
    old, before, old_writes = controlled_owner(monkeypatch, exact_old_core, incident)
    new, after, new_writes = controlled_owner(monkeypatch, SchwabV2Strategy, incident)
    assert input_identity(old, before) == input_identity(new, after)
    opportunity = incident[1]
    retry_before = (after.retry_one_segment_id, after.retry_one_closes_in_segment,
                    after.retry_one_budget_readable)
    old._end_flip_owner_on_sell(before)
    new._end_flip_owner_on_sell(after)
    assert before.flip_owner_phase == "unknown"
    assert before.flip_owner_opportunity_id == before.fanout_segment_id == opportunity
    assert not any(not active for _, _, active, _ in old_writes)
    assert any(reason == "sell_flip_idle_owner_contains_evidence" for _, _, _, reason in old_writes)
    assert after.flip_owner_phase == "idle"
    assert after.flip_owner_opportunity_id == after.fanout_segment_id == 0
    assert after.flip_owner_sell_reset_pending is None
    assert after.fanout_last_retired_opportunity_id == opportunity
    assert new_writes == [
        ("identity", opportunity, False, "sell_ended_probe_identity"),
        ("owner", opportunity, False, "sell_ended_probe_identity")]
    assert retry_before == (after.retry_one_segment_id, after.retry_one_closes_in_segment,
                            after.retry_one_budget_readable)
    # No cap, owner phase or ID reset is performed by the test after SELL.
    # A fresh CONTROLLED LOCAL callback permits the actual next owner gate;
    # native permission and real10:27 ARM/economic execution remain UNKNOWN.
    buy_bar = incident[4]
    next_clock = buy_bar + 62_000
    for strategy, state in ((old, before), (new, after)):
        strategy._now_ms = lambda: next_clock
        state.atr_state = "long"
        state.bars = [OHLCVBar(buy_bar, 4.0, 4.14, 3.95, 4.1, 1000)]
        strategy.apply_flip_position_book(FlipPositionBook(next_clock, True, {}))
        strategy._bind_flip_owner_on_buy_flip(state, flip_bar_ts=buy_bar)
    assert not old._strict_first_rest_admitted(before, slot="first")
    assert new._strict_first_rest_admitted(after, slot="first")
    assert before.flip_owner_phase == "unknown" and after.flip_owner_phase == "idle"
    assert not old._pending_intents and not new._pending_intents

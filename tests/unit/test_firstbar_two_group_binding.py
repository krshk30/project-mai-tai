"""Reviewed producer generation controls, not historical/native acceptance."""

import pytest

from project_mai_tai.atr_simulation.firstbar_binding import (
    FIRSTBAR_ENGINE_HEAD, FIRSTBAR_RESULT_VERSION, load_firstbar_engine,
)
from project_mai_tai.atr_simulation.firstbar_study_publication import (
    EXIT_POLICY_V2, PRODUCER_FILES, PRODUCER_FILES_V1, ProducerBinding, _producer,
)


def operands(tmp_path, members, policy):
    pins = tuple((name, "a" * 64) for name in members)
    binding = ProducerBinding(str(tmp_path), "b" * 40, pins,
        (("CONTROLLED:one", "schwab"),), "schwab_1m_v2")
    receipt = {"source_pins": {str(tmp_path / name): digest for name, digest in pins}}
    if policy is not None:
        receipt["exit_policy_version"] = policy
    return receipt, binding


def test_v2_requires_all_fourteen_independently_bound_members(tmp_path):
    receipt, binding = operands(tmp_path, PRODUCER_FILES, EXIT_POLICY_V2)
    assert len(PRODUCER_FILES) == 14
    assert _producer(receipt, binding) == {"CONTROLLED:one": "schwab"}


def test_v2_cannot_omit_live_target_leaf_from_producer_binding(tmp_path):
    receipt, binding = operands(tmp_path, PRODUCER_FILES[:-1], EXIT_POLICY_V2)
    with pytest.raises(ValueError, match="exact_reviewed_producer_members"):
        _producer(receipt, binding)


def test_fourteen_members_without_policy_cannot_impersonate_v1(tmp_path):
    receipt, binding = operands(tmp_path, PRODUCER_FILES, None)
    with pytest.raises(ValueError, match="exact_reviewed_producer_members"):
        _producer(receipt, binding)


def test_unknown_exit_policy_is_not_a_reviewed_generation(tmp_path):
    receipt, binding = operands(tmp_path, PRODUCER_FILES, "future-policy")
    with pytest.raises(ValueError, match="producer_generation"):
        _producer(receipt, binding)


def test_original_twelve_member_identity_is_not_silently_upgraded(tmp_path):
    receipt, binding = operands(tmp_path, PRODUCER_FILES_V1, None)
    assert len(PRODUCER_FILES_V1) == 12
    assert "exit_policy_version" not in receipt
    assert _producer(receipt, binding) == {"CONTROLLED:one": "schwab"}
    assert "exit_policy_version" not in receipt


def test_actual_bound_engine_uses_reviewed_successor_and_pinned_live_leaf():
    engine, api = load_firstbar_engine()
    assert FIRSTBAR_ENGINE_HEAD == "e712a99800b9cfd716b406e027845f89567647fe"
    assert api.FIRSTBAR_REPLAY_VERSION == FIRSTBAR_RESULT_VERSION
    assert type(engine).__name__ == "FirstBarReplayEngine"
    assert "V2_A2_CEILING_CAUSAL_STOP_PRINT" in FIRSTBAR_RESULT_VERSION

"""Measured rules-lookup cut; every print still validates its full evidence."""
from dataclasses import replace
import gc
import hashlib
from pathlib import Path
import weakref

import pytest

from project_mai_tai.atr_simulation import execution_eligibility as original
from project_mai_tai.atr_simulation import same_sip_eligibility as fast
from project_mai_tai.atr_simulation import same_sip_binding as binding
from tests.support.atr_same_sip_cases import trade, POLICY, SOURCE, encoded


@pytest.fixture(autouse=True)
def fresh_identity_cache():
    fast._RULES_BY_IDENTITY.clear()
    yield
    fast._RULES_BY_IDENTITY.clear()


@pytest.mark.parametrize('size', (37, 100))
@pytest.mark.parametrize('raw,present', ((None, True), (None, False), ('', True), ('14', True),
    ('37', True), ('bad', True), ('999999', True), ([14], True), ((-1,), True), (False, True)))
def test_every_classification_and_attached_evidence_outcome_is_literal_original(size, raw, present):
    row = trade(61, size=size)
    expected = original.classify_execution(row, raw, POLICY, conditions_present=present)
    actual = fast.classify_execution(row, raw, POLICY, conditions_present=present)
    assert encoded(actual) == encoded(expected)
    bound = replace(row, execution=actual)
    assert fast.execution_status(bound) == original.execution_status(bound)


def test_repeated_prints_pay_original_rules_loader_once_per_policy_object(monkeypatch):
    policy = replace(POLICY, source=replace(SOURCE, artifact='CONTROLLED:identity-policy'))
    row = trade(61)
    calls = []
    loader = original._rules
    def load(value):
        calls.append(value)
        return loader(value)
    monkeypatch.setattr(original, '_rules', load)
    for index in range(1000):
        result = fast.classify_execution(row, '14', policy)
        assert fast.execution_status(replace(row, execution=result))[0] is True
    assert len(calls) == 1 and calls[0] is policy


def test_distinct_equal_policies_are_separate_identity_entries(monkeypatch):
    left, right = replace(POLICY), replace(POLICY)
    row = trade(61)
    assert left == right and left is not right
    calls = []
    loader = original._rules
    monkeypatch.setattr(original, '_rules', lambda value: calls.append(id(value)) or loader(value))
    for policy in (left, right, left, right):
        fast.classify_execution(row, '14', policy)
    assert calls == [id(left), id(right)]
    assert fast._RULES_BY_IDENTITY[id(left)][0] is left
    assert fast._RULES_BY_IDENTITY[id(right)][0] is right


def test_strong_reference_prevents_id_reuse_and_cache_has_eight_entry_bound():
    policy = replace(POLICY, version='CONTROLLED:strongref')
    retained = weakref.ref(policy)
    fast.classify_execution(trade(61), '14', policy)
    original._rules.cache_clear()
    del policy
    gc.collect()
    assert retained() is not None
    for index in range(10):
        fast.classify_execution(trade(61), '14', replace(POLICY, version='CONTROLLED:'+str(index)))
    assert len(fast._RULES_BY_IDENTITY) == 8


@pytest.mark.parametrize('field', ('raw_id', 'raw_source', 'size', 'eligible', 'reason', 'conditions_present'))
def test_cached_rules_never_skip_per_print_identity_or_classification_validation(field):
    row = trade(61)
    evidence = fast.classify_execution(row, '14', POLICY)
    assert fast.execution_status(replace(row, execution=evidence))[0] is True
    values = {'raw_id': 'OTHER', 'raw_source': replace(SOURCE, artifact='CONTROLLED:OTHER'),
        'size': 200, 'eligible': False, 'reason': 'UNPROVEN', 'conditions_present': False}
    changed = replace(row, execution=replace(evidence, **{field: values[field]}))
    for method in (original.execution_status, fast.execution_status):
        with pytest.raises(ValueError):
            method(changed)


@pytest.mark.parametrize('fault', ('source_sha', 'duplicate_code', 'nonboolean_flag', 'empty_rules'))
def test_invalid_source_or_rules_keep_original_refusal(fault):
    policy = replace(POLICY)
    if fault == 'source_sha':
        policy = replace(policy, source=replace(SOURCE, sha256='bad'))
    elif fault == 'duplicate_code':
        policy = replace(policy, rules=(policy.rules[0], policy.rules[0]))
    elif fault == 'nonboolean_flag':
        policy = replace(policy, rules=(replace(policy.rules[0], updates_volume=1),))
    else:
        policy = replace(policy, rules=())
    for method in (original.classify_execution, fast.classify_execution):
        with pytest.raises(ValueError):
            method(trade(61), '14', policy)
    assert id(policy) not in fast._RULES_BY_IDENTITY


def test_mutable_unhashable_policy_graph_does_not_gain_new_acceptance():
    policy = replace(POLICY, rules=list(POLICY.rules))
    for method in (original.classify_execution, fast.classify_execution):
        with pytest.raises(TypeError):
            method(trade(61), '14', policy)


def test_first_identity_miss_still_invokes_original_source_validation(monkeypatch):
    row = trade(61)
    def denied(policy): raise ValueError('existing_condition_classifier_source_mismatch')
    monkeypatch.setattr(original, '_rules', denied)
    with pytest.raises(ValueError, match='source_mismatch'):
        fast.classify_execution(row, '14', POLICY)
    assert fast._RULES_BY_IDENTITY == {}


def test_new_identity_helper_is_pinned_without_replacing_original_seals():
    root = Path(fast.__file__).parent
    assert binding.SOURCE_PINS['same_sip_eligibility.py'] == hashlib.sha256(Path(fast.__file__).read_bytes()).hexdigest()
    assert binding.verify_same_sip_files(root) == binding.POLICY_VERSION

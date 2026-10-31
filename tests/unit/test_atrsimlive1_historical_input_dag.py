"""Independent controlled input-DAG cap semantics, no market replay."""
from dataclasses import replace
from decimal import Decimal
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

import atrsimlive1_historical_models as subject
from atrsimlive1_captured_rr import CapturedRefusal


def test_large_shared_typed_cohort_binds_all_edges_with_bounded_unique_content(monkeypatch):
    from project_mai_tai.atr_simulation.types import SourceRef, EvidenceKind

    source = SourceRef('CONTROLLED:shared-source', 'a' * 64, '$', EvidenceKind.CONTROLLED)
    # Distinct dataclass objects; compound-object memo alone cannot reduce
    # their repeated scalar fields. Literal smaller CONTROLLED cap exercises
    # the production 2M-cap mechanism without allocating 2M fixture objects.
    cohort = tuple(replace(source) for _ in range(5000))
    monkeypatch.setattr(subject, 'INPUT_NODE_CAP', 6000)
    counters = {}
    digest = subject.typed_input_fingerprint(cohort, statistics=counters)
    # Literal controlled structure, independent of the production traversal.
    # It binds all5000 members and every original SourceRef field/type/order.
    def h(document):
        return hashlib.sha256(json.dumps(document, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    source_digest = h(['dataclass', 'project_mai_tai.atr_simulation.types.SourceRef', [
        ['artifact', h(['str', 'CONTROLLED:shared-source'])],
        ['sha256', h(['str', 'a' * 64])], ['pointer', h(['str', '$'])],
        ['kind', h(['enum', 'project_mai_tai.atr_simulation.types.EvidenceKind',
                    'CONTROLLED', h(['str', 'CONTROLLED'])])],
        ['asof_ns', h(['NoneType', None])]]])
    assert digest == h(['tuple', [source_digest] * 5000])
    assert counters['total_visits'] > 25000
    assert 5000 < counters['attempted_unique_nodes'] < 6000
    assert counters['scalar_memo_hits'] > 20000
    assert counters['object_memo_entries'] == 5001
    assert counters['object_memo_entries'] + counters['scalar_memo_entries'] <= 6000
    changed = (*cohort[:-1], replace(cohort[-1], pointer='CHANGED'))
    assert subject.typed_input_fingerprint(changed) != digest


def test_unique_content_cap_still_refuses_and_exposes_actual_counters(monkeypatch):
    monkeypatch.setattr(subject, 'INPUT_NODE_CAP', 32)
    counters = {}
    with pytest.raises(CapturedRefusal, match='historical_input_node_cap_unique=33'):
        subject.typed_input_fingerprint(tuple(range(40)), statistics=counters)
    assert counters['attempted_unique_nodes'] == 33
    assert counters['object_memo_entries'] + counters['scalar_memo_entries'] <= 32


def test_scalar_memo_preserves_exact_typed_values_within_same_digest_call():
    clock = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)
    values = (True, 1, Decimal('2.0300'), Decimal('2.03'), -0.0, 0.0,
              clock, clock.astimezone(timezone(timedelta(hours=-4))))
    actual = subject.typed_input_fingerprint(values)
    for index in (0, 2, 4, 6):
        collapsed = (*values[:index], values[index + 1], *values[index + 1:])
        assert subject.typed_input_fingerprint(collapsed) != actual
    counters = {}
    assert subject.typed_input_fingerprint((*values, *values), statistics=counters)
    assert counters['scalar_memo_hits'] >= len(values)

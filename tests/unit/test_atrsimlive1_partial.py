"""Main-only CONTROLLED private files/owned tiny children; author UNRUN.

Three tests below start literal Python children via the ORIGINAL bounded_child.
No SQL/provider/model/native claim; Main must review this process membership.
"""
from copy import deepcopy
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = 'a' * 64
PROGRAM = """
import json, sys
from datetime import datetime
from pathlib import Path
root, directory, mode = sys.argv[1:]
sys.path[:0] = [str(Path(root)/'tools'), str(Path(root)/'src'), root]
from atrsimlive1_partial_run import PartialWriter
from tests.unit.test_atrsimlive1_output_split import original
doc = original()
asof = datetime.fromisoformat(doc['source_asof'])
writer = PartialWriter(directory, source_seal='a'*64, budget_seconds=1)
# CONTROLLED roster/census boundary, NOT a database transaction or native read.
writer.bind_roster(day=doc['trading_day'], source_asof=asof,
    snapshot_id=doc['producer']['snapshot_id'], roster=['ELPW', 'PENDING'])
if mode == 'one':
    data = dict(symbols=['ELPW'], trading_day=doc['trading_day'], source_asof=asof,
        snapshot_id=doc['producer']['snapshot_id'], CONTROLLED_RAW_INPUT={'conditions': None})
    writer.process(data, lambda _: doc)
print(json.dumps(writer.finish()))
"""


def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'tools'))
    return (importlib.import_module('atrsimlive1_partial'),
            importlib.import_module('atrsimlive1_partial_run'),
            importlib.import_module('atrsimlive1_runtime'),
            importlib.import_module('atrsimlive1_incremental_cache'))


def actual_fixture(monkeypatch, tmp_path, mode='one'):
    partial, run, runtime, cache = modules(monkeypatch)
    root = (tmp_path / 'owned-run').resolve()
    root.mkdir(mode=0o700)
    run_id = run.run_key(root, SOURCE)
    closed = []
    command = [sys.executable, '-c', PROGRAM, str(ROOT), str(root), mode]
    raw, _ = runtime.bounded_child(command, seconds=20,
        closure_observer=lambda process: closed.append(partial.observe_closed_child(process,
            source_seal=SOURCE, run_id=run_id)))
    assert len(closed) == 1 and closed[0]['returncode'] == 0 and closed[0]['pipes_closed'] is True
    return root, runtime.decode(raw), closed[0], run, cache


def test_actual_owned_child_partial_rejects_corruption_then_preserves_pending_and_last_good(monkeypatch, tmp_path):
    root, draft, closure, run, cache = actual_fixture(monkeypatch, tmp_path)
    meta_path, component_path = root / run.REGISTER, root / 'completed-000.json'
    raw_meta, raw_component = meta_path.read_bytes(), component_path.read_bytes()
    original = json.loads(raw_component)
    evidence_path = root / 'symbol-000' / 'atrsim-evidence.jsonl.gz'
    raw_evidence = evidence_path.read_bytes()
    for fault in ('foreign-source', 'wrong-pid', 'path-escape', 'snapshot', 'evidence-bytes', 'fake-closure', 'wrong-pending'):
        metadata, component = json.loads(raw_meta), deepcopy(original)
        observed, child = closure, deepcopy(draft)
        if fault == 'foreign-source':
            metadata['source_seal'] = 'b' * 64
        elif fault == 'wrong-pid':
            metadata['child_pid'] += 1
        elif fault == 'path-escape':
            component['directory'] = '../foreign'
        elif fault == 'snapshot':
            component['component']['component']['snapshot_hash'] = 'c' * 64
        elif fault == 'evidence-bytes':
            evidence_path.write_bytes(bytes([raw_evidence[0] ^ 1]) + raw_evidence[1:])
        elif fault == 'fake-closure':
            observed = dict(closure)
        else:
            child['pending_symbols'] = []
        meta_path.write_text(json.dumps(metadata))
        component_path.write_text(json.dumps(component))
        with pytest.raises((cache.CacheRefusal, ValueError)):
            run.finalize(root, source_seal=SOURCE, closure=observed,
                stop_reason='COOPERATIVE_CHILD_EXIT', child_document=child)
        assert not (root / run.RESULT).exists()
        meta_path.write_bytes(raw_meta)
        component_path.write_bytes(raw_component)
        evidence_path.write_bytes(raw_evidence)
    last_good = tmp_path / 'current.json'
    last_good.write_bytes(b'ORIGINAL COMPLETE FILE')
    receipt = run.finalize(root, source_seal=SOURCE, closure=closure,
        stop_reason='COOPERATIVE_CHILD_EXIT', child_document=draft)
    assert receipt['status'] == 'PARTIAL' and receipt['label'] == '1 symbols pending'
    assert receipt['original_symbols'] == ['ELPW', 'PENDING']
    assert receipt['completed_symbols'] == ['ELPW'] and receipt['pending_symbols'] == ['PENDING']
    assert receipt['pending'][0]['pnl'] is None and receipt['pending'][0]['pnl_label_known'] is False
    assert receipt['whole_population_pnl'] is None and receipt['native_execution_qualified'] is False
    assert receipt['actual_child_closure']['pid'] == closure['pid']
    assert receipt['qualification'] == 'INCOMPLETE_NOT_A_FULL_RUN_PASS'
    assert last_good.read_bytes() == b'ORIGINAL COMPLETE FILE'
    assert evidence_path.read_bytes() == raw_evidence
    assert (root / run.RESULT).stat().st_mode & 0o777 == 0o600


def test_zero_completed_after_actual_closed_roster_is_all_pending_never_zero_economics(monkeypatch, tmp_path):
    root, draft, closure, run, _ = actual_fixture(monkeypatch, tmp_path, mode='none')
    receipt = run.finalize(root, source_seal=SOURCE, closure=closure,
        stop_reason='COOPERATIVE_CHILD_EXIT', child_document=draft)
    assert receipt['status'] == 'PENDING' and receipt['label'] == '2 symbols pending'
    assert receipt['completed'] == [] and receipt['whole_population_pnl'] is None
    assert all(row['pnl'] is None and row['pnl_label_known'] is False for row in receipt['pending'])


def test_actual_timeout_joins_child_before_observer_and_cannot_fabricate_roster(monkeypatch, tmp_path):
    partial, run, runtime, _ = modules(monkeypatch)
    root = (tmp_path / 'timed-out').resolve()
    root.mkdir(mode=0o700)
    key, closed = run.run_key(root, SOURCE), []
    with pytest.raises(TimeoutError, match='today_wall_deadline'):
        runtime.bounded_child([sys.executable, '-c', 'import time; time.sleep(60)'], seconds=.05,
            closure_observer=lambda process: closed.append(partial.observe_closed_child(process,
                run_id=key, source_seal=SOURCE, allow_failed=True)))
    assert len(closed) == 1 and closed[0]['returncode'] != 0
    assert closed[0]['successful_exit'] is False and closed[0]['pipes_closed'] is True
    run.record_timeout(root, closure=closed[0], source_seal=SOURCE)
    assert (root / 'deadline-timeout.json').exists()
    with pytest.raises(FileNotFoundError):
        run.finalize(root, source_seal=SOURCE, closure=closed[0], stop_reason='ACTUAL_CHILD_WALL_TIMEOUT')
    assert not (root / run.RESULT).exists()


@pytest.mark.parametrize('budget', [False, 0, -1, 240, float('nan'), float('inf')])
def test_partial_invalid_soft_budget_refuses_before_files(monkeypatch, tmp_path, budget):
    _, run, _, cache = modules(monkeypatch)
    tmp_path.chmod(0o700)
    with pytest.raises(cache.CacheRefusal, match='partial_soft_budget_below_original_hard_deadline_required'):
        run.PartialWriter(tmp_path.resolve(), source_seal=SOURCE, budget_seconds=budget)
    assert list(tmp_path.iterdir()) == []


def test_no_roster_no_deadline_permission_and_remaining_budget_is_global(monkeypatch, tmp_path):
    _, run, _, cache = modules(monkeypatch)
    from atrsimlive1_output import EvidenceWriter, NAME

    tmp_path.chmod(0o700)
    writer = run.PartialWriter(tmp_path.resolve(), source_seal=SOURCE, budget_seconds=1)
    with pytest.raises(cache.CacheRefusal, match='full_roster_must_precede_partial_deadline_check'):
        writer.stop_before_next_symbol()
    with EvidenceWriter(tmp_path, remaining_bytes=10) as evidence:
        with pytest.raises(ValueError, match='evidence_byte_bound_no_truncation'):
            evidence.append('input', {'CONTROLLED': 'larger than remaining global budget'}, symbol='ELPW')
    assert not (tmp_path / NAME).exists()


def test_fake_process_and_bare_components_never_supply_actual_closure_or_seal(monkeypatch):
    partial, _, _, cache = modules(monkeypatch)
    with pytest.raises(cache.CacheRefusal, match='actual_owned_Popen_required'):
        partial.observe_closed_child(SimpleNamespace(pid=1, returncode=0), run_id='b' * 64, source_seal=SOURCE)
    run = partial.PartialRun(['ELPW'], trading_day='2026-10-28', source_asof='2026-10-28T18:00:00+00:00',
        snapshot_hash='c' * 64, source_seal=SOURCE, child_pid=1, run_id='b' * 64, enabled=True)
    with pytest.raises(cache.CacheRefusal, match='actual_sealed_component_readback_required'):
        run.add({'symbol': 'ELPW', 'pnl': 0})

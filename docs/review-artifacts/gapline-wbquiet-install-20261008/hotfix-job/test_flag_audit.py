import hashlib
import importlib.util
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('hotfix_flag_audit', Path(__file__).with_name('flag_audit.py'))
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def test_current_selector_is_reviewed_repo_source_not_old_box_registry():
    assert str(audit.CHECKER) == '/home/trader/project-mai-tai/ops/health/expected_flags_check.py'
    assert audit.CHECKER_SHA256 == '7931090ffc4e98429d31432aaf2cdd46291100314c2a2e504dea21339d8721c3'
    assert str(audit.NUMERIC) == '/home/trader/restart_evidence/expected_numeric.json'


def test_exact_box_selector_and_receipt_provenance(tmp_path, monkeypatch):
    checker, catalog, numeric, out, err = [tmp_path / name for name in ('box-checker.py', 'flags.json', 'numeric.json', 'out', 'err')]
    checker.write_bytes(b'pinned isolated checker fixture')
    catalog.write_bytes(b'{"flags":[]}')
    numeric.write_bytes(b'{"numeric":[]}')
    out.write_text('FLAGGATE checked=153/155 mismatches=0 unknown=2\nUNKNOWN service=momentum-paper reason=inactive\nUNKNOWN service=momentum-paper reason=inactive numeric\n')
    err.write_text('')
    monkeypatch.setattr(audit, 'CHECKER_SHA256', hashlib.sha256(checker.read_bytes()).hexdigest())
    args = audit.command('/python', checker=checker, catalog=catalog, numeric=numeric)
    assert args == ['/python', str(checker), '--catalog', str(catalog), '--numeric-catalog', str(numeric)]
    result = audit.receipt(args, 2, out, err)
    assert result['checker_path'] == str(checker)
    assert result['checker_sha256'] == audit.CHECKER_SHA256
    assert result['catalog_sha256'] == hashlib.sha256(catalog.read_bytes()).hexdigest()
    assert result['numeric_catalog_sha256'] == hashlib.sha256(numeric.read_bytes()).hexdigest()
    assert result['lines'] == out.read_text().splitlines()
    assert audit.readable_outcome(result)
    checker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='byte hash differs'):
        audit.command('/python', checker=checker, catalog=catalog, numeric=numeric)


@pytest.mark.parametrize('rc,lines', [
    (2, ['FLAGGATE checked=0/0 mismatches=0 unknown=1', 'UNKNOWN market_data_subscription_startup_enabled: invalid also_check_services']),
    (2, ['FLAGGATE checked=0/0 mismatches=0 unknown=1', 'UNKNOWN inventory extra orb_intrabar_reclaim_enabled']),
    (1, ['FLAGGATE checked=153/155 mismatches=1 unknown=2', 'REAL FAILURE service=oms flag=false_flip']),
    (2, ['UNKNOWN service=momentum-paper reason=inactive']),
    (124, ['FLAGGATE checked=153/155 mismatches=0 unknown=0']),
    (0, ['FLAGGATE checked=155/155 mismatches=1 unknown=0']),
    (0, ['FLAGGATE checked=151/153 mismatches=0 unknown=0']),
    (0, ['FLAGGATE checked=0/155 mismatches=0 unknown=0']),
])
def test_read_failure_or_missing_denominator_never_becomes_pass(rc, lines):
    assert not audit.readable_outcome(dict(rc=rc, lines=lines))

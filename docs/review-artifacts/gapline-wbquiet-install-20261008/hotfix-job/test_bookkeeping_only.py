"""Bookkeeping scope and unreadable-only retry controls."""
import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('closure', HERE / 'bookkeeping_only.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


@pytest.mark.parametrize('codes,waits', [([0], []), ([1], []), ([2, 0], [60]), ([2, 2, 2], [60, 60])])
def test_bookkeeping_gate_retries_only_unknown_and_preserves_both_streams(codes, waits):
    seen = []
    def run(command, **kwargs):
        assert command[-1].endswith('gate_readonly.py') and kwargs['timeout'] == 240
        return SimpleNamespace(returncode=codes.pop(0), stdout='raw stdout', stderr='raw stderr')
    result = m.gate(run, seen.append)
    assert not codes and seen == waits
    assert all(row['stdout'] == 'raw stdout' and row['stderr'] == 'raw stderr' for row in result)


def test_literal_closure_never_restarts_or_waives_failed_proof():
    source = (HERE / 'bookkeeping_only.py').read_text()
    tree = ast.parse(source)
    assert 'repin.apply' in source and 'never COMPLETE' in source
    assert 'LOCK_EX | fcntl.LOCK_NB' in source and '/run/lock/project-mai-tai-deploy.lock' in source
    assert 'sealed()' in source and "'xb'" in source
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'run':
            assert ast.literal_eval(call.args[0]) == ['bash', '-n']
    assert not any(value in source for value in ('systemctl restart', 'alembic upgrade', 'COMPLETE.json', 'unlink('))

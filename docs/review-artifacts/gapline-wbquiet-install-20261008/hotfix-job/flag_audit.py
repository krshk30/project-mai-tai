"""Current approved registry plus preserved box numeric policy, without flag edits."""
import hashlib
from pathlib import Path
import re

CHECKER = Path('/home/trader/project-mai-tai/ops/health/expected_flags_check.py')
CATALOG = Path('/home/trader/restart_evidence/expected_flags.json')
NUMERIC = Path('/home/trader/restart_evidence/expected_numeric.json')
CHECKER_SHA256 = '7931090ffc4e98429d31432aaf2cdd46291100314c2a2e504dea21339d8721c3'


def command(python, *, checker=CHECKER, catalog=CATALOG, numeric=NUMERIC):
    raw = checker.read_bytes()
    if hashlib.sha256(raw).hexdigest() != CHECKER_SHA256:
        raise ValueError('approved current checker byte hash differs')
    return [str(python), str(checker), '--catalog', str(catalog), '--numeric-catalog', str(numeric)]


def receipt(args, rc, stdout, stderr):
    """Retain rc and every result line, including inventory UNKNOWN and paper."""
    checker, catalog, numeric = map(Path, (args[1], args[3], args[5]))
    return dict(rc=rc, checker_path=str(checker), checker_sha256=hashlib.sha256(checker.read_bytes()).hexdigest(),
                catalog_path=str(catalog), catalog_sha256=hashlib.sha256(catalog.read_bytes()).hexdigest(),
                numeric_catalog_path=str(numeric), numeric_catalog_sha256=hashlib.sha256(numeric.read_bytes()).hexdigest(),
                stdout=str(stdout), stderr=str(stderr), lines=stdout.read_text().splitlines(),
                stdout_sha256=hashlib.sha256(stdout.read_bytes()).hexdigest(),
                stderr_sha256=hashlib.sha256(stderr.read_bytes()).hexdigest(),
                source_policy='approved current boolean inventory; actual box numeric policy retained unchanged',
                settings_inventory='deployed application Settings; not a frozen substitute')


def readable_outcome(result):
    lines = result['lines']
    real = [line for line in lines if line.startswith('REAL FAILURE')]
    unknown = [line for line in lines if line.startswith('UNKNOWN')]
    header = [line for line in lines if 'checked=' in line and 'mismatches=' in line and 'unknown=' in line]
    if real or len(header) != 1:
        return False
    parsed = re.search(r'checked=(\d+)/(\d+) mismatches=(\d+) unknown=(\d+)', header[0])
    if not parsed:
        return False
    checked, total, mismatches, count = map(int, parsed.groups())
    if total != 155 or mismatches != 0 or count != len(unknown) or checked + count != total:
        return False
    # rc 2 is not a success waiver: only the named inactive paper coverage limit
    # is accepted, with the measured denominator/header preserved in the receipt.
    return result['rc'] in (0, 2) and all('service=momentum-paper ' in line for line in unknown)

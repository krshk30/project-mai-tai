"""Controlled source-reader negatives; no actual retained GZ or model execution."""
import hashlib
from pathlib import Path
import sys

import pytest

# Do not depend on another test file adding repository tools during collection.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from atrsimlive1_captured_rr import (
    CapturedRefusal, decode, original_datetime, original_pairs, verified_bytes,
    verify_closed_original,
)


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'])
def test_captured_json_refuses_duplicate_and_nonfinite(raw):
    with pytest.raises(CapturedRefusal):
        decode(raw)


def test_captured_json_preserves_numeric_string_distinction():
    assert decode(b'{"jsonb":1.5,"sql_decimal":"1.5"}') == {
        'jsonb': 1.5, 'sql_decimal': '1.5'}


def test_metadata_hash_mismatch_never_admits_input(tmp_path):
    path = tmp_path / 'metadata.json'
    path.write_bytes(b'{}')
    path.chmod(0o600)
    with pytest.raises(CapturedRefusal, match='hash_mismatch'):
        verified_bytes({'path': str(path), 'sha256': '0' * 64})


def test_metadata_exact_bytes_and_no_symlink(tmp_path):
    path = tmp_path / 'metadata.json'
    raw = b'{"scope":"CONTROLLED"}\n'
    path.write_bytes(raw)
    path.chmod(0o600)
    spec = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
    assert verified_bytes(spec) == raw
    alias = tmp_path / 'alias.json'
    alias.symlink_to(path)
    with pytest.raises(CapturedRefusal, match='literal_captured_file_path'):
        verified_bytes({**spec, 'path': str(alias)})


@pytest.mark.parametrize('scope', ['CURRENT_RR', 'RETAINED_FILE', None])
def test_historical_binding_never_implicitly_admits_other_scopes(scope):
    with pytest.raises(CapturedRefusal, match='historical_capture_scope'):
        verify_closed_original({'scope': scope})


def test_plain_object_cannot_claim_original_capture_capability():
    with pytest.raises(CapturedRefusal, match='closed_original_capture_witness'):
        next(original_pairs(object()))


def test_original_clock_keeps_day_and_offset_and_refuses_naive():
    assert original_datetime('2026-10-29T03:36:15.030688+00:00').isoformat() == (
        '2026-10-29T03:36:15.030688+00:00')
    with pytest.raises(CapturedRefusal, match='original_aware_clock'):
        original_datetime('2026-10-29T03:36:15.030688')


def test_relative_metadata_path_refuses_before_read():
    with pytest.raises(CapturedRefusal, match='literal_captured_file_path'):
        verified_bytes({'path': 'foreign.json', 'sha256': '0' * 64})

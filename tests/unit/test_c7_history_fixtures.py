"""Historical fixture integrity fails closed without optional skips."""
import shutil

import pytest

from tests import c7_history


@pytest.mark.parametrize("defect", ["missing", "segment_corrupt", "manifest_corrupt"])
def test_history_fixture_damage_is_a_failure(tmp_path, monkeypatch, defect):
    root = tmp_path / "history"
    shutil.copytree(c7_history.ROOT, root)
    monkeypatch.setattr(c7_history, "ROOT", root)
    fragment = root / "20266cdfc614905eb62d309fa99e472058ccec99.txt"
    if defect == "missing":
        fragment.unlink()
    elif defect == "segment_corrupt":
        raw = fragment.read_bytes()
        fragment.write_bytes(b"X" + raw[1:])
    else:
        manifest = root / "manifest.json"
        raw = manifest.read_bytes()
        manifest.write_bytes(b"X" + raw[1:])
    with pytest.raises((AssertionError, FileNotFoundError)):
        c7_history.historical_source("20266cdf", "src/project_mai_tai/oms/service.py")

import json
import re

import pytest

from tests import c7_retained


def test_all_retained_inputs_are_bounded_and_hash_pinned():
    manifest = c7_retained.retained_manifest()
    assert len(manifest["artifacts"]) == 6
    for name in manifest["artifacts"]:
        raw = c7_retained.retained_bytes(name)
        assert not re.search(rb'(?i)(authorization|password|api[_-]?key|access[_-]?token|account[_-]?(?:id|number))', raw)


@pytest.mark.parametrize("fault", ["missing", "digest", "bound"])
def test_missing_tampered_or_unbounded_retained_input_refuses(tmp_path, monkeypatch, fault):
    manifest = c7_retained.retained_manifest()
    name = "v2-tail-1m-plus-anchor.txt"
    packed = (c7_retained.ROOT / (name + ".gz")).read_bytes()
    if fault == "digest":
        (tmp_path / (name + ".gz")).write_bytes(packed[:-1] + bytes([packed[-1] ^ 1]))
    if fault == "bound":
        manifest["artifacts"][name]["size"] = 8388609
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(c7_retained, "ROOT", tmp_path)
    with pytest.raises((AssertionError, FileNotFoundError)):
        c7_retained.retained_bytes(name)

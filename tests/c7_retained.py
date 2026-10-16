"""Bounded, hash-pinned offline inputs; no private paths or optional skips."""
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/c7_retained"


def retained_manifest():
    assert (ROOT / "manifest.json").stat().st_size <= 16384
    manifest = json.loads((ROOT / "manifest.json").read_text())
    assert manifest["version"] == 1
    return manifest


def retained_bytes(name):
    metadata = retained_manifest()["artifacts"][name]
    assert 0 < metadata["size"] <= 8 * 1024 * 1024
    path = ROOT / (name + ".gz")
    assert path.stat().st_size == metadata["compressed_size"] <= 2 * 1024 * 1024
    assert hashlib.sha256(path.read_bytes()).hexdigest() == metadata["compressed_sha256"]
    with gzip.open(path, "rb") as stream:
        raw = stream.read(metadata["size"] + 1)
    assert len(raw) == metadata["size"]
    assert hashlib.sha256(raw).hexdigest() == metadata["sha256"]
    return raw

"""Verbatim, bounded historical fragments; never consult Git at test time."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).parent / "fixtures/c7_history"
MANIFEST_SHA256 = "b7c20995d11af6e0259adb37e884407906666d48d97ffbe37187647dd2203957"


def historical_source(commit_prefix, path):
    manifest_path = ROOT / "manifest.json"
    assert manifest_path.stat().st_size <= 8192
    raw = manifest_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == MANIFEST_SHA256
    manifest = json.loads(raw)
    assert manifest["version"] == 1
    matches = [row for row in manifest["artifacts"]
               if row["commit"].startswith(commit_prefix) and row["path"] == path]
    assert len(matches) == 1
    row = matches[0]
    fixture = ROOT / row["fixture"]
    assert fixture.stat().st_size == row["segment_bytes"] + 1 <= 4096
    # apply_patch's terminal newline is not part of ast.get_source_segment.
    raw = fixture.read_bytes()
    assert raw.endswith(b"\n")
    segment = raw[:-1]
    assert hashlib.sha256(segment).hexdigest() == row["verbatim_segment_sha256"]
    source = segment.decode("utf-8")
    if row["function"] == "_v2_managed_sell_owned_quantity":
        # Retain the original test's class/method AST selection unchanged.
        return "class OmsRiskService:\n    " + source
    return source

"""Retain the separate later census, including the actual linked SCKT Fill."""
import hashlib
import json
from pathlib import Path

source = Path("/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/tonight-startup-later-with-fills-own-read.json")
raw = source.read_bytes()
data = json.loads(raw)
assert (len(data["jobs"]), len(data["orders"]), len(data["deferred_intents"]), len(data["fills"])) == (14, 11, 15, 1)
result = {"source_path": str(source), "source_sha256": hashlib.sha256(raw).hexdigest(),
    "read_at": data["read_at"], "tickets": data["jobs"], "orders": data["orders"],
    "deferred_intents": data["deferred_intents"], "fills": data["fills"],
    "limits": "Read-only census as of its capture; not the whole live day. Linked recorded Fill is not a fresh venue read."}
Path("tests/fixtures/rpgstuck1_startup_later.json").write_text(json.dumps(result, indent=2) + "\n")

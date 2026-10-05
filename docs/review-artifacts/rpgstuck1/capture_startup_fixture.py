"""Extract the parent's read-only ticket census without inventing broker proof."""
import hashlib
import json
from pathlib import Path

source = Path("/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005/tonight-startup-complete-own-read.json")
raw = source.read_bytes()
data = json.loads(raw)
assert len(data["jobs"]) == 8
result = {"source_path": str(source), "source_sha256": hashlib.sha256(raw).hexdigest(),
          "read_at": data["read_at"], "tickets": data["jobs"], "orders": data["orders"],
          "deferred_intents": data["deferred_intents"],
          "limits": "Recorded database census only; no fresh strict broker detail or fill-zero proof."}
Path("tests/fixtures/rpgstuck1_startup.json").write_text(json.dumps(result, indent=2) + "\n")
broker_source = source.with_name("tonight-schwab-ticket-orders-own-read.json")
broker_raw = broker_source.read_bytes()
broker = json.loads(broker_raw)
assert len(broker["orders"]) == 4 and all(row["http_status"] == 200 for row in broker["orders"])
broker.update(source_path=str(broker_source), source_sha256=hashlib.sha256(broker_raw).hexdigest())
Path("tests/fixtures/rpgstuck1_startup_broker.json").write_text(json.dumps(broker, indent=2) + "\n")

"""Extract the four original tickets and E3 authorizations, without inventing fields."""
import hashlib
import json
from pathlib import Path

root = Path("/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005")
e1_path = root / "ownmix-rpgstuck-own-read.json"
e3_path = root / "ownmix-rpgstuck-followup.json"
e1, e3 = json.loads(e1_path.read_text()), json.loads(e3_path.read_text())
authorizations = {row["id"]: row["authorization"] for row in e3["rpg_authorizations"]}
tickets = [{**row, "payload": {**row["payload"], "authorization": authorizations[row["id"]]}}
           for row in e1["queries"]["rpg_jobs"]]
output = {
    "source": "October 5 E1 durable tickets + E3 actual authorizations; simulated future inputs must be labelled",
    "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (e1_path, e3_path)},
    "tickets": tickets,
    "intents": e1["queries"]["reprice_intents"],
}
target = Path("tests/fixtures/rpgstuck1_recorded.json")
target.write_text(json.dumps(output, indent=2) + "\n")
print(f"Extracted {len(tickets)} tickets; {target}")

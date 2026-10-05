"""Extract retained probes and the bounded later read into replay-only fixtures."""
import hashlib
import json
from pathlib import Path
import re

root = Path("/Users/velkris/.codex/ownmix-rpgstuck-evidence-20261005")
early_path = root / "ownmix-rpgstuck-own-read.json"
late_path = Path("/tmp/rpgstuck-sequence-late-sql.json")
logs_path = Path("/tmp/rpgstuck-sequence-late-logs.txt")
early = json.loads(early_path.read_text())
late = json.loads(late_path.read_text())
lines = [row["line"] for row in early["logs"] if "[V2-ATR-PROBE]" in row["line"]
         and ((row["line"].startswith("2026-10-05 13:32:") and "sym=APUS " in row["line"])
              or (row["line"].startswith("2026-10-05 13:36:") and "sym=VEEA " in row["line"]))]
lines += [line for line in logs_path.read_text().splitlines() if "[V2-ATR-PROBE]" in line]
probes = []
for line in lines:
    fields = dict(re.findall(r"(sym|ts_ms|close|high|low|trail|state|age|vol)=([^ ]+)", line))
    stamp = re.search(r"2026-10-05 \d\d:\d\d:\d\d,\d+", line).group()
    probes.append({"observed_at": stamp, "fields": fields, "raw_line": line})
output = {"evidence_sha256": {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in (early_path, late_path, logs_path)},
          "probes": probes, "late": late}
Path("tests/fixtures/rpgstuck1_sequences.json").write_text(json.dumps(output, indent=2) + "\n")
target = Path("docs/review-artifacts/rpgstuck1")
(target / "sequence-late-sql.json").write_text(late_path.read_text())
(target / "sequence-late-logs.txt").write_text(logs_path.read_text())

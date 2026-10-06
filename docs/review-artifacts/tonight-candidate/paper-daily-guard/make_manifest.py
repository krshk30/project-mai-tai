import hashlib
import json
from pathlib import Path

root = Path(__file__).parent
names = ["daily_guard.py", "guard_rules.py", "sampler.py", "failure_stop.py",
         "project-mai-tai-option-a-daily-guard.service",
         "project-mai-tai-option-a-daily-guard.timer",
         "project-mai-tai-option-a-daily-guard-failure.service"]
print(json.dumps({"application_sha": "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf",
    "authority": "operator21:07 plus direct clarification: existing bot hours; daily trading-day start",
    "artifacts": {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}},
    sort_keys=True, indent=2))

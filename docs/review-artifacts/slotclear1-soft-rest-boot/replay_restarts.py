"""All kept starts and pending software-rest observations; absence is UNMEASURED."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).parent
data = json.loads((ROOT / "restart-events.json").read_text())
pending, starts = {}, []
for event in data["events"]:
    line = event["line"]
    if "bot starting" in line:
        starts.append({"time_utc": line[:23], "pid": re.search(r"pid=(\d+)", line)[1],
            "pending_soft_rests": dict(pending), "source": event["source"],
            "line_number": event["line_number"], "restored_owners": []})
        pending = {}
    elif "[V2-FLIP-OWNER-RESTORED]" in line and starts:
        starts[-1]["restored_owners"].append(line)
    else:
        match = re.search(r"\[V2-RESTING-(EH-ARM|EH-DISARM|EH-CROSS|CANCEL)\] (\S+)", line)
        if match:
            kind, symbol = match.groups()
            if kind == "EH-ARM":
                pending[symbol] = {"time_utc": line[:23], "source": event["source"],
                    "line_number": event["line_number"], "line": line}
            else:
                pending.pop(symbol, None)
print(json.dumps({"as_of_utc": data["as_of_utc"], "starts": len(starts),
    "starts_with_pending_soft_rest": sum(bool(s["pending_soft_rests"]) for s in starts),
    "pending_symbol_start_pairs": sum(len(s["pending_soft_rests"]) for s in starts),
    "restarts": starts,
    "coverage": "Kept logs since September22; log pending is not positive no-wire proof. Older starts without dispatch begin remain UNMEASURED/owned."}, indent=2))

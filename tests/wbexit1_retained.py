"""Strict, file-only binding of the delivered native log context; no provider I/O."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re


CONTEXT = Path(__file__).parent / "fixtures/wbexit1/aiok_htco_retained_log_context.json"
LOOP_DELTA = Path(__file__).parent / "fixtures/wbexit1/aiok_loop_delta_1504_1529.jsonl"
LOOP_DELTA_SHA256 = "cc2e29d1fcb2fb2bc56479c0f532ecdbef7ef8898715088e0e2e7258328e3cfa"


@dataclass(frozen=True)
class Episode:
    symbol: str
    quantity: Decimal
    entry_price: Decimal
    target: Decimal
    stop: Decimal
    row_id: str
    entry_id: str
    entry_coid: str
    protect_base: str
    old_outcome: str
    source_rows: tuple[dict, ...]


def field(line, name):
    match = re.search(r"(?:^|\s)" + re.escape(name) + r"=([^\s]+)", line)
    if match is None:
        raise ValueError("missing retained field: " + name)
    return match[1]


def load_episode(symbol):
    if symbol not in {"AIOK", "HTCO"}:
        raise ValueError("unbound retained case")
    document = json.loads(CONTEXT.read_text())
    if document["packet_sha256"] != "f735fd7d34a44dc19d3585f259c36834faa6d11804d754b9bd9af05a510b811b":
        raise ValueError("retained packet pin changed")
    rows = [row for row in document["rows"] if any(
        group.startswith(symbol.lower() + "_") for group in row["groups"])]
    if len(rows) != 37:
        raise ValueError("retained context incomplete")
    for row in rows:
        if hashlib.sha256((row["line"] + "\n").encode()).hexdigest() != row["raw_line_sha256"]:
            raise ValueError("retained line bytes changed")
    def one(marker):
        matches = [row["line"] for row in rows
                   if marker in row["line"] and symbol in row["line"]]
        if len(matches) != 1:
            raise ValueError("ambiguous/missing retained marker: " + marker)
        return matches[0]
    opened = one("[OMS-V2-MANAGED-OPEN]")
    protected = one("[WEBULL-PROTECT-ATTACHED]")
    outcome = one("[OMS-WEBULL-CANCEL-THEN-SELL]")
    terminal = next(row["line"] for row in rows if "[WBATTACH1-CANCEL]" in row["line"]
                    and "action=confirmed terminal=PROVEN" in row["line"])
    return Episode(symbol, Decimal(field(opened, "qty")), Decimal(field(opened, "entry")),
        Decimal(field(protected, "target")), Decimal(field(protected, "stop")),
        field(outcome, "row"), field(terminal, "entry_id"), field(terminal, "entry"),
        field(terminal, "base"), field(outcome, "outcome"), tuple(rows))


def raw_time(row):
    """Raw source clock only. BEGIN proves UTC server TZ, not native arrival."""
    return datetime.strptime(row["line"][:23], "%Y-%m-%d %H:%M:%S,%f")


def validate_aiok_loop_delta(packet):
    """Validate source identity/coverage independently from NEW app acceptance."""
    if not packet or packet[0].get("kind") != "BEGIN" or packet[-1].get("kind") != "END":
        raise ValueError("loop packet missing boundaries")
    begin, end = packet[0], packet[-1]
    if (begin.get("from_inclusive") != "2026-10-23T19:04:00Z"
            or begin.get("until_exclusive") != "2026-10-23T19:29:00Z"
            or begin.get("server_tzname") != ["UTC", "UTC"]
            or begin.get("source") != "/var/log/project-mai-tai/oms.log"
            or begin.get("read_only") is not True):
        raise ValueError("loop source/window identity UNKNOWN")
    records = packet[1:-1]
    if (len(records) != end.get("returned") or len(records) != end.get("matched_context_records")
            or len(records) >= begin["max_records"] or end.get("overflow") is not False
            or end.get("source_changed_while_read") is not False
            or end.get("source_size_after") != begin["source_bytes"]):
        raise ValueError("loop capture truncated or source changed")
    seen, previous_line = set(), 0
    outcomes = []
    for row in records:
        if row.get("kind") != "LOG":
            raise ValueError("foreign loop record")
        identity = row["line_number"]
        if identity in seen or identity <= previous_line:
            raise ValueError("duplicate/noncausal physical loop line")
        seen.add(identity)
        previous_line = identity
        if hashlib.sha256((row["line"] + "\n").encode()).hexdigest() != row["raw_line_sha256"]:
            raise ValueError("loop raw line changed")
        if row["timestamp_raw"] != row["line"][:19]:
            raise ValueError("loop timestamp locator mismatch")
        if "[OMS-WEBULL-CANCEL-THEN-SELL]" not in row["line"]:
            continue
        if any(field(row["line"], name) != value for name, value in (
            ("sym", "AIOK"), ("acct", "live:orb"), ("row", "c7ad4a8d-14ba-4eea-a038-9aec301779cf"),
            ("exit", "CW_TARGET"))):
            raise ValueError("foreign loop episode")
        when = raw_time(row)
        if not datetime(2026, 10, 23, 19, 4) <= when < datetime(2026, 10, 23, 19, 29):
            raise ValueError("loop outcome outside capture window")
        if outcomes and when < raw_time(outcomes[-1]):
            raise ValueError("outcome clock/source ordering ambiguous")
        outcomes.append(row)
    return {"begin": begin, "end": end, "records": records, "loop_records": outcomes}


def load_aiok_loop_delta():
    raw = LOOP_DELTA.read_bytes()
    if hashlib.sha256(raw).hexdigest() != LOOP_DELTA_SHA256:
        raise ValueError("loop delta whole-file pin changed")
    lines = raw.decode("utf-8").splitlines()
    if any(not line.strip() for line in lines):
        raise ValueError("empty loop packet record")
    return validate_aiok_loop_delta([json.loads(line) for line in lines])

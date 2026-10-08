"""Read-only replay of actual buy fills against recorded bot entry-bar probes."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


PROBE = re.compile(
    r"\[V2-ATR-PROBE\] sym=(\S+) ts_ms=(\d+) close=(\S+).*?"
    r"trail=(\S+) state=(\S+) age=(\S+).*?flip=(\S+)"
)


def assess(fills: dict, managed: dict, text: str) -> dict:
    probes: dict[tuple[str, int], list[dict]] = {}
    source = ""
    for line in text.splitlines():
        if line.startswith("SOURCE "):
            source = line[7:]
        match = PROBE.search(line)
        if not match:
            continue
        symbol, minute, close, trail, state, age, flip = match.groups()
        probe = dict(symbol=symbol, minute=int(minute), close=close, trail=trail,
                     state=state, age=age, flip=flip, source=source, line=line)
        probes.setdefault((symbol, int(minute)), []).append(probe)
    rows = {row["entry_order_id"]: row for row in managed["rows"]}
    cases = []
    for fill in fills["rows"]:
        if fill["side"] != "buy":
            continue
        minute = int(datetime.fromisoformat(fill["filled_at"]).timestamp()) // 60 * 60000
        matches = probes.get((fill["symbol"], minute), [])
        signatures = {(p["close"], p["trail"], p["state"], p["flip"]) for p in matches}
        classification = "UNMEASURED"
        if len(signatures) == 1:
            p = matches[0]
            if p["state"] == "short" and float(p["close"]) < float(p["trail"]):
                classification = "FALSE_FLIP"
            elif p["state"] == "long":
                classification = "REAL_FLIP"
        row = rows.get(fill["order_id"], {})
        payload = fill.get("order_payload") or {}
        filled_at = datetime.fromisoformat(fill["filled_at"])
        et = filled_at.astimezone(ZoneInfo("America/New_York"))
        anchor = et.replace(hour=4, minute=0, second=0, microsecond=0)
        if et < anchor:
            anchor -= timedelta(days=1)
        end_ms = int((anchor + timedelta(days=1)).timestamp() * 1000)
        later = sorted((p for (symbol, ts), values in probes.items()
                        if symbol == fill["symbol"] and minute < ts < end_ms
                        for p in values), key=lambda p: p["minute"])
        next_sell = next((p["minute"] for p in later if p["flip"] == "SELL"), None)
        next_buy = next((p for p in later if p["flip"] == "BUY"
                         and (next_sell is None or p["minute"] < next_sell)), None)
        cases.append(dict(fill_id=fill["id"], order_id=fill["order_id"],
                          managed_row_id=row.get("id"), account=fill["account"],
                          managed_account=row.get("broker_account_name"),
                          managed_symbol=row.get("symbol"),
                          managed_entry_order_id=row.get("entry_order_id"),
                          managed_entry_client_order_id=row.get("entry_client_order_id"),
                          fill_client_order_id=fill["client_order_id"],
                          symbol=fill["symbol"], filled_at=fill["filled_at"],
                          bar_ms=minute, classification=classification,
                          slot=payload.get("fanout_slot_id"),
                          segment=payload.get("fanout_segment_id"),
                          probes=matches, next_buy=next_buy,
                          restored="UNMEASURED: proposed code not built",
                          closed_at=row.get("updated_at"), status=row.get("status")))
    slots: dict[str, list] = {}
    for case in cases:
        slots.setdefault(str(case["slot"] or case["order_id"]), []).append(case)
    return dict(as_of_utc=fills["as_of"], buy_fill_legs=len(cases),
                distinct_slots=len(slots), classification_counts=dict(Counter(
                    case["classification"] for case in cases)),
                slot_classifications=dict(Counter(
                    ",".join(sorted({c["classification"] for c in group}))
                    for group in slots.values())), cases=cases)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fills", type=Path, required=True)
    parser.add_argument("--managed", type=Path, required=True)
    parser.add_argument("--probes", type=Path, required=True)
    args = parser.parse_args()
    result = assess(json.loads(args.fills.read_text()), json.loads(args.managed.read_text()),
                    args.probes.read_text())
    result["source_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (args.fills, args.managed, args.probes)}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

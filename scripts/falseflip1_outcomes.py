"""Historical facts and bar extrema only; never infer a counterfactual broker fill."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from falseflip1_step0 import PROBE


def assess(replay, bars, fills, text):
    probes = []
    source = ""
    for line in text.splitlines():
        if line.startswith("SOURCE "):
            source = line[7:]
        match = PROBE.search(line)
        if match:
            symbol, minute, close, trail, state, age, flip = match.groups()
            probes.append(dict(symbol=symbol, minute=int(minute), close=close, trail=trail,
                               state=state, age=age, flip=flip, source=source, line=line))
    output = []
    slots = sorted({row["slot"] for row in replay["cases"] if row["classification"] == "FALSE_FLIP"})
    for slot in slots:
        cases = [row for row in replay["cases"] if row["slot"] == slot]
        first = min(cases, key=lambda row: row["filled_at"])
        entry_ms = first["bar_ms"]
        et = datetime.fromtimestamp(entry_ms / 1000, UTC).astimezone(ZoneInfo("America/New_York"))
        anchor = et.replace(hour=4, minute=0, second=0, microsecond=0)
        if et < anchor:
            anchor -= timedelta(days=1)
        start = int(anchor.timestamp() * 1000)
        end = int((anchor + timedelta(days=1)).timestamp() * 1000)
        symbol_probes = sorted((p for p in probes if p["symbol"] == first["symbol"]
                                and start <= p["minute"] < end), key=lambda p: p["minute"])
        previous = [p for p in symbol_probes if p["flip"] == "SELL" and p["minute"] < entry_ms]
        following = [p for p in symbol_probes if p["flip"] == "SELL" and p["minute"] > entry_ms]
        segment_end = following[0]["minute"] if following else end
        same_segment = [b for b in bars["bars"] if b["symbol"] == first["symbol"]
                        and entry_ms <= int(datetime.fromisoformat(b["bar_time"]).timestamp() * 1000) < segment_end]
        real_buy = first["next_buy"]
        actual_later = [f for f in fills["rows"] if f["symbol"] == first["symbol"] and f["side"] == "buy"
                        and real_buy is not None and real_buy["minute"] <= int(
                            datetime.fromisoformat(f["filled_at"]).timestamp() * 1000) < segment_end]
        bound = sum(bool(row["managed_row_id"] and row["managed_entry_order_id"] == row["order_id"]
                         and row["managed_entry_client_order_id"] == row["fill_client_order_id"]
                         and row["managed_account"] == row["account"]
                         and row["managed_symbol"] == row["symbol"]) for row in cases)
        output.append(dict(symbol=first["symbol"], slot=slot, entry_bar_ms=entry_ms,
            filled_legs=len(cases), runtime_bound_legs=bound,
            historical_restore="UNMEASURED: feature not installed",
            controlled_restore="independent episode, exact controlled bindings: normal path; not a historical fill",
            causal_sell=previous[-1] if previous else None, segment_end_ms=segment_end,
            next_real_buy_same_segment=real_buy,
            subsequent_actual_buys=[{k: f[k] for k in ("id", "order_id", "account", "filled_at", "quantity", "price")}
                                   for f in actual_later],
            stored_segment_bar_count=len(same_segment),
            stored_high=str(max((Decimal(b["high_price"]) for b in same_segment), default=Decimal("NaN"))),
            stored_low=str(min((Decimal(b["low_price"]) for b in same_segment), default=Decimal("NaN"))),
            sources=sorted({b["source"] for b in same_segment}),
            bar_extrema_are_not_fills=True))
    return {"population_false_slots": len(output), "bar_capture_asof": bars["asof"], "cases": output}


def main():
    parser = argparse.ArgumentParser()
    for name in ("replay", "bars", "fills", "probes"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    paths = vars(args)
    result = assess(json.loads(args.replay.read_text()), json.loads(args.bars.read_text()),
                    json.loads(args.fills.read_text()), args.probes.read_text())
    result["source_sha256"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths.values()}
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

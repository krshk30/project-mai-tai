"""Offline retained-detection arithmetic; never dispatches or predicts trades."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from project_mai_tai.momentum_live.admission import Refusal, Route, price_entry, select_route


def replay(raw: bytes) -> dict:
    if len(raw) > 16*1024*1024:
        raise ValueError("retained_replay_input_bound")
    document = json.loads(raw)
    signals = document["signals"]
    if not isinstance(signals, list) or len(signals) > 1000:
        raise ValueError("retained_replay_signal_bound")
    rows, arithmetic = [], Counter()
    for signal in signals:
        observed = int(signal["observed_decision_ns"])
        now = datetime.fromtimestamp(observed/1e9, UTC)
        route = select_route(now, market_open=now.weekday() < 5, is_us=None)
        candidates = []
        for key in ("historical_massive_quote_candidate", "historical_schwab_quote_candidate"):
            quote = signal.get(key)
            if quote:
                received = datetime.fromisoformat(quote["received_at"])
                event = datetime.fromisoformat(quote["event_ts"])
                if received.tzinfo is None or event.tzinfo is None:
                    continue
                stamp = int(received.timestamp()*1e9)
                if 0 <= observed-stamp <= 2_000_000_000 and event <= now:
                    candidates.append((stamp, quote))
        conditional = None
        reason = "QUOTE_ARCHIVE_MISSING_OR_STALE"
        if candidates:
            stamp, quote = max(candidates, key=lambda item: item[0])
            ask = Decimal(str(quote["ask_price"]))
            tick = Decimal(".0001") if ask < 1 else Decimal(".01")
            try:
                sized = price_entry(Decimal(str(signal["signal"]["price"])), ask,
                    quote_ns=stamp, now_ns=observed, route=Route.WEBULL, tick=tick)
                conditional = {"limit": str(sized.limit), "quantity": str(sized.quantity),
                    "cap": str(Decimal(str(signal["signal"]["price"]))*Decimal("1.015")),
                    "archive_provider": quote.get("provider"), "archive_received_ns": stamp,
                    "scope": "ARCHIVE_RECEIVED_AGE_PROXY_NOT_ACTUAL_OMS_BOOK"}
                reason = "CONDITIONAL_CAP_PASS"
            except Refusal as exc:
                reason = str(exc)
        arithmetic[reason] += 1
        rows.append({"logical_id": signal["logical_id"], "symbol": signal["symbol"],
            "observed_decision_ns": observed,
            "historical_country_known_at_decision": "UNKNOWN_NOT_RETAINED",
            "unknown_country_policy_route_only": route.route.value,
            "actual_historical_live_route": "NOT_RUN",
            "since04_volume": "UNKNOWN", "actual_OMS_book": "UNKNOWN_NOT_RETAINED",
            "entry_decision": "SKIP_UNPROVEN_VOLUME_AND_OMS_BOOK", "wire_count": 0,
            "conditional_arithmetic": conditional, "conditional_arithmetic_reason": reason})
    return {"scope": "OFFLINE_RETAINED_DETECTIONS_NO_ORDERS_NO_PNL_PREDICTION",
        "input_sha256": hashlib.sha256(raw).hexdigest(), "denominator": len(rows),
        "names": len({row["symbol"] for row in rows}), "actual_wire_count": 0,
        "unknown_volume_count": len(rows), "unknown_book_count": len(rows),
        "unknown_historical_country_count": len(rows),
        "formatter": "ACTUAL_MOMLIVE_PRICE_ENTRY_ROUND_CEILING",
        "conditional_arithmetic_counts": dict(arithmetic), "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retained-input", type=Path, required=True)
    args = parser.parse_args()
    if args.retained_input.stat().st_size > 16*1024*1024:
        raise ValueError("retained_replay_input_bound")
    print(json.dumps(replay(args.retained_input.read_bytes()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

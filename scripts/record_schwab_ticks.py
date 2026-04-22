from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from project_mai_tai.market_data.models import QuoteTickRecord, TradeTickRecord
from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.settings import Settings


def _load_env_file(path_text: str) -> None:
    path = Path(path_text)
    if not path.exists():
        raise SystemExit(f"Env file not found: {path}")
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        import os

        os.environ[key.strip()] = value.strip()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record raw Schwab quote/trade ticks into an ordered JSONL capture for later replay.",
    )
    parser.add_argument("symbols", nargs="+", help="Ticker symbols to record.")
    parser.add_argument("--duration", type=float, default=30.0, help="How long to record, in seconds.")
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="JSONL output path. One event per line.",
    )
    parser.add_argument(
        "--env-file",
        default="",
        help="Optional KEY=VALUE env file for Schwab credentials/settings.",
    )
    return parser


async def _run_capture(
    *,
    symbols: list[str],
    duration_seconds: float,
    output_path: Path,
) -> dict[str, Any]:
    settings = Settings()
    client = SchwabStreamerClient(settings)
    loop = asyncio.get_running_loop()
    events: list[dict[str, Any]] = []
    arrival_seq = 0

    def _append_event(event_type: str, payload: QuoteTickRecord | TradeTickRecord) -> None:
        nonlocal arrival_seq
        arrival_seq += 1
        event = {
            "event_type": event_type,
            "arrival_seq": arrival_seq,
            "recorded_at_ns": time.time_ns(),
            "loop_time": loop.time(),
            **asdict(payload),
        }
        events.append(event)

    await client.start(
        on_trade=lambda record: _append_event("trade", record),
        on_quote=lambda record: _append_event("quote", record),
    )
    try:
        await client.sync_subscriptions(symbols)
        await asyncio.sleep(max(1.0, float(duration_seconds)))
    finally:
        await client.stop()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    quote_count = sum(1 for event in events if event["event_type"] == "quote")
    trade_count = sum(1 for event in events if event["event_type"] == "trade")
    return {
        "output": str(output_path),
        "symbols": symbols,
        "duration_seconds": float(duration_seconds),
        "event_count": len(events),
        "quote_count": quote_count,
        "trade_count": trade_count,
        "first_event": events[0] if events else None,
        "last_event": events[-1] if events else None,
    }


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.env_file.strip():
        _load_env_file(args.env_file)
    result = asyncio.run(
        _run_capture(
            symbols=[str(symbol).upper() for symbol in args.symbols if str(symbol).strip()],
            duration_seconds=float(args.duration),
            output_path=args.output,
        )
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

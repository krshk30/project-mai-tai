from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict

from project_mai_tai.market_data.schwab_streamer import SchwabStreamerClient
from project_mai_tai.settings import Settings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Probe the Schwab streamer using existing MAI_TAI Schwab credentials.",
    )
    parser.add_argument(
        "symbols",
        nargs="+",
        help="Ticker symbols to subscribe during the probe.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=10.0,
        help="Seconds to listen after subscribe. Default: 10.",
    )
    parser.add_argument(
        "--sample-limit",
        type=int,
        default=5,
        help="How many quote/trade samples to retain in the JSON output. Default: 5.",
    )
    return parser


async def _run_probe(symbols: list[str], duration: float, sample_limit: int) -> int:
    settings = Settings()
    client = SchwabStreamerClient(settings)
    result = await client.probe(
        symbols=symbols,
        duration_seconds=duration,
        sample_limit=sample_limit,
    )
    print(json.dumps(asdict(result), indent=2, sort_keys=True))
    return 0 if result.ok else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(
        _run_probe(
            symbols=[str(symbol).upper() for symbol in args.symbols],
            duration=float(args.duration),
            sample_limit=max(1, int(args.sample_limit)),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())

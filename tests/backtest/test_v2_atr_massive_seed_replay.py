from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from project_mai_tai.backtest.data import CapturedBar, SchwabBar
from project_mai_tai.backtest.replay import build_replay_settings, replay_symbol_day


class _MedsSource:
    def __init__(self) -> None:
        fixture = json.loads(
            (Path(__file__).parent / "fixtures" / "MEDS_20260916_atr_seed.json").read_text()
        )
        self.schwab = [
            SchwabBar(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), int(r[5]))
            for r in fixture["schwab"]
        ]
        self.massive = [
            CapturedBar(
                datetime.fromtimestamp(int(r[0]) / 1000.0, UTC),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]),
            )
            for r in fixture["massive"]
        ]

    def schwab_bars(self, symbol, start, end):
        lo = int(start.timestamp() * 1000)
        hi = int(end.timestamp() * 1000)
        return [bar for bar in self.schwab if lo <= bar.ts < hi]

    def massive_bars(self, symbol, start, end):
        return [bar for bar in self.massive if start <= bar.ts < end]

    def schwab_quotes(self, symbol, start, end):
        return []

    def trades(self, symbol, start, end):
        return []


def test_replay_flag_on_reproduces_meds_pair_and_flag_off_does_not() -> None:
    source = _MedsSource()
    enabled = replay_symbol_day(
        source,
        "MEDS",
        "2026-09-16",
        build_replay_settings(
            strategy_schwab_1m_v2_atr_massive_seed_enabled=True,
        ),
    )
    disabled = replay_symbol_day(
        source,
        "MEDS",
        "2026-09-16",
        build_replay_settings(
            strategy_schwab_1m_v2_atr_massive_seed_enabled=False,
        ),
    )

    pair = ((1_789_557_900_000, "SELL"), (1_789_562_040_000, "BUY"))
    assert enabled.atr_seed_outcome == "SEEDED"
    assert enabled.atr_seed_bars == 180
    assert pair[0] in enabled.atr_flips_observed
    assert pair[1] in enabled.atr_flips_observed
    assert pair[0] not in disabled.atr_flips_observed
    assert pair[1] not in disabled.atr_flips_observed
    assert disabled.atr_seed_outcome == "DISABLED"

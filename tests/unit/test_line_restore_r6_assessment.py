"""Recorded counterexample assessment; does not enable or implement R6 admission."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from project_mai_tai.backtest.atr_oracle import Bar, compute_atr_trail
from project_mai_tai.strategy_core.schwab_1m_v2 import session_start_ts_ms
from project_mai_tai.strategy_core.session_line_restore import (
    HistoryBar, RebuildInput, build_session_line,
)

RAW = json.loads((Path(__file__).parents[1] / "fixtures/line_restore_r6_pmi_20261002.json").read_text())


@pytest.mark.parametrize("through,code_state,code_trail,oracle_state,oracle_trail", [
    ("2026-10-02T20:45:00+00:00", "short", 5.852338786738176, "long", 5.7027),
    ("2026-10-02T21:15:00+00:00", "long", 5.727122541190053, "short", 5.8432),
])
def test_recorded_pmi_clamp_can_change_colour_not_only_trail(
    through, code_state, code_trail, oracle_state, oracle_trail,
):
    cutoff = datetime.fromisoformat(through)
    rows = [dict(zip(RAW["columns"], row)) for row in RAW["rows"]
            if datetime.fromisoformat(row[0]) <= cutoff]
    bars = tuple(HistoryBar(
        "PMI", *(float(row[key]) for key in ("open_price", "high_price", "low_price", "close_price")),
        int(row["volume"]), int(datetime.fromisoformat(row["bar_time"]).timestamp() * 1000),
    ) for row in rows)
    request = RebuildInput("PMI", 1, 1, session_start_ts_ms(bars[-1].timestamp_ms),
                           bars[-1].timestamp_ms, bars)
    indicator = dict(build_session_line(request, 5, 3.5).indicator)
    oracle = compute_atr_trail([Bar(b.timestamp_ms, b.open, b.high, b.low, b.close, b.volume)
                               for b in bars])[-1]
    assert indicator["atr_state"] == code_state
    assert indicator["atr_trail"] == pytest.approx(code_trail, abs=1e-10)
    assert oracle["state"] == oracle_state and oracle["trail"] == oracle_trail
    assert indicator["atr_state"] != oracle["state"]


def test_recorded_pmi_plus10_is_not_ten_contiguous_live_bars():
    event = datetime.fromisoformat(RAW["event_utc"])
    after = [datetime.fromisoformat(row[0]) for row in RAW["rows"]
             if not (datetime.fromisoformat(row[7]) <= event
                     and datetime.fromisoformat(row[0]).timestamp() + 60 <= event.timestamp())]
    assert len(after) == 11  # Runner's first bar + ten subsequent observed live bars.
    suffix = 1
    for left, right in reversed(list(zip(after, after[1:]))):
        if (right - left).total_seconds() != 60:
            break
        suffix += 1
    assert suffix == 1

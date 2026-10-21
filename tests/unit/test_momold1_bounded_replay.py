"""SYNTH harness-input controls; none attest the retained 10-20 export."""

import csv
import gzip
from pathlib import Path

import pytest

from scripts.momold1_bounded_replay import START_NS, read_events, spool
from tests.unit.test_momentum_paper_service import _condition_snapshot


def write_trades(root: Path, rows):
    with gzip.open(root / "trades_1020_massive.csv.gz", "wt", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[
            "id", "provider", "symbol", "event_ts", "received_at", "price", "size",
            "exchange", "conditions", "cumulative_volume",
        ])
        writer.writeheader()
        for index, (symbol, stamp, price) in enumerate(rows):
            writer.writerow(dict(id=index, provider="massive", symbol=symbol,
                event_ts=stamp, received_at=stamp, price=price, size=100, exchange=1,
                conditions="1", cumulative_volume=""))


def test_synth_spool_keeps_timestamp_ties_and_bar_bounds_without_empty_bars(tmp_path):
    write_trades(tmp_path, [
        ("TEST", "2026-10-20T08:00:00.100000+00:00", "10"),
        ("TEST", "2026-10-20T08:00:00.100000+00:00", "11"),
        ("TEST", "2026-10-20T08:00:02+00:00", "12"),
    ])
    paths, census = spool(tmp_path, tmp_path, _condition_snapshot(), {"TEST"})
    rows = list(read_events(paths["TEST"]))
    assert census["trades"] == 3 and census["event_ts_ties"] == 1
    assert [row[4]["price"] for row in rows if row[3] == "trade"] == ["10", "11", "12"]
    bars = [row[4] for row in rows if row[3] == "aggregate"]
    assert [row["start_ms"] for row in bars] == [START_NS // 1_000_000, START_NS // 1_000_000 + 2000]
    assert (bars[0]["open"], bars[0]["close"], bars[0]["low"], bars[0]["high"]) == ("10", "11", "10", "11")
    assert rows == sorted(rows, key=lambda row: (row[0], row[1], row[2]))


@pytest.mark.parametrize("rows,reason", [
    ([("TEST", "2026-10-20T08:00:02+00:00", "10"),
      ("TEST", "2026-10-20T08:00:01+00:00", "11")], "regressed"),
    ([("TEST", "2026-10-21T00:00:00+00:00", "10")], "outside bounded"),
    ([("OTHER", "2026-10-20T08:00:00+00:00", "10")], "outside supplied"),
])
def test_synth_spool_refuses_unreviewed_input_shape(tmp_path, rows, reason):
    write_trades(tmp_path, rows)
    with pytest.raises(ValueError, match=reason):
        spool(tmp_path, tmp_path, _condition_snapshot(), {"TEST"})

"""SYNTH harness-input controls; none attest the retained 10-20 export."""

import csv
import gzip
import io
import json
from pathlib import Path

import pytest

from scripts.momold1_bounded_replay import (
    CSV_MAX_FIELD_BYTES, START_NS, bounded_csv_reader, read_events, spool,
)
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


def csv_payload(value):
    handle = io.StringIO(newline="")
    writer = csv.writer(handle)
    writer.writerow(["payload"])
    writer.writerow([value])
    handle.seek(0)
    return handle


def test_synth_large_session_json_parses_above_default_limit_and_restores_setting():
    payload = json.dumps({"SYNTH_SESSION_READY": "x" * 200_000})
    original = csv.field_size_limit(131072)
    try:
        with bounded_csv_reader(csv_payload(payload)) as reader:
            rows = list(reader)
        assert json.loads(rows[0]["payload"])["SYNTH_SESSION_READY"] == "x" * 200_000
        assert csv.field_size_limit() == 131072
    finally:
        csv.field_size_limit(original)


def test_synth_exact_eight_mib_field_is_accepted():
    assert CSV_MAX_FIELD_BYTES == 8 * 1024 * 1024
    with bounded_csv_reader(csv_payload("x" * CSV_MAX_FIELD_BYTES)) as reader:
        rows = list(reader)
    assert len(rows[0]["payload"].encode("utf-8")) == CSV_MAX_FIELD_BYTES


@pytest.mark.parametrize("value", ["x", "\u00e9"])
def test_synth_over_limit_field_is_refused_without_skipping_and_limit_is_restored(value):
    repetitions = CSV_MAX_FIELD_BYTES // len(value.encode("utf-8")) + 1
    original = csv.field_size_limit(131072)
    try:
        with pytest.raises(csv.Error):
            with bounded_csv_reader(csv_payload(value * repetitions)) as reader:
                list(reader)
        assert csv.field_size_limit() == 131072
    finally:
        csv.field_size_limit(original)


@pytest.mark.parametrize("contents", ['payload\n"unterminated', "a,b\nvalue\n", "a\nvalue,extra\n"])
def test_malformed_csv_is_refused_without_row_skipping(contents):
    with pytest.raises(csv.Error):
        with bounded_csv_reader(io.StringIO(contents)) as reader:
            list(reader)

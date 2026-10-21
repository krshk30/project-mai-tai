"""SYNTHETIC input-contract controls, never actual incident acceptance."""

import csv
import io

import pytest

from tools.mombuffer1_export_inputs import QUOTE_HEADERS, SIGNAL_HEADERS, read_csv
from tools.mombuffer1_replay import MissingEvidence, bind_controlled_packet


def test_export_source_pointer_counts_csv_records_not_multiline_log_lines(tmp_path):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=SIGNAL_HEADERS)
    writer.writeheader()
    writer.writerow({"row_kind": "LOG", "source_line": "SYNTHETIC first\nsecond"})
    writer.writerow({"row_kind": "SIGNAL", "signal_logical_id": "SYNTHETIC-id"})
    path = tmp_path / "synthetic.csv"
    path.write_text(stream.getvalue())
    rows = read_csv(path, SIGNAL_HEADERS)
    assert [r["source_pointer"]["csv_record"] for r in rows] == [1, 2]
    assert rows[0]["raw"]["source_line"] == "SYNTHETIC first\nsecond"


def test_quote_header_never_aliases_sip_to_received_at():
    assert "sip_timestamp_ns" in QUOTE_HEADERS
    assert "participant_timestamp_ns" in QUOTE_HEADERS
    assert "received_at" not in QUOTE_HEADERS


@pytest.mark.parametrize("header", ["ask,bid", "received_at,ask", "episode_id,ask"])
def test_export_header_drift_fails_closed(tmp_path, header):
    path = tmp_path / "synthetic.csv"
    path.write_text(header + "\n")
    with pytest.raises(ValueError, match="headers changed"):
        read_csv(path, QUOTE_HEADERS)


def test_controlled_clock_requires_explicit_protocol_and_review():
    with pytest.raises(MissingEvidence, match="protocol/reviewer"):
        bind_controlled_packet({}, [], [])

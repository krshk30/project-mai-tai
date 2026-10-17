from __future__ import annotations

import ast
import csv
import io
import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momwarn_measurement_probes import BOUND, LocalProbe, NewErrorProbe
from project_mai_tai.momwarn_measurements import (
    DETAIL_FIELDS,
    MAX_BYTES,
    MAX_READ,
    NS,
    SAMPLE_FIELDS,
    STEP_NS,
    UNKNOWN,
    MeasurementCollector,
    ReadOnlyMetricCursor,
    replay,
    session_window,
    write_csv,
)

DAY = date(2026, 10, 13)
START, END = session_window(DAY)


def event(kind="gateway_sample", at=START, **fields):
    return dict(version=1, source="controlled-gateway", event_type=kind, observed_ns=at,
                gateway_instance_id="fake-instance", generation=1, **fields)


def gateway(at=START, **fields):
    defaults = dict(A_received=100, A_published=99, queue_drops=1, dropped_total=1,
                    lag_ms=10, coverage_status="provider_event_observed",
                    A_subscription_requested_ns=START - NS, A_first_provider_event_ns=START,
                    A_subscription_confirmed=None, owned_warning_symbols=["FAKE"])
    defaults.update(fields)
    return event(at=at, **defaults)


def decision(at=START + NS, **fields):
    defaults = dict(symbol="FAKE", window_seconds=30, ledger="momentum_30s_warn",
                    lineage_id="warn-one", comparison_id="episode-one", status="paper_filled",
                    cap_status="admitted", warning_obs_ns=START,
                    observeddecision_ns=at - 100_000_000, signal_basis="raw_exact",
                    signal_sip_ns=at - 150_000_000, entry_ns=at, entry_price="13.40",
                    outcome="open", clock_status="verified", clock_skew_bound_ns=1_000_000)
    defaults.update(fields)
    return event("decision", at, **defaults)


def feed(collector, payload, name=None, read_ns=None):
    return collector.record(payload, read_ns=read_ns or payload["observed_ns"],
                            stream_id=name or str(payload["observed_ns"]))


def test_actual_et_window_and_stable_ten_second_empty_format():
    rows, entries = replay(io.StringIO(""), DAY, record_kind="CONTROLLED")
    assert len(rows) == 1915
    assert rows[0]["sample_et"] == "2026-10-13T04:11:00-04:00"
    assert rows[-1]["sample_et"] == "2026-10-13T09:30:00-04:00"
    assert all(b["sample_ns"] - a["sample_ns"] == STEP_NS for a, b in zip(rows, rows[1:]))
    assert all(tuple(row) == SAMPLE_FIELDS for row in rows)
    assert all(row["A_received"] == row["sip_decision_p99_ms"] == UNKNOWN for row in rows)
    assert not entries


@pytest.mark.parametrize("now", [START - NS, END + NS, START + NS])
def test_time_fence_and_grid(now):
    with pytest.raises(ValueError):
        MeasurementCollector(DAY).sample(now, read_status="OK")


def test_same_epoch_counter_mean_observed_bins_no_zero_padding_and_real_zero():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway())
    first = collector.sample(START, read_status="OK")
    assert first["A_mean_per_s"] == UNKNOWN
    bins = [[START // NS, 100], [START // NS + 5, 300]]
    feed(collector, gateway(START + STEP_NS, A_received=2100, dropped_total=3,
                            A_rate_bins=bins, lag_ms=0))
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["A_mean_per_s"] == 200
    assert row["A_counter_interval_s"] == 10
    assert row["drop_delta"] == 2
    assert row["A_bin_p95_per_s"] == row["A_bin_peak_per_s"] == 300
    assert row["A_bin_coverage"] == "observed:2/10"
    assert row["lag_ms"] == 0
    assert row["A_subscription_confirmed"] == UNKNOWN
    assert row["stream_retention_coverage"] == UNKNOWN


@pytest.mark.parametrize("change", [{"generation": 2}, {"gateway_instance_id": "restart"},
                                     {"A_received": 10}, {"A_received": None}])
def test_counter_reset_or_missing_is_unknown(change):
    collector = MeasurementCollector(DAY)
    feed(collector, gateway())
    collector.sample(START, read_status="OK")
    changed = gateway(START + STEP_NS)
    changed.update(change)
    feed(collector, changed)
    assert collector.sample(START + STEP_NS, read_status="OK")["A_mean_per_s"] == UNKNOWN


def test_freshness_boundary_read_failure_and_binding():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway())
    feed(collector, event("consumer_sample", warning_count=2, subscribed_count=1), "consumer")
    row = collector.sample(START + 20 * NS, read_status="OK")
    assert row["gateway_age_ms"] == 20000 and row["warning_count"] == 2
    row = collector.sample(START + 30 * NS, read_status="OK")
    assert row["A_received"] == row["warning_count"] == UNKNOWN
    feed(collector, gateway(START + 40 * NS))
    wrong = event("consumer_sample", START + 40 * NS, warning_count=3)
    wrong["generation"] = 2
    feed(collector, wrong, "wrong")
    assert collector.sample(START + 40 * NS, read_status="OK")["warning_count"] == UNKNOWN
    feed(collector, gateway(START + 50 * NS))
    assert collector.sample(START + 50 * NS, read_status="UNKNOWN:read_failure")["lag_ms"] == UNKNOWN


@pytest.mark.parametrize("patch", [{"version": True}, {"version": 2}, {"generation": True},
                                    {"observed_ns": START + NS}, {"source": None},
                                    {"lag_ms": float("nan")}, {"event_type": "aggregate"}])
def test_invalid_version_skew_and_nonfinite_records(patch):
    collector = MeasurementCollector(DAY)
    item = gateway()
    item.update(patch)
    assert not collector.record(item, read_ns=START, stream_id="1-0")
    assert collector.invalid == 1 and not collector.latest


def test_oversize_and_duplicate_records_are_bounded():
    collector = MeasurementCollector(DAY, capacity=2)
    assert not feed(collector, gateway(extra="x" * MAX_BYTES))
    for offset in range(4):
        item = gateway(START + offset)
        assert feed(collector, item)
        assert not feed(collector, item)
    assert len(collector.seen) == 2


@pytest.mark.parametrize("bins", [[[START // NS, -1]], [[START // NS, True]],
                                  [[START // NS, 1], [START // NS, 2]]])
def test_invalid_rate_bins_never_appear_as_valid_zero(bins):
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(A_rate_bins=bins))
    row = collector.sample(START, read_status="OK")
    assert row["A_bin_coverage"] == "INVALID"
    assert row["A_bin_p95_per_s"] == UNKNOWN


def test_raw_exact_latency_percentiles_lead_and_unknown_clock():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(START + STEP_NS))
    feed(collector, decision())
    feed(collector, decision(START + 2 * NS, lineage_id="warn-two", comparison_id="ep-two",
                             clock_status="UNKNOWN"))
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["warning_lead_p50_ms"] == 900
    assert row["warning_lead_p95_ms"] == 1900
    assert row["qualified_sip_count"] == 1 and row["decision_count"] == 2
    assert row["sip_decision_p50_ms"] == row["sip_decision_p95_ms"] == row["sip_decision_p99_ms"] == 50
    rows = collector.comparison_rows()
    assert all(row["warning_sip_latency_raw_ms"] == 50 for row in rows)


def test_decision_after_observation_but_before_read_keeps_real_capture_time():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(START + STEP_NS))
    item = decision(observeddecision_ns=START + NS + 20_000_000,
                    signal_sip_ns=START + NS - 20_000_000)
    feed(collector, item, read_ns=START + 2 * NS)
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["sip_decision_p99_ms"] == 40
    assert collector.comparison_rows()[0]["warning_observeddecision_ns"] == START + NS + 20_000_000


@pytest.mark.parametrize("changes", [{"clock_skew_bound_ns": None},
                                     {"clock_skew_bound_ns": 100_000_000},
                                     {"signal_sip_ns": START + 2 * NS},
                                     {"observeddecision_ns": START - NS}])
def test_unqualified_or_bad_clock_never_becomes_exact_latency(changes):
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(START + STEP_NS))
    feed(collector, decision(**changes))
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["sip_decision_p50_ms"] == UNKNOWN


def test_bucket_evidence_bounds_never_invent_sip_low_or_exact_latency():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(START + STEP_NS))
    feed(collector, decision(START + 2 * NS, signal_basis="bucket_sufficient",
                             signal_sip_ns=None, signal_sip_start_ns=START,
                             signal_sip_end_ns=START + NS))
    row = collector.sample(START + STEP_NS, read_status="OK")
    detail = collector.comparison_rows()[0]
    assert row["bucket_signal_count"] == 1 and row["sip_decision_p50_ms"] == UNKNOWN
    assert detail["warning_signal_sip_ns"] == detail["warning_sip_latency_raw_ms"] == UNKNOWN
    assert detail["warning_sip_latency_lower_ms"] == 900
    assert detail["warning_sip_latency_upper_ms"] == 1900
    assert "SIP_low_at" not in detail


def test_per_stock_both_entries_outcomes_lineage_and_no_summation():
    collector = MeasurementCollector(DAY)
    feed(collector, decision(outcome="target", exit_price="14.20", pnl="0.80"))
    item = decision(START + 2 * NS, ledger="momentum_30s", lineage_id="snap-one",
                    entry_price="13.60", outcome="stop", pnl="-2.04")
    item["event_type"] = "snapshot_entry"
    feed(collector, item)
    row = collector.comparison_rows()[0]
    assert row["pair_status"] == "PAIRED" and row["symbol"] == "FAKE"
    assert row["warning_entry_price"] == "13.40" and row["snapshot_entry_price"] == "13.60"
    assert row["warning_outcome"] == "target" and row["snapshot_outcome"] == "stop"
    assert row["warning_lineage_id"] == "warn-one" and row["snapshot_lineage_id"] == "snap-one"
    assert "total_pnl" not in row


def test_no_implicit_episode_join_no_cross_generation_no_collision():
    collector = MeasurementCollector(DAY)
    feed(collector, decision(comparison_id=None))
    feed(collector, decision(START + 2 * NS, comparison_id=None, ledger="momentum_30s"))
    assert len(collector.comparison_rows()) == 2
    assert all(row["pair_status"] == "COUNTERPART_UNKNOWN" for row in collector.comparison_rows())
    feed(collector, decision(START + 3 * NS))
    assert not feed(collector, decision(START + 4 * NS, lineage_id="collision"))
    later = decision(START + 5 * NS, ledger="momentum_30s")
    later["generation"] = 2
    feed(collector, later)
    assert all(row["pair_status"] == "COUNTERPART_UNKNOWN" for row in collector.comparison_rows())


def test_bounded_decisions_comparisons_and_late_events_report_loss():
    collector = MeasurementCollector(DAY, capacity=2, comparisons=2)
    collector.sample(START + STEP_NS, read_status="OK")
    for offset in range(1, 5):
        feed(collector, decision(START + offset * NS, lineage_id=str(offset),
                                 comparison_id=str(offset)))
    assert len(collector.decisions) == len(collector.details) == 2
    assert collector.evictions == collector.comparison_evictions == 2
    assert collector.late == 4


def test_lifecycle_update_does_not_count_same_causal_decision_twice():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(START + STEP_NS))
    original = decision()
    feed(collector, original)
    update = dict(original, observed_ns=START + 2 * NS, outcome="target")
    feed(collector, update)
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["decision_count"] == row["qualified_sip_count"] == 1
    assert collector.comparison_rows()[0]["warning_outcome"] == "target"


def test_w3_revert_uses_only_gateway_receipt_and_does_not_derive_control():
    collector = MeasurementCollector(DAY)
    feed(collector, gateway(lag_ms=4000))
    assert collector.sample(START, read_status="OK")["watchdog_revert_reason"] == UNKNOWN
    receipt = event("revert", START + NS, watchdog_revert_reason="publish_lag",
                    watchdog_revert_ns=START + NS, coverage_status="reverted")
    feed(collector, receipt)
    row = collector.sample(START + STEP_NS, read_status="OK")
    assert row["watchdog_revert_reason"] == "publish_lag"
    assert row["watchdog_revert_ns"] == START + NS
    assert collector.sample(START + 40 * NS, read_status="OK")["revert_observed_ns"] == START + NS


@pytest.mark.asyncio
async def test_injected_redis_dedicated_nonblocking_read_and_failure():
    client = type("Fake", (), {"xread": AsyncMock(return_value=[
        (b"mai_tai:momentum-warning-metrics", [(b"1-0", {b"data": json.dumps(gateway())})])
    ])})()
    cursor, collector = ReadOnlyMetricCursor("mai_tai", "0-0"), MeasurementCollector(DAY)
    assert await cursor.read_once(client, collector, now_ns=START) == "OK"
    client.xread.assert_awaited_once_with({"mai_tai:momentum-warning-metrics": "0-0"}, count=MAX_READ)
    assert cursor.cursor == "1-0"
    client.xread.side_effect = OSError("controlled read fail")
    assert await cursor.read_once(client, collector, now_ns=START) == "UNKNOWN:read_failure:OSError"
    assert cursor.cursor == "1-0"


@pytest.mark.asyncio
async def test_redis_read_bound_invalid_and_timeout_receipts():
    client = type("Fake", (), {"xread": AsyncMock(return_value=[
        ("p:momentum-warning-metrics", [(f"{i}-0", {"data": json.dumps(gateway())})
                                       for i in range(MAX_READ)])
    ])})()
    cursor, collector = ReadOnlyMetricCursor("p", "0-0"), MeasurementCollector(DAY)
    assert await cursor.read_once(client, collector, now_ns=START) == "UNKNOWN:read_bound"
    client.xread.return_value = [("p:momentum-warning-metrics", [("bad-0", {"data": "bad"})])]
    assert await cursor.read_once(client, collector, now_ns=START) == "UNKNOWN:invalid_record"
    client.xread.side_effect = TimeoutError("controlled")
    assert await cursor.read_once(client, collector, now_ns=START) == "UNKNOWN:read_failure:TimeoutError"


@pytest.mark.asyncio
async def test_wrong_stream_refused_no_implicit_tail_cursor():
    with pytest.raises(ValueError):
        ReadOnlyMetricCursor("p", "$")
    client = type("Fake", (), {"xread": AsyncMock(return_value=[("p:market-data", [])])})()
    cursor = ReadOnlyMetricCursor("p", "0-0")
    status = await cursor.read_once(client, MeasurementCollector(DAY), now_ns=START)
    assert status == "UNKNOWN:read_failure:ValueError" and cursor.cursor == "0-0"


def test_current_g2_draft_missing_comparison_fields_stays_explicitly_unknown():
    collector = MeasurementCollector(DAY)
    draft = event("decision", START + NS, symbol="FAKE", observed_decision_ns=START + NS,
                  signal_sip_ns=START, strategy_code="momentum_30s_warn", logical_id="id",
                  lineage="raw_observed", status="FILLED", warning_obs_ns=START)
    assert not feed(collector, draft)
    assert collector.invalid == 1 and not collector.details


def test_replay_csv_roundtrip_and_missing_reads_not_carried_as_success():
    packets = [dict(read_ns=START, stream_id="1-0", data=gateway()),
               dict(read_ns=START + STEP_NS, stream_id="2-0", data=gateway(START + STEP_NS)),
               dict(read_ns=START + STEP_NS, stream_id="3-0", data=decision())]
    rows, details = replay(io.StringIO("\n".join(json.dumps(item) for item in packets)),
                           DAY, record_kind="CONTROLLED")
    assert rows[1]["A_mean_per_s"] == 0
    assert rows[2]["A_received"] == UNKNOWN and rows[2]["read_status"] == "UNKNOWN:no_read"
    for data, fields in ((rows, SAMPLE_FIELDS), (details, DETAIL_FIELDS)):
        output = io.StringIO()
        write_csv(output, data, fields)
        back = list(csv.DictReader(io.StringIO(output.getvalue())))
        assert len(back) == len(data) and list(back[0]) == list(fields)
        assert back[0]["record_kind"] == "CONTROLLED"
    assert json.loads(list(csv.DictReader(io.StringIO(output.getvalue())))[0]["warning_entry_price"]
                      .replace("UNKNOWN", "null")) == 13.40


def test_replay_out_of_order_unknown_kind_and_outside_entries():
    with pytest.raises(ValueError):
        replay(io.StringIO(json.dumps(dict(read_ns=START + NS)) + "\n" +
                           json.dumps(dict(read_ns=START))), DAY, record_kind="CONTROLLED")
    with pytest.raises(ValueError):
        replay(io.StringIO(""), DAY, record_kind="assumed-live")
    collector = MeasurementCollector(DAY)
    feed(collector, decision(END + NS))
    assert not collector.details


def test_cli_offline_only_csv_and_exclusive_outputs(tmp_path):
    root = Path(__file__).parents[2]
    source, samples, entries = [tmp_path / name for name in ("fake.jsonl", "ten.csv", "entry.csv")]
    source.write_text(json.dumps(dict(read_ns=START, stream_id="1-0", data=gateway())))
    command = [sys.executable, str(root / "scripts/momwarn1_measurements.py"), str(source),
               "--day", "2026-10-13", "--evidence-kind", "CONTROLLED",
               "--samples", str(samples), "--entries", str(entries)]
    env = {**os.environ, "PYTHONPATH": str(root / "src")}
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    with samples.open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1915 and rows[0]["record_kind"] == "CONTROLLED"
    preserved = samples.read_bytes()
    result = subprocess.run(command, cwd=root, env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2 and samples.read_bytes() == preserved


def test_retained_six_line_controlled_records_not_recorded_fills():
    fixture = Path(__file__).parents[1] / "fixtures/momwarn1_measurements_controlled.jsonl"
    with fixture.open() as handle:
        rows, details = replay(handle, DAY, record_kind="CONTROLLED")
    assert rows[0]["A_mean_per_s"] == rows[1]["A_mean_per_s"] == 100
    assert rows[1]["A_bin_p95_per_s"] == rows[1]["A_bin_peak_per_s"] == 120
    assert rows[1]["drop_delta"] == 1 and rows[1]["gateway_cpu_pct"] == 10
    assert rows[1]["sip_decision_p99_ms"] == 100
    assert rows[1]["warning_lead_p50_ms"] == 9100
    assert details[0]["warning_entry_price"] == "1.25"
    assert details[0]["snapshot_entry_price"] == "1.27"
    assert details[0]["record_kind"] == "CONTROLLED"


def test_new_error_only_after_baseline_partial_line_rotation_bounds_and_failure(tmp_path):
    path = tmp_path / "oms.log"
    path.write_text("old ERROR old\n")
    probe = NewErrorProbe([path])
    assert probe.sample()[0] == UNKNOWN
    with path.open("a") as handle:
        handle.write("2026-10-13 08:11:00,000 INFO [oms] normal\n"
                     "2026-10-13 08:11:00,000 ERROR [oms] new\n"
                     "2026-10-13 08:11:00,000 ERR")
    assert probe.sample() == (1, "OK")
    with path.open("a") as handle:
        handle.write("OR second\n")
    assert probe.sample() == (1, "OK")
    assert probe.sample() == (0, "OK")
    path.write_text("rotated\n")
    assert probe.sample()[0] == UNKNOWN
    with path.open("a") as handle:
        handle.write("x" * (BOUND + 1))
    assert probe.sample() == (UNKNOWN, "UNKNOWN:log_bound")
    path.unlink()
    assert "log_read" in probe.sample()[1]


def test_info_message_containing_error_word_is_not_new_error(tmp_path):
    path = tmp_path / "oms.log"
    path.write_text("")
    probe = NewErrorProbe([path])
    probe.sample()
    path.write_text("2026-10-13 08:11:00,000 INFO [oms] previous ERROR was cleared\n")
    assert probe.sample() == (0, "OK")


def fake_proc(root, ticks, start=100, idle=800, total_active=200):
    process = root / "12"
    process.mkdir(exist_ok=True)
    fields = ["0"] * 20
    fields[0], fields[11], fields[12], fields[19] = "S", str(ticks), "0", str(start)
    (process / "stat").write_text("12 (gateway (name)) " + " ".join(fields))
    (root / "stat").write_text(f"cpu0 {total_active} 0 0 {idle} 0 0 0 0\n")


def test_proc_cpu_per_core_pid_reuse_missing_are_unknown(tmp_path):
    fake_proc(tmp_path, 100)
    probe = LocalProbe(12, [], proc=tmp_path, ticks_per_second=100)
    assert probe.sample(START)["gateway_cpu_pct"] == UNKNOWN
    fake_proc(tmp_path, 350, idle=900, total_active=300)
    row = probe.sample(START + STEP_NS)
    assert row["gateway_cpu_pct"] == 25
    assert row["system_per_core_cpu_pct"] == {"cpu0": 50}
    assert row["new_error_count"] == UNKNOWN
    fake_proc(tmp_path, 500, start=200)
    assert probe.sample(START + 2 * STEP_NS)["gateway_cpu_pct"] == UNKNOWN
    (tmp_path / "stat").unlink()
    assert "proc_read" in probe.sample(START + 3 * STEP_NS)["probe_status"]["cpu"]


@pytest.mark.asyncio
async def test_probes_run_in_worker_not_event_loop(tmp_path, monkeypatch):
    import threading

    probe = LocalProbe(12, [], proc=tmp_path, ticks_per_second=100)
    main_thread = threading.get_ident()
    seen = []
    monkeypatch.setattr(probe, "sample", lambda now: seen.append(threading.get_ident()) or {})
    assert await probe.sample_offloop(START) == {}
    assert seen[0] != main_thread


def test_new_files_have_no_provider_database_control_imports_or_autostart():
    root = Path(__file__).parents[2]
    for filename in ("momwarn_measurements.py", "momwarn_measurement_probes.py"):
        tree = ast.parse((root / "src/project_mai_tai" / filename).read_text())
        names = [node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
        assert not any(token in name for name in names for token in ("redis", "sqlalchemy", "settings",
                                                                    "provider", "gateway", "engine"))
        assert not any(isinstance(node, ast.While) for node in tree.body)

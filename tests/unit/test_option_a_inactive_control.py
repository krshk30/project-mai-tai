import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scripts import option_a_inactive_control as control


def test_trace_reports_observed_and_whole_window_coverage_separately(tmp_path: Path) -> None:
    path = tmp_path / "trace.txt"
    path.write_text(
        "2026-09-30T11:29:14.000Z load=3.49,2.0,1.0 snap_last=1790767754000-0 hb_last=1-0\n"
        "2026-09-30T11:29:16.000Z load=3.51,2.0,1.0 snap_last=1790767759000-0 hb_last=2-0\n"
        "2026-09-30T13:40:00.000Z load=8.0,2.0,1.0 snap_last=1790775600000-0 hb_last=3-0\n",
        encoding="utf-8",
    )

    result = control.summarize_trace(path)

    assert result["load_unique_seconds"] == 2
    assert result["load_expected_whole_control"] == 9600
    assert result["load_missing_observed_seconds"] == 1
    assert result["load_over_3_5_unique_seconds"] == 1
    assert result["snapshot_interval_p95_s"] == 5.0
    assert result["snapshot_reference_by_et_hour"]["7"]["source"] == "whole_control_fallback"


def test_snapshot_reference_requires_rolling_window_with_twenty_intervals(tmp_path: Path) -> None:
    path = tmp_path / "trace.txt"
    start = datetime(2026, 9, 30, 11, 30, tzinfo=UTC)
    rows = []
    for index in range(85):
        stamp = start + timedelta(seconds=5 * index)
        stream_id = f"{int(stamp.timestamp() * 1000)}-0"
        rows.append(
            f"{stamp.isoformat().replace('+00:00', 'Z')} "
            f"load=2.0,2.0,2.0 snap_last={stream_id} hb_last=1-0"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    result = control.summarize_trace(path)

    assert result["snapshot_rolling_5m_p95_count"] > 0
    assert result["snapshot_reference_by_et_hour"]["7"]["control_p95_s"] == 5.0
    assert result["snapshot_reference_by_et_hour"]["7"]["stop_strictly_above_s"] == 10.0


def test_heartbeat_dumps_dedupe_events_and_keep_gateway_status(tmp_path: Path) -> None:
    path = tmp_path / "heartbeats.txt"
    one = {
        "event_id": "one", "event_type": "service_heartbeat", "source_service": "market-data-gateway",
        "produced_at": "2026-09-30T11:00:00Z", "payload": {"status": "healthy"},
    }
    two = {**one, "event_id": "two", "produced_at": "2026-09-30T11:00:15Z"}
    path.write_text("\n".join(map(json.dumps, (one, two, one))) + "\n", encoding="utf-8")

    result = control.summarize_heartbeats([path], None)

    assert result["gateway_events"] == 2
    assert result["status_counts"] == {"healthy": 2}
    assert result["interarrival_p95_s"] == 15.0


def test_v2_probe_uses_bar_close_and_watched_minutes(tmp_path: Path) -> None:
    path = tmp_path / "v2.log"
    path.write_text(
        "2026-09-30 10:59:00,000 INFO schwab_1m_v2 watchlist updated count=1 sample=TEST warmed=1\n"
        "2026-09-30 11:01:02,500 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766000000\n"
        "2026-09-30 11:01:02,600 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766000000\n"
        "2026-09-30 11:02:00,000 INFO schwab_1m_v2 watchlist updated count=0 sample= warmed=0\n",
        encoding="utf-8",
    )

    result = control.summarize_v2(path)

    assert result["watchlist_at_start"] == ["TEST"]
    assert result["probe_unique_total"] == 1
    assert result["probe_duplicate_total"] == 1
    assert result["per_symbol"]["TEST"]["lag_p95_s"] == 2.5
    assert result["per_symbol"]["TEST"]["reference_by_et_hour"]["7"]["control_p95_s"] == 2.5


def test_v2_reports_rolling_p95_and_raw_control_reference(tmp_path: Path) -> None:
    path = tmp_path / "v2.log"
    path.write_text(
        "2026-09-30 10:59:00,000 INFO schwab_1m_v2 watchlist updated count=1 sample=TEST warmed=1\n"
        "2026-09-30 11:01:01,000 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766000000\n"
        "2026-09-30 11:02:02,000 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766060000\n"
        "2026-09-30 11:03:03,000 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766120000\n",
        encoding="utf-8",
    )

    result = control.summarize_v2(path)["per_symbol"]["TEST"]

    assert result["rolling_5m_p95_count"] >= 1
    assert result["reference_by_et_hour"]["7"]["source"] == "symbol_whole_control_fallback"
    assert result["reference_by_et_hour"]["7"]["control_p95_s"] == 3.0
    assert result["reference_by_et_hour"]["7"]["stop_strictly_above_s"] == 6.0


def test_v2_truncated_watchlist_sample_is_reported_as_incomplete(tmp_path: Path) -> None:
    path = tmp_path / "v2.log"
    path.write_text(
        "2026-09-30 10:59:00,000 INFO schwab_1m_v2 watchlist updated count=6 sample=A,B,C,D,E warmed=6\n"
        "2026-09-30 11:01:00,000 INFO schwab_1m_v2 watchlist updated count=2 sample=A,B warmed=2\n",
        encoding="utf-8",
    )

    result = control.summarize_v2(path)

    assert result["watchlist_at_start_complete"] is False
    assert result["watchlist_incomplete_minutes"] == 2
    assert result["watchlist_truncated_updates_in_window"] == 0


def test_v2_seed_probe_is_excluded_before_deduplicating_live_probe(tmp_path: Path) -> None:
    path = tmp_path / "v2.log"
    path.write_text(
        "2026-09-30 10:59:00,000 INFO watchlist updated count=1 sample=TEST warmed=1\n"
        "2026-09-30 11:01:02,100 WARNING [V2-DB-SEED-GAP] TEST dropped 2 bars\n"
        "2026-09-30 11:01:02,101 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766000000\n"
        "2026-09-30 11:01:02,102 INFO schwab_1m_v2 db-seed: TEST hydrated 3 bars\n"
        "2026-09-30 11:01:03,000 INFO [V2-ATR-PROBE] sym=TEST ts_ms=1790766000000\n",
        encoding="utf-8",
    )

    result = control.summarize_v2(path)

    assert result["probe_replay_marker_excluded_total"] == 1
    assert result["probe_unique_total"] == 1
    assert result["probe_duplicate_total"] == 0
    assert result["per_symbol"]["TEST"]["lag_p95_s"] == 3.0


def test_oms_counts_both_direct_refusal_kinds(tmp_path: Path) -> None:
    path = tmp_path / "oms.log"
    path.write_text(
        "2026-09-30 11:01:00,000 INFO [OMS-ABANDON-INTENT] code=NO_FRESH_QUOTE symbol=TEST\n"
        "2026-09-30 11:02:00,000 WARNING no valid OMS market snapshot\n"
        "2026-09-30 11:03:00,000 WARNING [OMS-BROKER-REJECT] "
        "reason=NO_FRESH_QUOTE: Webull resting mirror has no valid OMS market snapshot\n"
        "2026-09-30 13:40:00,000 INFO [OMS-ABANDON-INTENT] code=NO_FRESH_QUOTE symbol=TEST\n",
        encoding="utf-8",
    )

    result = control.summarize_oms(path)

    assert result["timestamped_records_in_window"] == 3
    assert result["direct_refusals"] == {
        "NO_FRESH_QUOTE": 2,
        "no_valid_market_snapshot": 1,
    }

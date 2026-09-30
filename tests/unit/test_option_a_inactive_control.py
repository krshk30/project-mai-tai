import json
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
    assert result["per_symbol"]["TEST"]["reference_by_et_hour"]["7"]["source"] == "symbol_whole_control_fallback"


def test_oms_counts_both_direct_refusal_kinds(tmp_path: Path) -> None:
    path = tmp_path / "oms.log"
    path.write_text(
        "2026-09-30 11:01:00,000 INFO [OMS-ABANDON-INTENT] code=NO_FRESH_QUOTE symbol=TEST\n"
        "2026-09-30 11:02:00,000 WARNING no valid OMS market snapshot\n"
        "2026-09-30 13:40:00,000 INFO [OMS-ABANDON-INTENT] code=NO_FRESH_QUOTE symbol=TEST\n",
        encoding="utf-8",
    )

    result = control.summarize_oms(path)

    assert result["timestamped_records_in_window"] == 2
    assert result["direct_refusals"] == {
        "NO_FRESH_QUOTE": 1,
        "no_valid_market_snapshot": 1,
    }

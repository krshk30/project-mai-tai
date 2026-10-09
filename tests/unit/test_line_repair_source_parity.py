"""Synthetic measurement controls plus untouched recorded 25-row assessment."""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path

import pytest

from scripts.line_repair_source_parity import ET, analyze, assess, index, main, table


def bar(stamp, *, price=2, volume=10):
    return {"t": stamp, "o": price, "h": price + 0.1, "l": max(0, price - 0.1),
            "c": price, "v": volume}


def fixture():
    boundary = int(datetime(2026, 10, 9, 7, tzinfo=ET).timestamp() * 1000)
    prefix = [bar(boundary - 3 * 3_600_000 + minute * 60_000) for minute in range(20)]
    live = [bar(boundary + minute * 60_000) for minute in range(10)]
    candles = [{"datetime": b["t"], "open": b["o"], "high": b["h"], "low": b["l"],
                "close": b["c"], "volume": b["v"]} for b in live]
    return {"day": "2026-10-09", "symbol": "CONTROL", "boundary_ms": boundary,
            "schwab": {"candles": candles}, "preopen": {"results": prefix},
            "chart": {"results": deepcopy(prefix + live)}}


def test_exact_sources_pass_and_do_not_modify_input():
    row = fixture()
    before = deepcopy(row)
    result = analyze(row)
    assert row == before
    assert (result["both"], result["time_exact"], result["only_M"], result["only_S"]) == (
        10, True, [], [])
    assert result["ohlc_exact"] == result["volume_exact"] == 10
    assert result["ohlc_max_diff_pct"] == result["volume_max_diff_pct"] == "0"
    assert result["line_impact"]["result"] == result["source_result"] == "PASS"
    assert assess([row])["exit_code"] == 0


def test_exact_ohlc_measurement_uses_massive_denominator_without_tolerance():
    row = fixture()
    row["schwab"]["candles"][0]["close"] = "2.000001"
    result = analyze(row)
    assert result["ohlc_exact"] == 9
    assert result["field_exact"] == {"o": 10, "h": 10, "l": 10, "c": 9, "v": 10}
    assert Decimal(result["ohlc_max_diff_pct"]) == Decimal("0.00005")
    assert result["diff_records"][0]["fields"]["c"] == {
        "Schwab": "2.000001", "Massive": "2", "abs_diff_pct": "0.0000500"}
    assert result["source_result"] == "FAIL"


def test_volume_exact_median_max_and_direction_are_per_paired_bar():
    row = fixture()
    row["schwab"]["candles"] = [row["schwab"]["candles"][0], row["schwab"]["candles"][-1]]
    row["chart"]["results"] = row["chart"]["results"][:20] + [
        row["chart"]["results"][20], row["chart"]["results"][-1]]
    row["schwab"]["candles"][0]["volume"] = 20
    row["schwab"]["candles"][-1]["volume"] = 5
    result = analyze(row)
    assert result["volume_exact"] == 0
    assert Decimal(result["volume_median_diff_pct"]) == 75
    assert Decimal(result["volume_max_diff_pct"]) == 100
    assert result["volume_direction"] == {"massive_greater": 1, "schwab_greater": 1, "equal": 0}
    assert result["line_impact"]["result"] == "PASS"  # Volume cannot change ATR.
    assert result["source_result"] == "FAIL" and assess([row])["exit_code"] == 1


@pytest.mark.parametrize("s,m,expected", [(0, 0, "0"), (1, 0, "Infinity"), (0, 1, "100")])
def test_zero_reference_is_explicit_and_json_is_standard(s, m, expected):
    row = fixture()
    for candle in row["schwab"]["candles"]:
        candle["volume"] = s
    for candle in row["chart"]["results"][20:]:
        candle["v"] = m
    result = analyze(row)
    assert result["volume_max_diff_pct"] == result["volume_median_diff_pct"] == expected
    json.dumps(assess([row]), allow_nan=False)
    assert result["line_impact"]["result"] == "PASS"


def test_only_provider_minutes_and_et_window_are_not_silently_intersected():
    row = fixture()
    boundary = row["boundary_ms"]
    row["schwab"]["candles"].pop(1)
    row["chart"]["results"].pop(22)
    row["chart"]["results"].append(bar(boundary + 3_600_000))  # 08:00 excluded.
    result = analyze(row)
    assert result["both"] == 8 and result["time_exact"] is False
    assert result["only_M"] == [boundary + 60_000]
    assert result["only_S"] == [boundary + 120_000]
    assert [d["kind"] for d in result["diff_records"]] == ["only_Massive", "only_Schwab"]
    assert result["source_result"] == "FAIL"


def test_line_impact_only_overlaps_and_preserves_schwab_timestamps_and_prefix():
    row = fixture()
    boundary = row["boundary_ms"]
    row["chart"]["results"][-1] = bar(boundary + 9 * 60_000, price=1)
    row["chart"]["results"].append(bar(boundary + 10 * 60_000, price=999))
    row["chart"]["results"].pop(21)  # Leave this Schwab-only minute intact.
    impact = analyze(row)["line_impact"]
    assert impact["result"] == "FAIL"
    assert impact["changed_fields"] == ["state", "level", "age"]
    assert impact["baseline"]["state"] == "long" and impact["replaced"]["state"] == "short"
    assert impact["baseline_timestamps_ms"] == impact["replaced_stream_timestamps_ms"]
    assert impact["baseline_timestamps_ms"][:20] == [b["t"] for b in row["preopen"]["results"]]
    assert boundary + 60_000 not in impact["replaced_timestamps_ms"]
    assert boundary + 10 * 60_000 not in impact["replaced_stream_timestamps_ms"]
    assert assess([row])["exit_code"] == 1


def test_missing_exact_0709_is_unmeasured_not_previous_bar_pass():
    row = fixture()
    row["schwab"]["candles"].pop()
    row["chart"]["results"].pop()
    impact = analyze(row)["line_impact"]
    assert impact["result"] == "UNMEASURED"
    assert impact["baseline"] is impact["replaced"] is None
    assert impact["last_available_baseline"]["timestamp_ms"] == row["boundary_ms"] + 8 * 60_000
    assert assess([row])["exit_code"] == 1


def test_unseeded_target_does_not_certify_line_parity():
    row = fixture()
    row["preopen"]["results"] = []
    row["chart"]["results"] = row["chart"]["results"][-1:]
    row["schwab"]["candles"] = row["schwab"]["candles"][-1:]
    assert analyze(row)["line_impact"]["result"] == "UNSEEDED"
    assert assess([row])["exit_code"] == 1


@pytest.mark.parametrize("status,count,result", [("OK", 0, "UNSEEDED"),
                                                ("ERROR", 0, "ERROR"), ("OK", 1, "ERROR")])
def test_missing_results_only_means_empty_with_explicit_provider_proof(status, count, result):
    row = fixture()
    row["preopen"] = {"status": status, "resultsCount": count}
    row["chart"]["results"] = row["chart"]["results"][-1:]
    row["schwab"]["candles"] = row["schwab"]["candles"][-1:]
    report = assess([row])
    measured = report["rows"][0]
    assert (measured["source_result"] if result == "ERROR" else measured["line_impact"]["result"]) == result


@pytest.mark.parametrize("damage", ["duplicate", "bad-boundary", "nonminute", "prefix", "nan"])
def test_invalid_rows_are_reported_without_hiding_later_rows(damage):
    row = fixture()
    if damage == "duplicate":
        row["schwab"]["candles"].append(row["schwab"]["candles"][0])
    elif damage == "bad-boundary":
        row["boundary_ms"] += 60_000
    elif damage == "nonminute":
        row["schwab"]["candles"][0]["datetime"] += 1
    elif damage == "prefix":
        row["preopen"]["results"][0]["c"] = 3
    else:
        row["schwab"]["candles"][0]["volume"] = "NaN"
    report = assess([row, fixture()])
    assert report["exit_code"] == 2
    assert report["rows"][0]["source_result"] == "ERROR"
    assert report["rows"][1]["source_result"] == "PASS"
    assert "ERROR" in table(report)


def test_volume_fraction_is_not_truncated_and_duplicates_never_overwrite():
    assert index([bar(0, volume="0.123456")])[0]["v"] == Decimal("0.123456")
    with pytest.raises(ValueError, match="duplicate"):
        index([bar(0), bar(0)])


def test_cli_reports_mismatch_exit_and_all_diff_records(tmp_path, capsys):
    row = fixture()
    row["schwab"]["candles"][0]["volume"] = 11
    path = tmp_path / "controlled.json"
    path.write_text(json.dumps({"rows": [row]}))
    assert main([str(path), "--format", "json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert len(report["rows"][0]["diff_records"]) == 1
    assert "Massive" in report["method"]["percent"]


def test_recorded_population_reports_all_25_without_changing_fixture():
    path = Path(__file__).parents[1] / "fixtures/line_repair_preopen_20261005_09.json"
    raw = path.read_bytes()
    rows = json.loads(raw, parse_float=Decimal)["rows"]
    report = assess(rows)
    assert len(report["rows"]) == 25 and report["exit_code"] == 1
    assert path.read_bytes() == raw
    assert all(row["time_exact"] and not row["only_M"] and not row["only_S"]
               for row in report["rows"])
    differing = [(r["day"], r["symbol"], r["both"] - r["ohlc_exact"]) for r in report["rows"]
                 if r["ohlc_exact"] != r["both"]]
    assert differing == [("2026-10-06", "RUBI", 6), ("2026-10-08", "AIXI", 4),
                         ("2026-10-08", "FLYE", 2)]
    assert all(r["volume_exact"] == 0 for r in report["rows"])

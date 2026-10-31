"""CONTROLLED C1 14:46 scope mechanics; no actual TODAY completeness proof."""

from datetime import datetime, timedelta, timezone

import pytest

from tests.unit.test_atrsimlive1_runtime import ROOT, module


LEFT = datetime(2026, 10, 28, 14, tzinfo=timezone.utc)
END = LEFT + timedelta(minutes=5)


def operands(monkeypatch, *, positive=(0, 1), zero=(), missing_print=()):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    coverage = module("atrsimlive1_data_coverage")
    models = module("atrsimlive1_models")
    collect = module("atrsimlive1_collect")
    bars = [dict(id=minute + 1, provider="massive", symbol="CONTROLLED", interval_secs=60,
                 event_ts=LEFT + timedelta(minutes=minute), received_at=END,
                 volume=100 if minute in positive else 0)
            for minute in sorted((*positive, *zero))]
    trades = [dict(id=minute + 1, provider="massive", symbol="CONTROLLED",
                   event_ts=LEFT + timedelta(minutes=minute, seconds=3), received_at=END,
                   size=100, price="2.00", conditions="")
              for minute in positive if minute not in missing_print]
    proof = dict(provider="massive", symbol="CONTROLLED", start=LEFT, end=END,
                 full_read=True, truncated=False, row_count=len(trades), rows_sha256=collect.digest(trades),
                 bar_table="market_capture_bars", bars_full_read=True, bars_truncated=False,
                 bar_count=len(bars), bars_sha256=collect.digest(bars))
    log = dict(status="AVAILABLE", first_stats_at=LEFT.isoformat(), last_stats_at=END.isoformat(),
               dropped=0, asof=END.isoformat(), ruling="C1_20261028_1446_DATA_DERIVED",
               label="CONTROLLED log witness; not an actual capture fact")
    args = dict(symbol="CONTROLLED", day="2026-10-28", bars=bars, trades=trades,
                read_proof=proof, log=log, asof_ns=models.nanos(END), nanos=models.nanos)
    return coverage, models, collect, args


def test_positive_bar_print_crosscheck_forms_maximal_interval_and_retains_census(monkeypatch):
    coverage, models, _, args = operands(monkeypatch)
    windows, report = coverage.derive(**args)
    assert len(windows) == 1
    assert windows[0][:2] == (models.nanos(LEFT), models.nanos(LEFT + timedelta(minutes=2)))
    metrics = report["windows"][0]
    assert metrics["row_count"] == 2 and metrics["first_print_id"] == 1 and metrics["last_print_id"] == 2
    assert metrics["max_interprint_gap_ns"] == 60000000000
    assert report["native_market_complete"] is False
    assert "native/market completeness UNKNOWN" in windows[0][3]
    assert report["minutes"][-1]["status"] == "UNKNOWN_UNPROVEN_MINUTE_OR_LOG_BOUNDARY"


def test_positive_volume_without_print_breaks_never_bridges(monkeypatch):
    coverage, models, _, args = operands(monkeypatch, positive=(0, 1, 2), missing_print=(1,))
    windows, report = coverage.derive(**args)
    assert len(windows) == 2
    assert windows[0][1] == models.nanos(LEFT + timedelta(minutes=1))
    assert windows[1][0] == models.nanos(LEFT + timedelta(minutes=2))
    assert report["minutes"][1]["status"] == "UNKNOWN_POSITIVE_BAR_ZERO_PRINTS"


@pytest.mark.parametrize("zero", [(), (1,)])
def test_only_single_interior_no_trading_gap_can_bridge(monkeypatch, zero):
    coverage, models, _, args = operands(monkeypatch, positive=(0, 2), zero=zero)
    windows, report = coverage.derive(**args)
    assert len(windows) == 1 and windows[0][1] == models.nanos(LEFT + timedelta(minutes=3))
    assert report["minutes"][1]["status"] == "COVERED_INTERIOR_NO_TRADING_BETWEEN_INDEPENDENT_MINUTES"


def test_consecutive_missing_minutes_do_not_bootstrap_each_other(monkeypatch):
    coverage, _, _, args = operands(monkeypatch, positive=(0, 3))
    windows, report = coverage.derive(**args)
    assert len(windows) == 2
    assert all("UNKNOWN" in report["minutes"][minute]["status"] for minute in (1, 2, 4))


def test_absent_day_never_becomes_complete_no_trading_day(monkeypatch):
    coverage, _, _, args = operands(monkeypatch, positive=())
    windows, report = coverage.derive(**args)
    assert windows == [] and len(report["minutes"]) == 5
    assert all("UNKNOWN" in minute["status"] for minute in report["minutes"])


def test_missing_day_log_is_secondary_unknown_nonblocking_under_c1_1534(monkeypatch):
    coverage, _, _, args = operands(monkeypatch)
    args["log"] = dict(status="UNKNOWN", issues=["CAPTURE_LOG_TIMESTAMP_DAY_UNKNOWN"])
    windows, report = coverage.derive(**args)
    assert len(windows) == 1
    assert report["minutes"][0]["print_count"] == 1
    assert report["minutes"][0]["status"] == "COVERED_POSITIVE_BAR_WITH_PRINT"
    assert "SECONDARY_CAPTURE_LOG_UNKNOWN_NONBLOCKING" in report["secondary_issues"]


@pytest.mark.parametrize("mutation", ["future_bar", "bar_hash", "trade_hash", "cap", "foreign_log"])
def test_full_snapshot_receipts_and_actual_asof_are_not_optional(monkeypatch, mutation):
    coverage, _, collect, args = operands(monkeypatch)
    if mutation == "future_bar":
        args["bars"][0]["received_at"] = END + timedelta(seconds=1)
        args["read_proof"]["bars_sha256"] = collect.digest(args["bars"])
    elif mutation == "bar_hash":
        args["read_proof"]["bars_sha256"] = "0" * 64
    elif mutation == "trade_hash":
        args["read_proof"]["rows_sha256"] = "0" * 64
    elif mutation == "cap":
        args["read_proof"]["truncated"] = True
    else:
        args["log"]["asof"] = (END - timedelta(seconds=1)).isoformat()
    windows, report = coverage.derive(**args)
    if mutation == "foreign_log":
        assert windows and "SECONDARY_CAPTURE_LOG_UNKNOWN_NONBLOCKING" in report["secondary_issues"]
    else:
        assert windows == [] and report["issues"]


@pytest.mark.parametrize("mutation", ["normal", "undated", "drop", "reset", "restart", "truncated"])
def test_capture_log_requires_actual_day_clock_stable_epoch_and_zero_drop(monkeypatch, tmp_path, mutation):
    coverage, _, _, _ = operands(monkeypatch)
    first = "2026-10-28T14:00:00+00:00 INFO [CAPTURE] rows_written=100 buffered=0 dropped=0\n"
    last = "2026-10-28T14:05:00+00:00 INFO [CAPTURE] rows_written=200 buffered=0 dropped=0\n"
    middle = ""
    if mutation == "undated":
        first = "INFO:market-capture:[CAPTURE] rows_written=100 buffered=0 dropped=0\n"
    elif mutation == "drop":
        last = last.replace("dropped=0", "dropped=1")
    elif mutation == "reset":
        last = last.replace("rows_written=200", "rows_written=20")
    elif mutation == "restart":
        middle = "2026-10-28T14:02:00+00:00 INFO [CAPTURE] starting — CONTROLLED\n"
    elif mutation == "truncated":
        last = last.rstrip("\n")
    path = tmp_path / "capture.log"
    raw = (first + middle + last).encode("utf-8")
    path.write_bytes(raw)
    before = path.read_bytes()
    result = coverage.capture_log(path, asof=END)
    assert path.read_bytes() == before
    if mutation == "normal":
        assert result["status"] == "AVAILABLE" and result["dropped"] == 0 and result["stats_count"] == 2
        assert result["first_stats_at"] == LEFT.isoformat() and result["last_stats_at"] == END.isoformat()
    else:
        assert result["status"] == "UNKNOWN" and result["issues"]
        assert "dropped" not in result

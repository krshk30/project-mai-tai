"""CONTROLLED C1 15:34/36/41 units; no real TODAY/provider/native proof."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json

import pytest

from tests.unit.test_atrsimlive1_runtime import ROOT, module


LEFT = datetime(2026, 10, 28, 8, tzinfo=timezone.utc)  # TODAY04ET


def units(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "tools"))
    return module("atrsimlive1_line_route"), module("atrsimlive1_models"), module("atrsimlive1_collect")


def bars():
    prices = [(10, 11, 9)] * 9 + [(2, 3, 1), (20, 21, 19),
        (22, 23, 21), (22, 23, 21), (22, 23, 21), (1, 2, Decimal("0.5")), (1, 2, Decimal("0.5"))]
    return [dict(id=str(index + 1), symbol="CONTROLLED", bar_time=LEFT + timedelta(minutes=index),
        open_price=str(close), high_price=str(high), low_price=str(low), close_price=str(close),
        volume=1000, source="CONTROLLED-stored", created_at=LEFT + timedelta(minutes=index + 1, seconds=1),
        indicators={}, decision_status=None, decision_reason=None, decision_path=None)
        for index, (close, high, low) in enumerate(prices)]


def route_packet(models, broker="schwab"):
    stamp = LEFT + timedelta(minutes=10)
    segment = models.nanos(stamp) // 1000000
    text = ("[ATR-COUNTRY-SELECTED] symbol=CONTROLLED segment=" + str(segment) + " route=" + broker
            + " reason=" + ("sec_us" if broker == "schwab" else "sec_non_us")
            + " status=READY as_of=" + LEFT.isoformat() + " notional=" + ("600" if broker == "schwab" else "300"))
    return dict(schema="ATRSIMLIVE1_COUNTRY_LOG_PROJECTION_V1", trading_day="2026-10-28",
        source=dict(artifact="/CONTROLLED/selected-lines.json", sha256="a" * 64,
                    asof=(LEFT + timedelta(minutes=18)).isoformat(), epoch="CONTROLLED"),
        lines=[dict(line_number=1, recorded_at=(stamp + timedelta(seconds=1)).isoformat(), text=text)])


def test_unchanged_oracle_recomputes_literal_buy_sell_from_empty_indicators(monkeypatch):
    helper, models, _ = units(monkeypatch)
    source = bars()
    before = deepcopy(source)
    result = helper.recompute(source, nanos=models.nanos)
    assert result[8]["oracle"]["state"] == "long"
    assert result[8]["oracle"]["trail"] == 3.0
    assert result[9]["oracle"]["flip"] == "SELL"
    assert result[10]["oracle"]["flip"] == "BUY"
    assert result[10]["oracle"]["trail"] == 4.46
    assert result[14]["oracle"]["flip"] == "SELL"
    assert result[10]["available_ns"] == models.nanos(LEFT + timedelta(minutes=11))
    assert result[10]["source_created_at"] > LEFT + timedelta(minutes=11)
    assert source == before and all(not row["indicators"] for row in source)


@pytest.mark.parametrize("offset,status", [(0, "MATCH"), (1, "MATCH"), (2, "MISMATCH")])
def test_c1_reported_buy_reference_tolerance_is_one_completed_bar(monkeypatch, offset, status):
    helper, models, _ = units(monkeypatch)
    expected = datetime(2026, 10, 28, 13, 2, tzinfo=timezone.utc)
    lines = [dict(original=dict(id="CONTROLLED"), oracle=dict(flip="BUY"), start_ns=1,
                  available_ns=models.nanos(expected + timedelta(minutes=offset)))]
    result = helper.parity("2026-10-28", "ELPW", lines,
                          asof_ns=models.nanos(expected + timedelta(hours=1)), nanos=models.nanos)
    assert result["comparisons"][0]["status"] == status
    assert "C1-reported" in result["comparisons"][0]["reference_authority"]


def test_future_day_reference_is_unknown_not_zero_of_zero_pass(monkeypatch):
    helper, models, _ = units(monkeypatch)
    result = helper.parity("2026-10-29", "ELPW", [], asof_ns=1, nanos=models.nanos)
    assert result["status"] == "UNKNOWN" and result["expected"] is None


@pytest.mark.parametrize("mismatches,blocks", [(0, False), (1, True), (2, True)])
def test_all_six_original_references_required_without_automatic_miss_allowance(monkeypatch, mismatches, blocks):
    helper, _, _ = units(monkeypatch)
    expected = (("ELPW", "09:02"), ("POM", "10:51"), ("POM", "11:44"),
                ("CHSN", "12:20"), ("CHSN", "13:31"), ("FTFT", "15:39"))
    # Human reference correction C1 20:09:08; population remains all six.
    assert helper.REFERENCES["2026-10-28"] == expected
    comparisons = [dict(symbol=symbol, reference=clock, status="MISMATCH" if index < mismatches else "MATCH")
                   for index, (symbol, clock) in enumerate(expected)]
    result = helper.parity_summary("2026-10-28", [dict(comparisons=comparisons)])
    assert result["expected"] == 6 and result["mismatches"] == mismatches
    assert result["blocks_qualification"] is blocks and len(result["comparisons"]) == 6


def test_missing_reference_is_unknown_and_duplicate_comparison_refuses(monkeypatch):
    helper, _, _ = units(monkeypatch)
    item = dict(symbol="ELPW", reference="09:02", status="MATCH")
    result = helper.parity_summary("2026-10-28", [dict(comparisons=[item])])
    assert result["unresolved"] == 5 and result["blocks_qualification"] is True
    with pytest.raises(ValueError, match="duplicate_original"):
        helper.parity_summary("2026-10-28", [dict(comparisons=[item, item])])


@pytest.mark.parametrize("broker,quantity", [("schwab", 28), ("webull", 14)])
def test_actual_log_grammar_route_and_floor_sizing(monkeypatch, broker, quantity):
    helper, models, _ = units(monkeypatch)
    packet = route_packet(models, broker)
    before = deepcopy(packet)
    fact = helper.route_fact(packet, day="2026-10-28", symbol="CONTROLLED",
        asof_ns=models.nanos(LEFT + timedelta(minutes=18)), nanos=models.nanos)
    assert fact["broker"] == broker and fact["status"] == "RECORDED_DAY_ROUTE"
    assert helper.size(fact, "21.02")["quantity"] == quantity
    assert packet == before


@pytest.mark.parametrize("mutation", ["missing", "wrong_day", "conflict", "wrong_notional", "future"])
def test_unproven_or_conflicting_route_has_no_units_or_dollars(monkeypatch, mutation):
    helper, models, _ = units(monkeypatch)
    packet = route_packet(models)
    if mutation == "missing":
        packet = None
    elif mutation == "wrong_day":
        packet["trading_day"] = "2026-10-27"
    elif mutation == "conflict":
        packet["lines"].extend(route_packet(models, "webull")["lines"])
    elif mutation == "wrong_notional":
        packet["lines"][0]["text"] = packet["lines"][0]["text"].replace("notional=600", "notional=300")
    else:
        packet["source"]["asof"] = (LEFT + timedelta(days=1)).isoformat()
    fact = helper.route_fact(packet, day="2026-10-28", symbol="CONTROLLED",
        asof_ns=models.nanos(LEFT + timedelta(minutes=18)), nanos=models.nanos)
    sized = helper.size(fact, "21.02")
    assert fact["status"] == "UNKNOWN" and sized["usd_known"] is False
    assert sized["quantity"] == 1 and "normalized" in sized["percent_basis"]


def test_percent_preserves_weighted_cost_and_whole_exit_conservation(monkeypatch):
    helper, _, _ = units(monkeypatch)
    entries = [dict(price="2", quantity=100), dict(price="3", quantity=100)]
    assert helper.percent(entries, [dict(price="2.75", quantity=200)]) == Decimal("10.0")
    assert helper.percent(entries, [dict(price="2.75", quantity=100)]) is None
    assert helper.percent(entries, [dict(price=None, quantity=200)]) is None


def data_fixture(monkeypatch, tmp_path):
    helper, models, collect = units(monkeypatch)
    raw_policy = json.dumps({"1": {"name": "CONTROLLED", "updates_high_low": True,
        "updates_open_close": True, "updates_volume": True}}).encode()
    policy = tmp_path / "controlled-policy.json"
    policy.write_bytes(raw_policy)
    monkeypatch.setattr(models, "POLICY_SHA", hashlib.sha256(raw_policy).hexdigest())
    source_bars = bars()
    end = LEFT + timedelta(minutes=18)
    trades = [dict(id=index + 1, provider="massive", symbol="CONTROLLED",
        event_ts=row["bar_time"] + timedelta(seconds=5), received_at=end,
        price=row["close_price"], size=1000, conditions="", exchange=1, cumulative_volume=None)
        for index, row in enumerate(source_bars)]
    trades.extend([
        dict(id=101, provider="massive", symbol="CONTROLLED", event_ts=LEFT + timedelta(minutes=11, seconds=1),
             received_at=end, price="21.02", size=1000, conditions="", exchange=1, cumulative_volume=None),
        dict(id=102, provider="massive", symbol="CONTROLLED", event_ts=LEFT + timedelta(minutes=11, seconds=2),
             received_at=end, price="22.08", size=1000, conditions="", exchange=1, cumulative_volume=None)])
    rows = dict(bars=source_bars, capture_bars=[], scanner=[], fills=[], trades=trades, quotes=[])
    data = dict(trading_day="2026-10-28", source_asof=end, snapshot_id="CONTROLLED", start=LEFT, end=end,
        symbols=["CONTROLLED"], rows=rows, source_hashes={key: collect.digest(value) for key, value in rows.items()},
        source_counts={key: len(value) for key, value in rows.items()}, query_sha256="b" * 64,
        capture_log=dict(status="UNKNOWN", issues=["CAPTURE_LOG_UNREADABLE"]),
        read_proof=dict(provider="massive", symbol="CONTROLLED", start=LEFT, end=end,
            full_read=True, truncated=False, rows_sha256=collect.digest(trades), row_count=len(trades),
            bar_provider="schwab", bar_table="strategy_bar_history", bars_full_read=True,
            bars_truncated=False, bar_count=len(source_bars), bars_sha256=collect.digest(source_bars)))
    return models, data, policy


@pytest.mark.parametrize("broker,expected_qty,expected_pnl", [("schwab", "28", "29.68"), ("webull", "14", "14.84")])
def test_sim_buy_opportunities_do_not_require_real_fills_or_metadata(monkeypatch, tmp_path, broker, expected_qty, expected_pnl):
    models, data, policy = data_fixture(monkeypatch, tmp_path)
    result = models.produce(data, policy_path=policy, producer=dict(label="CONTROLLED"),
                            route_packet=route_packet(models, broker))
    rows = result["tables"]["FIRSTBAR"]["rows"]
    known = [item["row"] for item in rows if item["row"]["pnl_label_known"]]
    assert len(known) == 1 and known[0]["quantity"] == expected_qty
    assert Decimal(known[0]["pnl"]) == Decimal(expected_pnl)
    assert known[0]["pnl_pct_label_known"] is True
    assert known[0]["percent_basis"].startswith("sized modeled")
    assert "line recomputed from stored bars" in known[0]["coverage_note"]
    assert result["tables"]["LIVE:ACTUAL"]["rows"] == [] and data["rows"]["fills"] == []


def test_missing_route_retains_percent_but_never_claims_sized_units_usd(monkeypatch, tmp_path):
    models, data, policy = data_fixture(monkeypatch, tmp_path)
    result = models.produce(data, policy_path=policy, producer=dict(label="CONTROLLED"))
    rows = [item["row"] for item in result["tables"]["FIRSTBAR"]["rows"] if item["row"]["pnl_pct_label_known"]]
    assert len(rows) == 1
    assert rows[0]["quantity"] == "UNKNOWN" and rows[0]["pnl"] is None
    assert rows[0]["pnl_label"] == "UNKNOWN" and rows[0]["pnl_label_known"] is False
    assert "normalized" in rows[0]["percent_basis"]


def test_real_quantity_remains_recorded_while_sim_sizing_is_independent(monkeypatch, tmp_path):
    models, data, policy = data_fixture(monkeypatch, tmp_path)
    account = "11111111-1111-4111-8111-111111111111"
    strategy = "22222222-2222-4222-8222-222222222222"
    row = dict(symbol="CONTROLLED", side="buy", account_name="CONTROLLED-actual", broker="schwab",
        strategy_code="schwab_1m_v2", order_id="33333333-3333-4333-8333-333333333333",
        fill_id="44444444-4444-4444-8444-444444444444", quantity=3, price="21.02",
        order_quantity=3, order_status="filled", recorded_order_type="LIMIT", intent_payload={},
        order_payload={}, reason="CONTROLLED", submitted_at=LEFT + timedelta(minutes=11),
        filled_at=LEFT + timedelta(minutes=11, seconds=1))
    for prefix in ("fill", "order", "intent", "physical"):
        row[prefix + "_account_id"] = account
        row[prefix + "_strategy_id"] = strategy
    data["rows"]["fills"] = [row]
    data["source_hashes"]["fills"] = module("atrsimlive1_collect").digest([row])
    data["source_counts"]["fills"] = 1
    result = models.produce(data, policy_path=policy, producer=dict(label="CONTROLLED"),
                            route_packet=route_packet(models))
    assert result["tables"]["LIVE:ACTUAL"]["rows"][0]["row"]["quantity"] == "3"
    assert result["tables"]["FIRSTBAR"]["rows"][0]["row"]["quantity"] == "28"
    assert row["quantity"] == 3


def test_actual_positive_capture_drop_still_breaks_primary_coverage(monkeypatch):
    _, models, collect = units(monkeypatch)
    coverage = module("atrsimlive1_data_coverage")
    source = bars()
    trades = [dict(id=index, provider="massive", symbol="CONTROLLED", event_ts=row["bar_time"],
                   received_at=LEFT + timedelta(minutes=18)) for index, row in enumerate(source)]
    windows, report = coverage.derive(symbol="CONTROLLED", day="2026-10-28", bars=source, trades=trades,
        asof_ns=models.nanos(LEFT + timedelta(minutes=18)), nanos=models.nanos,
        log=dict(status="UNKNOWN", issues=["CAPTURE_LOG_DAY_DROPPED_POSITIVE"]),
        read_proof=dict(provider="massive", symbol="CONTROLLED", start=LEFT, end=LEFT + timedelta(minutes=18),
            full_read=True, truncated=False, rows_sha256=collect.digest(trades), row_count=len(trades),
            bar_provider="schwab", bar_table="strategy_bar_history", bars_full_read=True,
            bars_truncated=False, bar_count=len(source), bars_sha256=collect.digest(source)))
    assert windows == [] and "UNKNOWN_POSITIVE_CAPTURE_LOG_FAILURE" in report["issues"]

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from project_mai_tai.backtest.retry_one import (
    Bar,
    FillRow,
    ManagedRow,
    StudyInput,
    _fresh_cross,
    _model_counterfactual,
    evaluate,
)


def _at(day: date, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour + 4, minute, second, tzinfo=UTC)


def _bar(day: date, hour: int, minute: int, *, high: str, low: str, close: str) -> Bar:
    value = Decimal(close)
    return Bar(
        at=_at(day, hour, minute),
        open=value,
        high=Decimal(high),
        low=Decimal(low),
        close=value,
    )


def _episode(
    *,
    day: date,
    symbol: str,
    account: str,
    segment: str,
    entry_hm: tuple[int, int],
    exit_hm: tuple[int, int],
    entry: str,
    exit: str,
    line: str,
    reason: str,
) -> tuple[ManagedRow, FillRow, FillRow]:
    entry_at = _at(day, *entry_hm)
    exit_at = _at(day, *exit_hm)
    row_id = f"{symbol}-{account}-{segment}"
    managed = ManagedRow(
        row_id=row_id,
        account=account,
        symbol=symbol,
        entry_at=entry_at + timedelta(seconds=5),
        closed_at=exit_at + timedelta(seconds=2),
        quantity=Decimal("1"),
    )
    buy = FillRow(
        fill_id=f"{row_id}-buy",
        account=account,
        symbol=symbol,
        side="buy",
        quantity=Decimal("1"),
        price=Decimal(entry),
        filled_at=entry_at,
        order_quantity=Decimal("1"),
        order_type="STOP_LIMIT",
        reason="schwab_1m_v2 ATR Flip",
        metadata={"fanout_segment_id": segment, "cw_flip_level": line},
    )
    sell = FillRow(
        fill_id=f"{row_id}-sell",
        account=account,
        symbol=symbol,
        side="sell",
        quantity=Decimal("1"),
        price=Decimal(exit),
        filled_at=exit_at,
        order_quantity=Decimal("1"),
        order_type="oco_exit" if reason == "OCO_RESOLVED_FLAT" else "market",
        reason=(
            "schwab_1m_v2 ATR Flip"
            if reason == "OCO_RESOLVED_FLAT"
            else f"oms_v2_managed_exit:{reason}"
        ),
        metadata={},
    )
    return managed, buy, sell


def _population() -> StudyInput:
    vsa_day = date(2026, 9, 23)
    dcoy_day = date(2026, 9, 22)
    episodes = [
        _episode(
            day=vsa_day,
            symbol="VSA",
            account="live:orb",
            segment="vsa-1",
            entry_hm=(15, 29),
            exit_hm=(15, 30),
            entry="3.87",
            exit="3.57",
            line="3.859",
            reason="CW_HARD_STOP",
        )
    ]
    for account in ("live:schwab_1m_v2", "live:orb"):
        episodes.extend(
            [
                _episode(
                    day=dcoy_day,
                    symbol="DCOY",
                    account=account,
                    segment="dcoy-1",
                    entry_hm=(10, 53),
                    exit_hm=(10, 55),
                    entry="5.79",
                    exit="5.61",
                    line="5.7912",
                    reason="CONFIRMATION_EXIT",
                ),
                _episode(
                    day=dcoy_day,
                    symbol="DCOY",
                    account=account,
                    segment="dcoy-2",
                    entry_hm=(11, 3),
                    exit_hm=(11, 5),
                    entry="5.79",
                    exit="5.68",
                    line="5.7912",
                    reason="CONFIRMATION_EXIT",
                ),
                _episode(
                    day=dcoy_day,
                    symbol="DCOY",
                    account=account,
                    segment="dcoy-3",
                    entry_hm=(11, 52),
                    exit_hm=(12, 1),
                    entry="5.33",
                    exit="4.90",
                    line="5.3309",
                    reason="OCO_RESOLVED_FLAT",
                ),
            ]
        )
    managed = tuple(item[0] for item in episodes)
    fills = tuple(value for item in episodes for value in item[1:])
    bars = {
        (vsa_day, "VSA"): (
            _bar(vsa_day, 15, 31, high="3.71", low="3.55", close="3.70"),
            _bar(vsa_day, 15, 50, high="3.86", low="3.40", close="3.84"),
            _bar(vsa_day, 15, 51, high="4.04", low="3.75", close="3.95"),
            _bar(vsa_day, 15, 53, high="4.43", low="3.90", close="4.22"),
        ),
        (dcoy_day, "DCOY"): (
            _bar(dcoy_day, 10, 56, high="5.60", low="5.40", close="5.50"),
            _bar(dcoy_day, 11, 3, high="5.81", low="5.50", close="5.79"),
            _bar(dcoy_day, 11, 6, high="5.30", low="5.10", close="5.20"),
            _bar(dcoy_day, 11, 52, high="5.34", low="5.20", close="5.33"),
        ),
    }
    return StudyInput(managed_rows=managed, fills=fills, bars=bars)


def test_vsa_hard_stop_gets_one_fresh_cross_retry_on_both_brokers() -> None:
    report = evaluate(_population(), date(2026, 8, 24), date(2026, 9, 23))

    assert report.vsa_replay["logical_trips"] == 2
    retry = report.vsa_replay["trips"][1]
    assert retry["source"] == "modeled"
    assert retry["fresh_cross_at_et"].startswith("2026-09-23T15:50")
    assert {leg["account"] for leg in retry["legs"]} == {
        "live:schwab_1m_v2",
        "live:orb",
    }
    assert {leg["exit_reason"] for leg in retry["legs"]} == {"TARGET"}


def test_dcoy_one_retry_keeps_two_trips_and_drops_the_third() -> None:
    report = evaluate(_population(), date(2026, 8, 24), date(2026, 9, 23))

    assert report.dcoy_replay["logical_trips"] == 3
    assert report.dcoy_replay["trips"][1]["fresh_cross_at_et"].startswith(
        "2026-09-22T11:03"
    )
    max_one = next(row for row in report.variants if row.max_retries == 1)
    max_two = next(row for row in report.variants if row.max_retries == 2)
    assert max_two.logical_trips == max_one.logical_trips + 1
    assert max_two.broker_trades == max_one.broker_trades + 2


def test_fresh_cross_requires_a_completed_below_line_bar_first() -> None:
    day = date(2026, 9, 23)
    after = _at(day, 15, 30)
    line = Decimal("3.859")
    bars = (
        _bar(day, 15, 31, high="3.90", low="3.70", close="3.88"),
        _bar(day, 15, 32, high="3.95", low="3.75", close="3.90"),
    )
    assert _fresh_cross(bars, after=after, line=line) is None


def test_same_bar_target_and_stop_is_scored_as_stop() -> None:
    day = date(2026, 9, 23)
    bars = (
        _bar(day, 15, 31, high="9.90", low="9.70", close="9.80"),
        _bar(day, 15, 32, high="10.10", low="9.80", close="10.00"),
        _bar(day, 15, 33, high="10.60", low="9.10", close="10.20"),
    )
    trip = _model_counterfactual(
        bars,
        day=day,
        symbol="BOTH",
        line=Decimal("10"),
        after=_at(day, 15, 30),
        index=2,
    )
    assert trip is not None
    assert {leg.exit_reason for leg in trip.legs} == {"STOP"}
    assert {leg.return_pct for leg in trip.legs} == {Decimal("-8.00")}

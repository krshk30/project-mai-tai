from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from scripts.b11_rest_provenance_study import (
    Placement,
    Probe,
    classify_place,
    match_place,
    overlapping_sessions,
    pair_fills,
    parse_logs,
)


def _at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 9, 25, hour, minute, second, tzinfo=UTC)


def _probes(start: datetime, *, historical: bool = False) -> tuple[Probe, ...]:
    result = []
    for age in range(4):
        emitted = start + timedelta(minutes=age + 1, seconds=2)
        bar = start + timedelta(minutes=age)
        if historical:
            bar -= timedelta(hours=1)
        result.append(Probe(emitted, bar, Decimal("4.803022"), "short", age,
                            "SELL" if age == 0 else "none", age + 1))
    return tuple(result)


def _place(probes: tuple[Probe, ...]) -> Placement:
    return Placement(
        symbol="INLF", at=probes[-1].emitted, slot="first",
        line=Decimal("4.8030"), trigger=Decimal("4.8270"),
        kind="PLACE", source_line=5, short_segment=probes,
        latest_warm=_at(13, 30), latest_seed=None,
        watch_sample=_at(13, 31), segment_id="1790343422322",
        slot_id="8fd6d7e6-01ff-5878-a361-4672b254f9cb",
    )


def test_live_rest_is_classified_from_short_bars_not_later_arm() -> None:
    place = _place(_probes(_at(13, 34)))
    windows = {("2026-09-25", "INLF"): [(_at(13, 29), None)]}
    assert classify_place(place, windows) == (
        "LIVE", "", "live_short_and_trail_after_warmup"
    )


def test_readd_rest_is_rebuilt_even_when_placement_is_later() -> None:
    place = _place(_probes(_at(13, 34), historical=True))
    windows = {("2026-09-25", "INLF"): [
        (_at(12, 0), _at(13, 35)), (_at(13, 37), None),
    ]}
    assert classify_place(place, windows) == (
        "REBUILT", "same_session_readd", "historical_short_or_trail_bar"
    )


def test_missing_warmup_stays_unknown() -> None:
    place = _place(_probes(_at(13, 34)))
    place = Placement(**{**place.__dict__, "latest_warm": None})
    windows = {("2026-09-25", "INLF"): [(_at(13, 29), None)]}
    assert classify_place(place, windows)[0] == "UNKNOWN"


def test_exact_slot_and_trigger_join_delayed_webull_intent() -> None:
    place = _place(_probes(_at(13, 34)))
    fill = {
        "symbol": "INLF", "intent_at": _at(13, 42).isoformat(),
        "filled_at": _at(13, 44).isoformat(),
        "intent_payload": json.dumps({"metadata": {
            "cw_entry_slot": "first", "entry_price": "4.8270",
            "fanout_segment_id": place.segment_id,
            "fanout_slot_id": place.slot_id,
        }}),
    }
    assert match_place(fill, [place]) == (place, "exact_trigger_identity_delayed")
    wrong = Placement(**{**place.__dict__, "slot_id": "00000000-0000-0000-0000-000000000000"})
    assert match_place(fill, [wrong])[0] is None


def test_duplicate_delayed_placements_stay_unknown() -> None:
    place = _place(_probes(_at(13, 34)))
    later = Placement(**{**place.__dict__, "at": _at(13, 39)})
    fill = {
        "symbol": "INLF", "intent_at": _at(13, 43).isoformat(),
        "filled_at": _at(13, 44).isoformat(),
        "intent_payload": json.dumps({"metadata": {
            "cw_entry_slot": "first", "entry_price": "4.8270",
            "fanout_segment_id": place.segment_id,
            "fanout_slot_id": place.slot_id,
        }}),
    }
    assert match_place(fill, [place, later])[0] is None


def test_legacy_rest_log_is_retained_but_without_probe_is_unknown(tmp_path) -> None:
    log = tmp_path / "schwab-1m-v2.log"
    log.write_text(
        "2026-09-25 13:37:02,336 INFO [V2-RESTING-PLACE] INLF slot=first "
        "stop=4.8270 limit=4.8512 (band 0.50%)\n",
        encoding="utf-8",
    )
    places, counts = parse_logs([log], {"INLF"}, _at(14, 0))
    assert counts["placements"] == 1
    assert places[0].trigger == Decimal("4.8270")
    assert classify_place(places[0], {})[0] == "UNKNOWN"


def test_fill_pairing_does_not_borrow_next_session_sell() -> None:
    fills = [
        {"fill_id": "buy", "account": "live:orb", "symbol": "INLF", "side": "buy",
         "quantity": "1", "price": "4", "filled_at": "2026-09-25T19:00:00Z"},
        {"fill_id": "sell", "account": "live:orb", "symbol": "INLF", "side": "sell",
         "quantity": "1", "price": "5", "filled_at": "2026-09-26T13:00:00Z"},
    ]
    assert pair_fills(fills) == {}


def test_overlapping_buys_do_not_receive_fifo_outcomes() -> None:
    fills = [
        {"fill_id": "buy1", "account": "live:orb", "symbol": "INLF", "side": "buy",
         "quantity": "1", "price": "4", "filled_at": "2026-09-25T14:00:00Z"},
        {"fill_id": "buy2", "account": "live:orb", "symbol": "INLF", "side": "buy",
         "quantity": "1", "price": "5", "filled_at": "2026-09-25T14:01:00Z"},
        {"fill_id": "sell", "account": "live:orb", "symbol": "INLF", "side": "sell",
         "quantity": "2", "price": "6", "filled_at": "2026-09-25T14:02:00Z"},
    ]
    assert len(overlapping_sessions(fills)) == 1
    assert pair_fills(fills) == {}

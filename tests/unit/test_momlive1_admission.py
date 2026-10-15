from dataclasses import replace
from itertools import permutations
import inspect
import textwrap
from datetime import datetime
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Route, Signal, price_entry, select_route
from project_mai_tai.momentum_live import admission as runtime


def signal(symbol="SCNI", **fields):
    return replace(Signal("2026-10-14", symbol, "30:source", 100, 200, D("3"), 10, 50_000), **fields)


def ledger():
    return EpisodeLedger.restore([])


@pytest.mark.parametrize("hour,route", [(4, Route.WEBULL), (6, Route.WEBULL), (7, Route.SCHWAB), (15, Route.SCHWAB)])
def test_us_sec_classification_hours(hour, route):
    result = select_route(datetime(2026, 10, 14, hour, tzinfo=ZoneInfo("America/New_York")),
                          market_open=True, is_us=True)
    assert result.route == route
    assert result.reason in {"sec_us", "all_before_0700"}


@pytest.mark.parametrize("hour", [3, 16, 20])
def test_closed_entry_hours(hour):
    with pytest.raises(Refusal, match="window_closed"):
        select_route(datetime(2026, 10, 14, hour, tzinfo=ZoneInfo("America/New_York")),
                     market_open=True, is_us=None)


@pytest.mark.parametrize("is_us,reason", [(False, "sec_non_us"), (None, "sec_unknown_at_decision")])
def test_non_us_or_unknown_upfront_uses_webull(is_us, reason):
    result = select_route(datetime(2026, 10, 14, 13, tzinfo=ZoneInfo("America/New_York")),
                          market_open=True, is_us=is_us)
    assert result.route == Route.WEBULL and result.reason == reason


@pytest.mark.parametrize("age,accepted", [(0, True), (2_000_000_000, True), (2_000_000_001, False), (-1, False)])
def test_quote_boundaries(age, accepted):
    args = dict(quote_ns=10_000_000_000-age, now_ns=10_000_000_000, route=Route.SCHWAB, tick=D(".01"))
    if accepted:
        assert price_entry(D("3"), D("3.001"), **args).limit == D("3.01")
    else:
        with pytest.raises(Refusal):
            price_entry(D("3"), D("3.001"), **args)


def test_wire_rounding_cap_and_actual_limit_quantity():
    result = price_entry(D("3"), D("3.001"), quote_ns=10, now_ns=10, route=Route.SCHWAB, tick=D(".01"))
    assert result.quantity == 33 and result.limit >= D("3.001") and result.limit <= result.cap
    with pytest.raises(Refusal, match="above_cap"):
        price_entry(D("3"), D("3.044"), quote_ns=10, now_ns=10, route=Route.SCHWAB, tick=D(".01"))
    assert price_entry(D("200"), D("200"), quote_ns=10, now_ns=10, route=Route.SCHWAB, tick=D(".01")).quantity == 1
    assert price_entry(D(".01"), D(".01"), quote_ns=10, now_ns=10, route=Route.WEBULL, tick=D(".0001")).quantity == 10000


@pytest.mark.parametrize("volume", [None, 49_999])
def test_volume_refusal(volume):
    with pytest.raises(Refusal, match="day_volume"):
        ledger().claim(signal(day_volume=volume), now_ns=10)


def test_atomic_30_60_overlapping_move_and_durable_duplicate():
    book = ledger()
    first, fresh = book.claim(signal(), now_ns=10)
    second, duplicate = book.claim(signal(source_id="60:source", interval_start_ms=50), now_ns=11)
    assert fresh and not duplicate and first is second and first.start_ms == 50
    restored = EpisodeLedger.restore(book.snapshot())
    assert restored.claim(signal(), now_ns=12)[0].episode_id == first.episode_id
    assert not restored.claim(signal(), now_ns=12)[1]


def test_pending_unknown_partial_and_cancel_occupy_slots():
    book = ledger()
    one, _ = book.claim(signal(), now_ns=10)
    two, _ = book.claim(signal("DLXY"), now_ns=10)
    book.begin_submit(one.episode_id, Route.SCHWAB, now_ns=10)
    book.reconcile(one.episode_id, cumulative_filled=D(1), cumulative_exited=D(0),
                   now_ns=20, terminal_entry=False, first_fill_ns=15)
    assert book.cancel_due(one.episode_id, now_ns=2_000_000_010)
    assert not book.cancel_due(one.episode_id, now_ns=2_000_000_011)
    book.reconcile(two.episode_id, cumulative_filled=D(0), cumulative_exited=D(0), now_ns=20, terminal_entry=False)
    with pytest.raises(Refusal, match="reservations_full"):
        book.claim(signal("THIRD"), now_ns=30)
    restored = EpisodeLedger.restore(book.snapshot())
    with pytest.raises(Refusal, match="reservations_full"):
        restored.claim(signal("THIRD"), now_ns=30)


def test_late_fill_after_cancel_never_reissues_cancel_and_timer_uses_first_fill():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.begin_submit(episode.episode_id, Route.SCHWAB, now_ns=10)
    assert book.cancel_due(episode.episode_id, now_ns=2_000_000_010)
    book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(0),
                   now_ns=3_000_000_000, terminal_entry=False, first_fill_ns=1_000_000_000)
    assert not book.cancel_due(episode.episode_id, now_ns=4_000_000_000)
    assert not book.time_exit_due(episode.episode_id, now_ns=300_999_999_999)
    assert book.time_exit_due(episode.episode_id, now_ns=301_000_000_000)
    book.reconcile(episode.episode_id, cumulative_filled=D(2), cumulative_exited=D(0),
                   now_ns=302_000_000_000, terminal_entry=True)
    assert episode.first_fill_ns == 1_000_000_000


@pytest.mark.parametrize("proof,terminal,age,filled", [(False, True, 1, 0), (True, False, 1, 0),
    (True, True, 2_000_000_000, 0), (True, True, -1, 0), (True, True, 1, 1), (True, True, 1, 0)])
def test_legacy_fallback_is_always_disabled_even_authoritative_zero_fill(proof, terminal, age, filled):
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.begin_submit(episode.episode_id, Route.SCHWAB, now_ns=10)
    episode.filled = D(filled)
    with pytest.raises(Refusal, match="fallback"):
        book.claim_fallback(episode.episode_id, now_ns=10+age, proven_ineligible=proof,
                            proven_terminal_zero_fill=terminal)


def test_disabled_fallback_keeps_original_broker_deadline_and_ownership():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.begin_submit(episode.episode_id, Route.SCHWAB, now_ns=10)
    before = book.snapshot()
    with pytest.raises(Refusal, match="disabled_operator"):
        book.claim_fallback(episode.episode_id, now_ns=11, proven_ineligible=True, proven_terminal_zero_fill=True)
    assert book.snapshot() == before and episode.route == Route.SCHWAB


def test_cooldown_only_after_proved_exit_and_quantity_never_negative():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(1),
                   now_ns=100, terminal_entry=True, first_fill_ns=20)
    with pytest.raises(Refusal, match="cooldown"):
        book.claim(signal(source_id="next", interval_start_ms=300, interval_end_ms=400), now_ns=30_000_000_099)
    assert book.claim(signal(source_id="next", interval_start_ms=300, interval_end_ms=400), now_ns=30_000_000_100)[1]
    with pytest.raises(Refusal, match="reconciliation"):
        book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(2), now_ns=200, terminal_entry=True)


def test_foreign_owner_and_not_restored_fail_closed():
    with pytest.raises(Refusal, match="restore"):
        EpisodeLedger().claim(signal(), now_ns=10)
    with pytest.raises(Refusal, match="foreign"):
        ledger().claim(signal(), now_ns=10, foreign_owner=True)


@pytest.mark.parametrize("current_day", ["2026-10-14", "2026-10-15"])
def test_restore_all_orders_active_symbol_reservation_is_session_independent(current_day):
    original = ledger()
    old, _ = original.claim(signal(source_id="old", interval_start_ms=10, interval_end_ms=20), now_ns=10)
    original.reconcile(old.episode_id, cumulative_filled=D(0), cumulative_exited=D(0), now_ns=20, terminal_entry=True)
    active, _ = original.claim(signal(source_id="active", interval_start_ms=100, interval_end_ms=200), now_ns=30)
    later_closed = dict(original.snapshot()[0], episode_id="third-history", source_ids=["later-history"], start_ms=300, end_ms=400)
    rows = [*original.snapshot(), later_closed]
    for order in permutations(rows):
        restored = EpisodeLedger.restore(list(order))
        assert restored._active_count == 1 and restored.episodes[active.episode_id].state == "claimed"
        with pytest.raises(Refusal, match="symbol_episode_pending_or_held"):
            restored.claim(signal(session_date=current_day, source_id="new", interval_start_ms=500, interval_end_ms=600), now_ns=40)
        assert restored._active_count == 1 and len(restored.episodes) == 3
        assert restored.claim(signal(source_id="active", interval_start_ms=100, interval_end_ms=200), now_ns=40)[0].episode_id == active.episode_id
        assert not restored.claim(signal(source_id="old", interval_start_ms=10, interval_end_ms=20), now_ns=40)[1]
        merged, fresh = restored.claim(signal(source_id="60-active", interval_start_ms=110, interval_end_ms=190), now_ns=40)
        assert merged.episode_id == active.episode_id and not fresh


def test_restore_keeps_multiple_actual_owned_exposures_without_admitting_third():
    original = ledger()
    a, _ = original.claim(signal(), now_ns=10)
    b, _ = original.claim(signal("DLXY"), now_ns=10)
    rows = original.snapshot()
    rows[1]["symbol"] = "SCNI"
    rows[1]["source_ids"] = ["second-owned"]
    rows[1]["start_ms"], rows[1]["end_ms"] = 300, 400
    for order in permutations(rows):
        restored = EpisodeLedger.restore(list(order))
        assert set(restored._active_by_symbol["SCNI"]) == {a.episode_id, b.episode_id}
        assert restored._active_count == 2
        with pytest.raises(Refusal, match="symbol_episode_pending_or_held"):
            restored.claim(signal(source_id="third", interval_start_ms=500, interval_end_ms=600), now_ns=20)


def test_earliest_trusted_fill_corrects_300s_deadline_across_restart():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(0),
                   now_ns=40_000_000_000, terminal_entry=False, first_fill_ns=30_000_000_000)
    book.reconcile(episode.episode_id, cumulative_filled=D(2), cumulative_exited=D(0),
                   now_ns=50_000_000_000, terminal_entry=True, first_fill_ns=20_000_000_000)
    assert episode.first_fill_ns == 20_000_000_000
    assert not book.time_exit_due(episode.episode_id, now_ns=319_999_999_999)
    assert book.time_exit_due(episode.episode_id, now_ns=320_000_000_000)
    restored = EpisodeLedger.restore(book.snapshot())
    assert restored.time_exit_due(episode.episode_id, now_ns=320_000_000_000)
    restored.reconcile(episode.episode_id, cumulative_filled=D(3), cumulative_exited=D(0),
        now_ns=60_000_000_000, terminal_entry=True, first_fill_ns=40_000_000_000)
    assert restored.episodes[episode.episode_id].first_fill_ns == 20_000_000_000


def test_unknown_first_send_deadline_survives_restore_without_false_first_proof():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.begin_submit(episode.episode_id, Route.WEBULL, now_ns=1_000_000_000)
    book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(0),
        now_ns=4_000_000_000, terminal_entry=True, first_fill_ns=None)
    restored = EpisodeLedger.restore(book.snapshot())
    assert restored.episodes[episode.episode_id].first_fill_ns is None
    assert not restored.time_exit_due(episode.episode_id, now_ns=300_999_999_999)
    assert restored.time_exit_due(episode.episode_id, now_ns=301_000_000_000)


@pytest.mark.parametrize("invalid", [0, -1, 51_000_000_000])
def test_later_first_fill_proof_invalid_does_not_change_quantity_or_timer(invalid):
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    book.reconcile(episode.episode_id, cumulative_filled=D(1), cumulative_exited=D(0),
        now_ns=40_000_000_000, terminal_entry=False, first_fill_ns=30_000_000_000)
    before = book.snapshot()
    with pytest.raises(Refusal, match="first_fill_time"):
        book.reconcile(episode.episode_id, cumulative_filled=D(2), cumulative_exited=D(0),
            now_ns=50_000_000_000, terminal_entry=True, first_fill_ns=invalid)
    assert book.snapshot() == before


def test_restore_active_index_mutation_is_caught(monkeypatch):
    source = textwrap.dedent(inspect.getsource(EpisodeLedger.restore))
    guard = ('        if episode.state in ACTIVE_STATES:\n'
             '            ledger._active_by_symbol.setdefault(episode.symbol, set()).add(episode.episode_id)\n')
    assert guard in source
    namespace = dict(runtime.__dict__)
    exec(compile(source.replace(guard, ""), "<momlive-no-active-index>", "exec"), namespace)
    monkeypatch.setattr(EpisodeLedger, "restore", namespace["restore"])
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        test_restore_all_orders_active_symbol_reservation_is_session_independent("2026-10-14")


def test_earliest_fill_minimum_mutation_is_caught(monkeypatch):
    source = textwrap.dedent(inspect.getsource(EpisodeLedger.reconcile))
    expression = "min(episode.first_fill_ns, first_fill_ns)"
    assert expression in source
    namespace = dict(runtime.__dict__)
    exec(compile(source.replace(expression, "episode.first_fill_ns"), "<momlive-ignore-earlier-fill>", "exec"), namespace)
    monkeypatch.setattr(EpisodeLedger, "reconcile", namespace["reconcile"])
    with pytest.raises(AssertionError):
        test_earliest_trusted_fill_corrects_300s_deadline_across_restart()

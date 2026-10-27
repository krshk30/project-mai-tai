from dataclasses import replace

import pytest

from project_mai_tai.brokerdecides1 import (
    AdmissionMemory, NativeAccountBook, NativeBookPublisher, assess, decode, encode,
)

ACCOUNTS = ("live:schwab", "live:webull")


def book(account, provider, **updates):
    return replace(NativeAccountBook(account, provider, 1000, 1100,
        frozenset(), frozenset(), True, "complete"), **updates)


def snapshot(**updates):
    publisher = NativeBookPublisher(epoch="boot-one", accounts=ACCOUNTS)
    generation = publisher.begin(1000)
    args = dict(completed_ms=1200, books=(book(ACCOUNTS[0], "schwab"),
        book(ACCOUNTS[1], "webull")), inflight=(), filled_segments=(),
        local_complete=True, reason="complete")
    args.update(updates)
    return publisher.finish(generation, **args)


def decision(value, **updates):
    args = dict(accounts=ACCOUNTS, symbol="WORX", segment=900,
        now_ms=1300, max_age_ms=500)
    args.update(updates)
    return assess(value, **args)


def test_complete_both_accounts_flat_admits():
    assert decision(snapshot()).allowed


@pytest.mark.parametrize("account_index", [0, 1])
@pytest.mark.parametrize("field", ["working", "positions"])
def test_manual_or_other_strategy_any_account_blocks(account_index, field):
    value = snapshot()
    books = list(value.books)
    books[account_index] = replace(books[account_index], **{field: frozenset({"WORX"})})
    assert not decision(replace(value, books=tuple(books))).allowed


@pytest.mark.parametrize("changes", [
    {"books": ()}, {"local_complete": False},
    {"inflight": ("WORX",)}, {"filled_segments": (("WORX", 900),)},
])
def test_unknown_inflight_or_same_segment_fill_blocks(changes):
    assert not decision(snapshot(**changes)).allowed


@pytest.mark.parametrize("age", [0, -1, 299])
def test_unreviewed_or_stale_age_holds(age):
    assert not decision(snapshot(), max_age_ms=age).allowed


@pytest.mark.parametrize("segment", [0, -1, None, True])
def test_missing_segment_cannot_clear_buy_fill_gate(segment):
    assert not decision(snapshot(), segment=segment).allowed


def test_new_segment_does_not_erase_old_fill_but_can_admit():
    assert decision(snapshot(filled_segments=(("WORX", 800),))).allowed


def test_incomplete_or_future_native_read_holds():
    value = snapshot()
    for updates in ({"complete": False}, {"wire_completed_ms": 1500}):
        assert not decision(replace(value, books=(
            replace(value.books[0], **updates), value.books[1]))).allowed


def test_new_pass_revokes_old_negative_and_superseded_result():
    publisher = NativeBookPublisher(epoch="boot", accounts=ACCOUNTS)
    first = publisher.begin(1000)
    publisher.begin(1100)
    assert not decision(publisher.snapshot).allowed
    with pytest.raises(ValueError, match="superseded"):
        publisher.finish(first, completed_ms=1200, books=(), inflight=(),
            filled_segments=(), local_complete=True, reason="old")


def test_lost_ack_reservation_survives_later_flat_snapshot():
    memory = AdmissionMemory()
    memory.apply(snapshot())
    memory.reserve("WORX", "exact-order-token")
    memory.apply(snapshot())
    args = dict(accounts=ACCOUNTS, symbol="WORX", segment=900,
        now_ms=1300, max_age_ms=500)
    assert not memory.assess(**args).allowed
    assert not memory.terminal("WORX", "foreign", proven=True)
    assert not memory.terminal("WORX", "exact-order-token", proven=False)
    assert not memory.assess(**args).allowed
    assert memory.terminal("WORX", "exact-order-token", proven=True)
    assert memory.assess(**args).allowed


def test_regressing_generation_refused():
    memory = AdmissionMemory()
    value = snapshot()
    memory.apply(replace(value, generation=2))
    with pytest.raises(ValueError, match="regressed"):
        memory.apply(value)


def test_symbol_bound_does_not_drop_overflow_to_flat():
    with pytest.raises(ValueError):
        snapshot(inflight=(f"S{n}" for n in range(4097)))


def test_packet_roundtrip_preserves_exact_evidence():
    value = snapshot(inflight=("OLOX",), filled_segments=(("AAME", 700),))
    assert decode(encode(value)) == value


@pytest.mark.parametrize("raw", [
    '{"schema":1,"schema":1}', '{"schema":true}', '{}', 'null',
    '{"schema":1', '[]', 'x' * (512 * 1024 + 1),
])
def test_invalid_or_oversize_packet_rejected(raw):
    with pytest.raises((ValueError, TypeError)):
        decode(raw)


def test_duplicate_symbol_and_unknown_packet_field_rejected():
    import json

    for key, value in (("inflight", ["WORX", "WORX"]), ("unexpected", "value")):
        payload = json.loads(encode(snapshot()))
        payload[key] = value
        with pytest.raises((ValueError, TypeError)):
            decode(json.dumps(payload))

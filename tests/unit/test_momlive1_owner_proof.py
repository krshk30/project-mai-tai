import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from project_mai_tai.momentum_live.admission import Refusal
from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher, current_process_identity, owner_proof, replace_quote_owner
from tests.unit.test_momlive1_admission import ledger, signal


ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def test_exact_owner_schema_pending_flag_off_keeps_quotes():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    proof = owner_proof(book, observed_ns=20, producer_instance_id="123:456", account_names=ACCOUNTS,
                        bindings={episode.episode_id: ACCOUNTS[0]}, entry_admission_enabled=False)
    assert proof["version"] == 1 and proof["source_service"] == "oms"
    assert proof["producer_instance_id"] == "123:456" and proof["strategy_code"] == "momentum_warn_live"
    assert proof["restore_ready"] and proof["entry_admission_enabled"] is False
    assert proof["account_names"] == sorted(ACCOUNTS)
    assert proof["quote_symbols"] == ["SCNI"]
    assert proof["episodes"] == [{"episode_id": episode.episode_id, "symbol": "SCNI",
        "account_name": ACCOUNTS[0], "state": "claimed", "filled_quantity": "0", "exited_quantity": "0"}]


def test_empty_ready_proof_is_explicit_and_unready_not_falsely_healthy():
    book = ledger()
    proof = owner_proof(book, observed_ns=20, producer_instance_id="123:456", account_names=ACCOUNTS,
                        bindings={}, entry_admission_enabled=True)
    assert proof["episodes"] == proof["quote_symbols"] == [] and proof["restore_ready"]
    book.ready = False
    assert owner_proof(book, observed_ns=20, producer_instance_id="123:456", account_names=ACCOUNTS,
                       bindings={}, entry_admission_enabled=False)["restore_ready"] is False


def test_unknown_episode_account_cannot_be_proof():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    with pytest.raises(Refusal, match="episode_owner"):
        owner_proof(book, observed_ns=20, producer_instance_id="123:456", account_names=ACCOUNTS,
                    bindings={episode.episode_id: "manual:other"}, entry_admission_enabled=True)


@pytest.mark.parametrize("identity", ["hostname", "123", "123:bad"])
def test_proof_requires_process_start_identity(identity):
    with pytest.raises(Refusal, match="producer"):
        owner_proof(ledger(), observed_ns=20, producer_instance_id=identity, account_names=ACCOUNTS,
                    bindings={}, entry_admission_enabled=True)


def publisher(book, *, enabled=True, bindings=None):
    redis = AsyncMock()
    replace = AsyncMock()
    result = OwnerProofPublisher(redis, "mai_tai", book, producer_instance_id="123:456",
        account_names=ACCOUNTS, bindings=bindings or {}, enabled=lambda: enabled,
        replace_owner=replace, state_lock=asyncio.Lock(), clock_ns=lambda: 100)
    return result, redis, replace


@pytest.mark.asyncio
async def test_initial_restored_empty_owner_is_replaced_before_exact_stream_proof():
    result, redis, replace = publisher(ledger())
    calls = []
    replace.side_effect = lambda symbols: calls.append(("owner", symbols))
    redis.xadd.side_effect = lambda *args, **kwargs: calls.append(("proof", args, kwargs))
    await result.publish_once()
    assert calls[0] == ("owner", set())
    assert calls[1][1][0] == "mai_tai:momentum-live-owner-proof"
    proof = json.loads(calls[1][1][1]["data"])
    assert proof["restore_ready"] and proof["entry_admission_enabled"]
    assert proof["episodes"] == proof["quote_symbols"] == []
    assert calls[1][2] == {"maxlen": 64, "approximate": False}
    assert result.last_success_ns == 100


@pytest.mark.asyncio
async def test_flag_off_pending_owner_still_registers_quote_union():
    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    result, redis, replace = publisher(book, enabled=False, bindings={episode.episode_id: ACCOUNTS[1]})
    await result.publish_once()
    replace.assert_awaited_once_with({"SCNI"})
    proof = json.loads(redis.xadd.await_args.args[1]["data"])
    assert proof["entry_admission_enabled"] is False
    assert proof["episodes"][0]["filled_quantity"] == "0"


@pytest.mark.asyncio
async def test_unrestored_book_never_updates_owner_or_publishes():
    book = ledger()
    book.ready = False
    result, redis, replace = publisher(book)
    with pytest.raises(Refusal, match="restore_not_ready"):
        await result.publish_once()
    redis.xadd.assert_not_awaited()
    replace.assert_not_awaited()
    assert result.last_success_ns is None


@pytest.mark.asyncio
async def test_owner_registration_failure_cannot_publish_fresh_proof():
    result, redis, replace = publisher(ledger())
    replace.side_effect = OSError("redis unavailable")
    with pytest.raises(OSError):
        await result.publish_once()
    redis.xadd.assert_not_awaited()
    assert result.last_success_ns is None


@pytest.mark.asyncio
async def test_cancelled_publication_does_not_mark_success_and_releases_lock():
    result, redis, replace = publisher(ledger())
    replace.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await result.publish_once()
    assert not result.state_lock.locked()
    assert result.last_success_ns is None
    redis.xadd.assert_not_awaited()


def test_process_identity_uses_actual_start_ticks_not_parenthesized_comm(monkeypatch):
    monkeypatch.setattr("project_mai_tai.momentum_live.owner_proof.os.getpid", lambda: 123)
    fields = ["S"] + ["0"] * 18 + ["456"] + ["0"] * 5
    monkeypatch.setattr("project_mai_tai.momentum_live.owner_proof.Path.read_text",
                        lambda *args, **kwargs: "123 (oms (worker)) " + " ".join(fields))
    assert current_process_identity() == "123:456"


@pytest.mark.asyncio
async def test_real_quote_owner_event_and_producer_are_accepted_by_frozen_main_guard():
    from tests.unit.test_momlive1_guard_owner_compat import COMPAT

    result, redis, _ = publisher(ledger())
    result.replace_owner = lambda symbols: replace_quote_owner(redis, "mai_tai", symbols, maxlen=1000)
    await result.publish_once()
    owner_event = json.loads(redis.xadd.await_args_list[0].args[1]["data"])
    proof = COMPAT.decode_proof(redis.xadd.await_args_list[1].args[1]["data"])
    assert owner_event["source_service"] == "oms"
    assert owner_event["payload"] == {"consumer_name": "momentum-live", "mode": "replace", "symbols": []}
    assert COMPAT.validate_live_owner(set(), proof, enabled=True, now_ns=100,
        max_age_ns=30_819_000_000, expected_instance="123:456",
        expected_accounts=set(ACCOUNTS))["episode_count"] == 0


def test_main_successor_keeps_two_restored_owned_episodes_one_quote_symbol():
    from tests.unit.test_momlive1_guard_owner_compat import COMPAT

    book = ledger()
    episode, _ = book.claim(signal(), now_ns=10)
    rows = book.snapshot()
    second = dict(rows[0], episode_id="00000000-0000-0000-0000-000000000001", source_ids=["other-source"])
    restored = type(book).restore([rows[0], second])
    proof = owner_proof(restored, observed_ns=100, producer_instance_id="123:456", account_names=ACCOUNTS,
        bindings={episode.episode_id: ACCOUNTS[0], second["episode_id"]: ACCOUNTS[1]}, entry_admission_enabled=False)
    assert COMPAT.validate_live_owner({"SCNI"}, proof, enabled=False, now_ns=100,
        max_age_ns=30_819_000_000, expected_instance="123:456",
        expected_accounts=set(ACCOUNTS))["episode_count"] == 2


@pytest.mark.parametrize("symbols", [{"SCNI", "DLXY", "GLND"}, {"bad"}])
@pytest.mark.asyncio
async def test_new_owner_request_never_widens_union_or_uses_unvalidated_symbol(symbols):
    redis = AsyncMock()
    with pytest.raises(Refusal, match="scope_invalid"):
        await replace_quote_owner(redis, "mai_tai", symbols, maxlen=1000)
    redis.xadd.assert_not_awaited()

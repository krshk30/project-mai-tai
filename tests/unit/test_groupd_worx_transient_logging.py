"""D4 logging-only source controls; no historical raw quantities reconstructed."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import logging

import pytest

from project_mai_tai.momentum_live.actor import OBSERVATION_POLL_NS, logger
from project_mai_tai.momentum_live.admission import Refusal
from tests.unit.test_momlive1_actor import setup


@pytest.fixture
def owned_logs(caplog):
    old_level, old_propagate = logger.level, logger.propagate
    added = caplog.handler not in logger.handlers
    logger.setLevel(logging.INFO)
    logger.propagate = False  # The same caplog handler may also be on root.
    if added:
        logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        if added:
            logger.removeHandler(caplog.handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def mismatch_actor():
    actor, ports, episode, position = setup(filled=3, terminal=True)
    actor.restored = True
    actor._refresh_owners()
    ports.diagnostic_account = lambda: position.account
    clock = [3_000_000_000]
    ports.clock_ns = lambda: clock[0]
    ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal(1), owner_provider="schwab")
    return actor, ports, episode, position, clock


def log_records(caplog):
    return [record for record in caplog.records if "[MOMLIVE1-OWNED]" in record.getMessage()]


@pytest.mark.asyncio
async def test_worx_quantity_race_info_once_then_error_after_two_owned_polls(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    await actor._service_owned(episode.episode_id)
    first = actor._quantity_mismatch_since[episode.episode_id]
    for offset in (100_000_000, 440_000_000, 1_000_000_000, 2*OBSERVATION_POLL_NS):
        clock[0] = first+offset
        await actor._service_owned(episode.episode_id)
    assert [record.levelno for record in log_records(owned_logs)] == [logging.INFO]
    clock[0] += 1
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert actor._quantity_mismatch_since[episode.episode_id] == first
    assert episode.episode_id in actor.work.owners
    assert episode.filled == episode.exited == 0
    ports.save.assert_not_awaited()
    ports.protect_owned.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_quantity_changes_and_state_changes_do_not_extend_continuous_grace(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    await actor._service_owned(episode.episode_id)
    first = actor._quantity_mismatch_since[episode.episode_id]
    episode.state = "cancel_requested"
    actor._observations.clear()
    ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal(2))
    clock[0] = first+OBSERVATION_POLL_NS
    await actor._service_owned(episode.episode_id)
    assert [record.levelno for record in log_records(owned_logs)] == [logging.INFO, logging.INFO]
    actor._observations.clear()
    ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal("2.5"))
    clock[0] = first+2*OBSERVATION_POLL_NS+1
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert actor._quantity_mismatch_since[episode.episode_id] == first
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_actual_recovery_clears_clock_and_new_mismatch_starts_new_grace(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    await actor._service_owned(episode.episode_id)
    actor._observations.clear()
    ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal(0))
    await actor._service_owned(episode.episode_id)
    assert episode.episode_id not in actor._quantity_mismatch_since
    actor._observations.clear()
    clock[0] += 10*OBSERVATION_POLL_NS
    ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal(1))
    await actor._service_owned(episode.episode_id)
    assert actor._quantity_mismatch_since[episode.episode_id] == clock[0]
    assert log_records(owned_logs)[-1].levelno == logging.INFO


@pytest.mark.parametrize("case", ["symbol", "episode", "strategy", "webull", "account", "nonfinite", "overfill"])
@pytest.mark.asyncio
async def test_non_worx_identity_and_invalid_quantity_errors_remain_error(owned_logs, case):
    actor, ports, episode, position, clock = mismatch_actor()
    if case == "symbol":
        position.symbol = "FOREIGN"
    elif case == "episode":
        position.episode_id = "foreign"
    elif case == "strategy":
        position.strategy = "orb_schwab"
    elif case == "webull":
        ports.observe.return_value = replace(ports.observe.return_value, owner_provider="webull")
    elif case == "account":
        ports.diagnostic_account = lambda: "live:other"
    elif case == "nonfinite":
        position.quantity = Decimal("NaN")
    elif case == "overfill":
        ports.observe.return_value = replace(ports.observe.return_value, exited=Decimal(4))
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert episode.episode_id not in actor._quantity_mismatch_since
    ports.exit_owned.assert_not_awaited()


@pytest.mark.parametrize("failure", [Refusal("cancel_entry_broker_identity_UNKNOWN"),
    Refusal("owned_observation_identity_mismatch"), RuntimeError("DB failed"), OSError("transport failed")])
@pytest.mark.asyncio
async def test_xlab_and_other_sources_never_use_worx_exception_text_heuristic(owned_logs, failure):
    actor, ports, episode, position, clock = mismatch_actor()
    ports.observe.side_effect = failure
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert not actor._quantity_mismatch_since
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_still_raises_the_original_refusal_without_info_grace(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    actor.restored = False
    with pytest.raises(Refusal, match="owned_observation_identity_mismatch"):
        await actor.restore_before_serving()
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert not [record for record in log_records(owned_logs) if record.levelno == logging.INFO]
    assert not actor._quantity_mismatch_since
    assert not actor.restored


@pytest.mark.asyncio
async def test_optional_account_diagnostic_failure_preserves_original_refusal(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    def unavailable():
        raise RuntimeError("diagnostic account unavailable")
    ports.diagnostic_account = unavailable
    await actor._service_owned(episode.episode_id)
    error = log_records(owned_logs)[-1]
    assert error.levelno == logging.ERROR
    assert type(error.exc_info[1]) is Refusal
    assert str(error.exc_info[1]) == "owned_observation_identity_mismatch"
    assert episode.episode_id in actor.work.owners
    ports.save.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.parametrize("second", [None, True, 3.0, "unknown", 2_999_999_999,
    RuntimeError("diagnostic clock unavailable")])
@pytest.mark.asyncio
async def test_optional_exception_clock_failure_does_not_escape_restored_worker(owned_logs, second):
    actor, ports, episode, position, clock = mismatch_actor()
    # Cache the original observation so clock #1 is the original pre-observe
    # clock and #2 is ONLY the additional diagnostic calculation in except.
    actor._observations[episode.episode_id] = (clock[0], ports.observe.return_value)
    calls = []
    def diagnostic_clock():
        calls.append(True)
        if len(calls) == 1:
            return clock[0]
        if isinstance(second, Exception):
            raise second
        return second
    ports.clock_ns = diagnostic_clock
    await actor._service_owned(episode.episode_id)
    assert len(calls) == 2
    error = log_records(owned_logs)[-1]
    assert error.levelno == logging.ERROR
    assert type(error.exc_info[1]) is Refusal
    assert str(error.exc_info[1]) == "owned_observation_identity_mismatch"
    assert episode.episode_id in actor.work.owners
    assert actor._quantity_mismatch_since == {episode.episode_id: None}
    ports.save.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_episode_retirement_prunes_grace_and_backward_clock_does_not_restart_it(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    await actor._service_owned(episode.episode_id)
    first = actor._quantity_mismatch_since[episode.episode_id]
    clock[0] = first-1
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert actor._quantity_mismatch_since[episode.episode_id] == first
    actor.book._active_by_symbol.clear()
    actor._refresh_owners()
    assert not actor._quantity_mismatch_since and not actor._quantity_mismatch_states
    ports.exit_owned.assert_not_awaited()


@pytest.mark.parametrize("provider", ["schwab", "webull"])
@pytest.mark.parametrize("hour", [12, 15, 21], ids=["PM", "RTH", "AH"])
@pytest.mark.asyncio
async def test_session_and_provider_do_not_expand_log_only_worx_scope(owned_logs, provider, hour):
    actor, ports, episode, position, clock = mismatch_actor()
    clock[0] = int(datetime(2026, 10, 16, hour, tzinfo=UTC).timestamp()*1_000_000_000)
    ports.observe.return_value = replace(ports.observe.return_value, owner_provider=provider)
    # The mismatch is before reconciliation, save, cancel or exit regardless
    # of session. Only the independently bound Schwab case can get INFO.
    await actor._service_owned(episode.episode_id)
    assert [record.levelno for record in log_records(owned_logs)] == [
        logging.INFO if provider == "schwab" else logging.ERROR]
    assert episode.filled == episode.exited == 0
    ports.save.assert_not_awaited()
    ports.protect_owned.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.parametrize("missing", ["entry_coid", "entry_order_id"])
@pytest.mark.asyncio
async def test_missing_entry_binding_never_qualifies_for_info(owned_logs, missing):
    actor, ports, episode, position, _clock = mismatch_actor()
    setattr(position, missing, "")
    await actor._service_owned(episode.episode_id)
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    assert not actor._quantity_mismatch_since
    ports.save.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_first_clock_cannot_rearm_grace_without_actual_recovery(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    actor._observations[episode.episode_id] = (clock[0], ports.observe.return_value)
    values = iter([clock[0], None])
    ports.clock_ns = lambda: next(values)
    await actor._service_owned(episode.episode_id)
    assert actor._quantity_mismatch_since == {episode.episode_id: None}
    ports.clock_ns = lambda: clock[0]
    clock[0] += OBSERVATION_POLL_NS
    await actor._service_owned(episode.episode_id)
    assert [record.levelno for record in log_records(owned_logs)] == [logging.ERROR, logging.ERROR]
    assert actor._quantity_mismatch_since == {episode.episode_id: None}
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_diagnostic_cache_is_bounded_and_close_discards_only_log_state(owned_logs):
    actor, ports, episode, position, _clock = mismatch_actor()
    actor._quantity_mismatch_since = {"older-owned-a": 1, "older-owned-b": 1}
    actor._quantity_mismatch_states = {"older-owned-a": "held", "older-owned-b": "held"}
    await actor._service_owned(episode.episode_id)
    assert len(actor._quantity_mismatch_since) == len(actor._quantity_mismatch_states) == 2
    assert log_records(owned_logs)[-1].levelno == logging.ERROR
    await actor.close()
    assert not actor._quantity_mismatch_since and not actor._quantity_mismatch_states
    assert episode.filled == episode.exited == 0
    ports.save.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.parametrize("case", ["exact", "foreign_account", "webull", "lookup_error"])
def test_runtime_optional_provider_evidence_cannot_change_observation_path(case):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from project_mai_tai.momentum_live.runtime import OmsLiveRuntime

    runtime = object.__new__(OmsLiveRuntime)
    runtime.accounts = ("live:controlled-schwab", "live:controlled-webull")
    runtime.provider = Mock(return_value="webull" if case == "webull" else "schwab")
    if case == "lookup_error":
        runtime.provider.side_effect = RuntimeError("optional provider unavailable")
    account = "live:other" if case == "foreign_account" else runtime.accounts[0]
    position = SimpleNamespace(account=account)
    assert runtime._diagnostic_owner_provider(position) == (
        "schwab" if case == "exact" else "webull" if case == "webull" else None)
    assert runtime._diagnostic_owner_provider(None) is None
    if case == "foreign_account":
        runtime.provider.assert_not_called()


@pytest.mark.asyncio
async def test_optional_info_emission_failure_preserves_original_error_and_worker(owned_logs, monkeypatch):
    actor, ports, episode, position, _clock = mismatch_actor()

    def failed_info(*args, **kwargs):
        raise OSError("optional INFO handler unavailable")

    monkeypatch.setattr(logger, "info", failed_info)
    await actor._service_owned(episode.episode_id)
    error = log_records(owned_logs)[-1]
    assert error.levelno == logging.ERROR and type(error.exc_info[1]) is Refusal
    assert str(error.exc_info[1]) == "owned_observation_identity_mismatch"
    assert not actor._quantity_mismatch_states
    assert episode.episode_id in actor.work.owners
    ports.save.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()


@pytest.mark.asyncio
async def test_unknown_original_cache_clock_is_not_replaced_by_later_diagnostic_time(owned_logs):
    actor, ports, episode, position, clock = mismatch_actor()
    stamps = iter([clock[0], None, clock[0]])
    ports.clock_ns = lambda: next(stamps)
    await actor._service_owned(episode.episode_id)
    error = log_records(owned_logs)[-1]
    assert error.levelno == logging.ERROR and type(error.exc_info[1]) is Refusal
    assert str(error.exc_info[1]) == "owned_observation_identity_mismatch"
    assert actor._quantity_mismatch_since == {episode.episode_id: None}
    ports.save.assert_not_awaited()
    ports.cancel_entry_once.assert_not_awaited()
    ports.exit_owned.assert_not_awaited()

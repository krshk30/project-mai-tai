from datetime import UTC, datetime, timedelta

from project_mai_tai.market_halts import (
    HALT_MIN_PRINT_GAP,
    HALT_MIN_QUOTE_UPDATES,
    LiveHaltTracker,
    confirmed_halt_window,
    halt_is_confirmed,
)


def test_shared_halt_definition_requires_time_and_continuing_quotes() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    assert HALT_MIN_PRINT_GAP == timedelta(seconds=285)
    assert HALT_MIN_QUOTE_UPDATES == 2
    assert not halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=284),
        quote_updates=2,
    )
    assert not halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=285),
        quote_updates=1,
    )
    assert halt_is_confirmed(
        last_print_at=start,
        through_at=start + timedelta(seconds=285),
        quote_updates=2,
    )


def test_batch_and_live_paths_use_the_same_confirmation_function() -> None:
    start = datetime(2026, 8, 24, 15, 21, 14, tzinfo=UTC)
    reopen = start + timedelta(minutes=5)
    historical = confirmed_halt_window(
        last_print_at=start,
        reopen_print_at=reopen,
        quote_updates=2,
    )
    live = LiveHaltTracker()
    live.observe_print(start)
    live.observe_quote(start + timedelta(seconds=1))
    observation = live.observe_quote(start + timedelta(seconds=285))

    assert historical is not None
    assert observation.state == "CONFIRMED"
    assert observation.newly_confirmed is True
    assert live.observe_print(reopen) == historical

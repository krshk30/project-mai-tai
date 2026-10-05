"""Real recorded session gates remain correct after the host's window closes."""
from datetime import UTC, datetime, timedelta
import time

import pytest

from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from tests.unit import test_pmprint1_tonight_flags as recorded_fixture


@pytest.mark.asyncio
@pytest.mark.parametrize("ticket", recorded_fixture.TICKETS, ids=lambda row: row["id"][:8])
@pytest.mark.parametrize("timezone", ["UTC", "America/New_York"])
@pytest.mark.parametrize("host_time", ["2026-10-05T20:05:33+00:00", "2026-10-06T00:05:33+00:00"],
                         ids=["host-after1600", "host-after2000"])
async def test_recorded_restart_real_session_gates_ignore_host_after_window(monkeypatch, ticket, timezone, host_time):
    host = datetime.fromisoformat(host_time)
    expected = datetime.fromtimestamp((ticket["payload"].get("authorization") or {}).get(
        "at", ticket["payload"]["created_at"]), UTC) + timedelta(seconds=60)
    original_strategy = recorded_fixture.SchwabV2Strategy
    restored = []

    class HostClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return host.astimezone(tz or UTC)

    def capture_strategy(settings):
        result = original_strategy(settings)
        restored.append(result)
        return result

    try:
        with monkeypatch.context() as control:
            control.setenv("TZ", timezone)
            time.tzset()
            control.setattr(strategy_module, "datetime", HostClock)
            control.setattr(recorded_fixture, "SchwabV2Strategy", capture_strategy)
            # Preserve all original proven/unproven and both-leg assertions.
            await recorded_fixture.test_current_rpg_off_startup_restores_proof_dependent_ticket_ownership(control, ticket)
            restarted, = restored
            assert restarted._now_ms() == int(expected.timestamp() * 1000)
            assert restarted._resting_in_window()
            assert not restarted._resting_session_is_eh()
            assert not restarted._entry_window_closed_for_session()
            # The real production gates still reject the separate hostile time;
            # the fixture supplies recorded time, never constant True/False.
            assert not original_strategy._resting_in_window(restarted, host)
            assert original_strategy._resting_session_is_eh(restarted, host)
            assert not restarted._resting_in_window(host)
            assert restarted._resting_session_is_eh(host)
    finally:
        time.tzset()  # The context restores TZ before resetting libc's timezone.

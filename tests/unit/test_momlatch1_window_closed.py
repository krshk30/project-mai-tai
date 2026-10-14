from datetime import UTC, datetime

import pytest

from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy
from project_mai_tai.momwarn_pause_incidents import PauseIncidentStore


def test_window_end_finishes_pause_and_freezes_counters():
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    policy.trip("monitor_unreadable", 0)
    before = policy.metrics(1)
    policy.close_window(10)
    for now in range(20, 620):
        policy.observe("monitor_unreadable", now_ns=now)
    after = policy.metrics(720_000_000_000)
    assert after["state"] == "window_closed"
    assert after["alert_reasons"] == []
    assert after["pause_count"] == before["pause_count"]
    assert after["pause_episode_count"] == before["pause_episode_count"]
    assert after["noise_reason_counts"] == before["noise_reason_counts"]
    assert after["history"][0]["end_ns"] == 10
    assert after["history"][0]["duration_ns"] == 10
    assert after["history"][0]["outcome"] == "window_closed"


def test_window_closed_does_not_hide_harm_or_rearm_on_clean_reads():
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    policy.close_window(0)
    for now in range(1, 5):
        assert not policy.clean(now)
    policy.trip("live_error", 5)
    policy.close_window(6)
    result = policy.metrics(7)
    assert result["state"] == "off"
    assert result["reason"] == "live_error"
    assert result["window_closed"]
    assert result["alert_reasons"] == []


def test_reconnects_outside_window_do_not_create_flapping_or_storm():
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    policy.close_window(0)
    for now in range(1, 20):
        policy.trip("disconnect", now)
    result = policy.metrics(20)
    assert result["state"] == "window_closed"
    assert result["reconnect_event_count"] == 0
    assert result["pause_count"] == 0
    assert result["alert_reasons"] == []


def test_new_window_still_requires_three_real_clean_reads():
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    assert not policy.clean(0)
    assert not policy.clean(1)
    assert policy.clean(2)
    assert policy.metrics(3)["state"] == "active"


def test_explicit_reopen_preserves_sticky_harm_and_restarts_clean_streak():
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    policy.trip("monitor_unreadable", 0)
    policy.clean(1)
    policy.close_window(2)
    policy.open_window(3)
    assert not policy.clean(4)
    assert not policy.clean(5)
    assert policy.clean(6)
    policy.trip("live_error", 7)
    policy.close_window(8)
    policy.open_window(9)
    assert not policy.clean(10)
    assert policy.metrics(11)["reason"] == "live_error"


def test_closed_window_sample_does_not_open_db_incident():
    class NoSession:
        def __call__(self):
            pytest.fail("closed window must not open a DB session")
    policy = WarningPausePolicy(clock_ns=lambda: 0)
    policy.trip("monitor_unreadable", 0)
    policy.close_window(1)
    sample = {"observed_ns": 1_800_000_000_000,
              "pause_state": policy.metrics(1_800_000_000_000)}
    assert PauseIncidentStore(NoSession()).record(sample) == 0


@pytest.mark.asyncio
async def test_gateway_window_close_reports_off_by_window_not_pause(monkeypatch):
    from tests.unit.test_momwarn1_gateway import service
    from project_mai_tai.market_data import gateway
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 13, 23, 0, tzinfo=UTC).astimezone(tz or UTC)
    monkeypatch.setattr(gateway, "datetime", Clock)
    svc, _, _, _ = service()
    svc._pauses.trip("monitor_unreadable")
    await svc._end_warning_window()
    metrics = svc.warning_metrics()
    assert metrics["coverage_status"] == "off_by_window"
    assert metrics["pause_state"]["alert_reasons"] == []
    before = metrics["pause_state"]["pause_count"]
    svc._latch_warning_revert("disconnect")
    assert svc.warning_metrics()["pause_state"]["pause_count"] == before
    assert svc._warning_revert_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize("night,opening", [
    ("2026-10-13T23:00:00-04:00", "2026-10-14T04:00:00-04:00"),
    ("2026-11-01T23:00:00-05:00", "2026-11-02T04:00:00-05:00"),
])
async def test_night_restart_arms_after_three_reads_without_utc_day_change(
        monkeypatch, night, opening):
    from types import SimpleNamespace
    from project_mai_tai.market_data import gateway, massive_provider
    from tests.unit.test_momwarn1_gateway import service
    stamp = [datetime.fromisoformat(night)]
    clock = SimpleNamespace(now=lambda tz: stamp[0].astimezone(tz))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)
    svc, _, _, _ = service()
    await svc._warning_monitor_once()
    assert svc._pauses.state == "window_closed"
    assert not svc._warning_active
    assert stamp[0].astimezone(UTC).date() == datetime.fromisoformat(opening).astimezone(UTC).date()
    stamp[0] = datetime.fromisoformat(opening)
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active is (index == 2)
    assert svc._warning_window_day == stamp[0].date()
    assert svc._pauses.state == "active"


@pytest.mark.asyncio
async def test_eighteen_close_then_next_et_day_open_requires_three_reads(monkeypatch):
    from types import SimpleNamespace
    from project_mai_tai.market_data import gateway, massive_provider
    from tests.unit.test_momwarn1_gateway import service
    stamp = [datetime.fromisoformat("2026-10-13T17:59:00-04:00")]
    clock = SimpleNamespace(now=lambda tz: stamp[0].astimezone(tz))
    monkeypatch.setattr(gateway, "datetime", clock)
    monkeypatch.setattr(massive_provider, "datetime", clock)
    svc, _, _, _ = service()
    for _ in range(3):
        await svc._warning_monitor_once()
    assert svc._warning_active
    stamp[0] = datetime.fromisoformat("2026-10-13T18:00:00-04:00")
    await svc._warning_monitor_once()
    assert svc._pauses.state == "window_closed"
    stamp[0] = datetime.fromisoformat("2026-10-13T23:00:00-04:00")
    await svc._warning_monitor_once()
    stamp[0] = datetime.fromisoformat("2026-10-14T04:00:00-04:00")
    for index in range(3):
        await svc._warning_monitor_once()
        assert svc._warning_active is (index == 2)

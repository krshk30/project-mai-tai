"""Alert decisions preserve display semantics without constructing the display."""
import pytest

from project_mai_tai.market_data.momwarn_pause_policy import WarningPausePolicy


@pytest.mark.parametrize("reason", [None, "monitor_unreadable", "disconnect", "load"])
def test_decisions_do_not_materialize_snapshot(monkeypatch, reason):
    policy = WarningPausePolicy()
    monkeypatch.setattr(policy, "snapshot", lambda *args: pytest.fail("decision copied display history"))
    policy.observe(reason, ready=reason is None, now_ns=0)


def test_decision_alerts_match_snapshot_through_flapping_long_pause_and_expiry():
    policy = WarningPausePolicy()
    now = 0
    for cycle in range(10):
        for reason in (None, None, None, "monitor_unreadable"):
            now += 1_000_000_000
            decision = policy.observe(reason, ready=reason is None, now_ns=now)
            assert decision.alert_reasons == tuple(sorted(policy.snapshot(now)["alert_reasons"]))
    for delta in (601_000_000_000, 3_600_000_000_000):
        now += delta
        decision = policy.observe("monitor_unreadable", now_ns=now)
        assert decision.alert_reasons == tuple(sorted(policy.snapshot(now)["alert_reasons"]))

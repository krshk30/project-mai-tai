from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ops.health.option_a_treatment_guard import (
    Blind,
    LogTail,
    SamplerEvidence,
    SlowdownRules,
    _release_confirmed,
    _route_sampler_exit,
    stop_paper,
)


NOW = datetime(2026, 10, 1, 11, 10, tzinfo=UTC)


def test_new_gateway_1008_route_is_not_a_successful_observation():
    # The collector's rc=3 is handled by the supervisor; a bare collector cannot stop paper.
    from scripts.option_a_treatment_1008_sampler import sample_log

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "market-data.log"
        log.write_text("old received 1008\n")
        _initial, cursor = sample_log(log, None, sampled_at=NOW)
        log.write_text("old received 1008\nnew received 1008\n")
        row, _ = sample_log(log, cursor, sampled_at=NOW)
    assert row["status"] == "STOP_TRIGGER"
    assert row["new_1008_lines"] == 1
    stopped = []
    assert _route_sampler_exit(3, lambda reason: stopped.append(reason) or 0) == 0
    assert stopped == ["gateway_1008"]
    with pytest.raises(Blind, match="rc=2"):
        _route_sampler_exit(2, lambda _reason: 0)


def test_heartbeat_stop_after_two_nonhealthy_samples_and_absolute_age():
    rules = SlowdownRules()
    assert rules.heartbeat("degraded", 1) is None
    assert "gateway_status=degraded" in rules.heartbeat("degraded", 2)
    assert rules.heartbeat("healthy", 30.819) is None
    assert "gateway_heartbeat_age" in rules.heartbeat("healthy", 30.820)


def test_snapshot_rule_uses_matching_hour_and_five_distinct_minutes():
    rules = SlowdownRules()
    for minute in range(4):
        trigger, _ = rules.minute(
            NOW.replace(minute=minute), snapshot_intervals=[15.0] * 20,
            probe_lags={}, oms_refusals=0, snapshot_warmup_complete=True,
        )
        assert trigger is None
    trigger, _ = rules.minute(
        NOW.replace(minute=4), snapshot_intervals=[15.0] * 20,
        probe_lags={}, oms_refusals=0, snapshot_warmup_complete=True,
    )
    assert trigger == "snapshot_p95_s=15.000>14.076"


def test_missing_snapshot_coverage_is_unknown_not_pass():
    rules = SlowdownRules()
    with pytest.raises(Blind, match="snapshot intervals=19<20"):
        rules.minute(
            NOW, snapshot_intervals=[5.0] * 19, probe_lags={},
            oms_refusals=0, snapshot_warmup_complete=True,
        )


def test_oms_refusal_rule_stops_after_two_minutes():
    rules = SlowdownRules()
    assert rules.minute(
        NOW, snapshot_intervals=[5.0] * 20, probe_lags={},
        oms_refusals=3, snapshot_warmup_complete=True,
    )[0] is None
    assert "oms_quote_refusals_5m=3" in rules.minute(
        NOW.replace(minute=11), snapshot_intervals=[5.0] * 20,
        probe_lags={}, oms_refusals=3, snapshot_warmup_complete=True,
    )[0]


def test_v2_lag_requires_three_probes_and_five_minutes():
    rules = SlowdownRules()
    for minute in range(4):
        assert rules.minute(
            NOW.replace(minute=minute), snapshot_intervals=[5.0] * 20,
            probe_lags={"LGHL": [6.019] * 3}, oms_refusals=0,
            snapshot_warmup_complete=True,
        )[0] is None
    assert "v2_symbol=LGHL" in rules.minute(
        NOW.replace(minute=4), snapshot_intervals=[5.0] * 20,
        probe_lags={"LGHL": [6.019] * 3}, oms_refusals=0,
        snapshot_warmup_complete=True,
    )[0]


class FakeRedis:
    def __init__(self, owners: dict[str, list[str]], count: int):
        self.owners = owners
        self.count = count

    def hgetall(self, _key: str):
        return {"_migration_complete": "1", **{
            name: json.dumps(symbols) for name, symbols in self.owners.items()
        }}

    def xrevrange(self, _key: str, count: int):
        assert count == 25
        event = {
            "source_service": "market-data-gateway",
            "produced_at": "2026-10-01T11:10:01+00:00",
            "payload": {"status": "healthy", "details": {"active_symbols": str(self.count)}},
        }
        return [("1-0", {"data": json.dumps(event)})]


class FakeSettings:
    redis_stream_prefix = "mai_tai"
    market_data_static_symbol_list: tuple[str, ...] = ()


def test_stop_release_keeps_other_consumers_symbols(monkeypatch):
    redis = FakeRedis({"momentum-paper": [], "strategy-engine": ["A"],
                       "schwab-1m-v2": ["A", "B"]}, count=2)
    monkeypatch.setattr(
        "ops.health.option_a_treatment_guard._union_line_confirms",
        lambda _path, _offset, removed: removed == {"C"},
    )
    assert _release_confirmed(
        redis, FakeSettings(),
        before={"momentum-paper": {"A", "C"}, "strategy-engine": {"A"},
                "schwab-1m-v2": {"A", "B"}},
        stopped_at=NOW, log_offset=0,
    )
    redis.count = 1
    assert not _release_confirmed(
        redis, FakeSettings(),
        before={"momentum-paper": {"A", "C"}}, stopped_at=NOW, log_offset=0,
    )


def test_log_tail_fails_closed_on_copytruncate(tmp_path: Path):
    log = tmp_path / "gateway.log"
    log.write_text("before rotation\n")
    tail = LogTail(log)
    log.write_text("")
    with pytest.raises(Blind, match="rotated/truncated"):
        tail.read()


def test_sampler_alive_but_not_writing_fails_closed(tmp_path: Path):
    output = tmp_path / "1008.jsonl"
    evidence = SamplerEvidence(
        output, start=NOW, end=NOW.replace(hour=13), launched_at=NOW,
    )
    with pytest.raises(Blind, match="output stalled"):
        evidence.read(NOW.replace(second=3))


def test_sampler_row_gap_and_1008_trigger(tmp_path: Path):
    output = tmp_path / "1008.jsonl"
    output.write_text(json.dumps({"sampled_at_utc": NOW.isoformat(), "status": "BASELINE"}) + "\n")
    evidence = SamplerEvidence(
        output, start=NOW, end=NOW.replace(hour=13), launched_at=NOW,
    )
    assert evidence.read(NOW) is None
    output.write_text(output.read_text() + json.dumps({
        "sampled_at_utc": NOW.replace(second=1).isoformat(), "status": "STOP_TRIGGER",
    }) + "\n")
    assert evidence.read(NOW.replace(second=1)) == "gateway_1008"
    assert evidence.treatment_count == 2

    gap = tmp_path / "gap.jsonl"
    gap.write_text(json.dumps({"sampled_at_utc": NOW.isoformat(), "status": "BASELINE"}) + "\n")
    missing = SamplerEvidence(gap, start=NOW, end=NOW.replace(hour=13), launched_at=NOW)
    assert missing.read(NOW) is None
    gap.write_text(gap.read_text() + json.dumps({
        "sampled_at_utc": NOW.replace(second=3).isoformat(), "status": "OK",
    }) + "\n")
    with pytest.raises(Blind, match="row gap"):
        missing.read(NOW.replace(second=3))


def test_consecutive_minute_rule_rejects_a_monitor_gap():
    rules = SlowdownRules()
    rules.minute(NOW, snapshot_intervals=[5.0] * 20, probe_lags={},
                 oms_refusals=0, snapshot_warmup_complete=True)
    with pytest.raises(Blind, match="minute evaluation gap"):
        rules.minute(NOW.replace(minute=12), snapshot_intervals=[5.0] * 20,
                     probe_lags={}, oms_refusals=0, snapshot_warmup_complete=True)


def test_stop_is_paper_only_and_pages_after_owner_release(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: True)
    commands = []
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        commands.append(command) or subprocess.CompletedProcess(command, 0)
    ))
    pages = []
    monkeypatch.setattr(guard, "_page", lambda title, *_args, **_kw: (
        pages.append(title) or True
    ))
    audit = tmp_path / "audit.jsonl"
    assert stop_paper("gateway_1008", FakeRedis({}, 0), FakeSettings(), audit) == 0
    assert commands == [["systemctl", "stop", "project-mai-tai-momentum-paper.service"]]
    assert pages == ["Option A paper STOP"]
    assert json.loads(audit.read_text().splitlines()[0])["reason"] == "gateway_1008"


def test_owner_release_fallback_publishes_only_momentum_empty_replace(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: False)
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        subprocess.CompletedProcess(command, 0)
    ))
    monkeypatch.setattr(guard, "_page", lambda *_args, **_kw: True)
    monkeypatch.setattr(guard.time, "monotonic", iter((0, 40, 50, 90)).__next__)
    class RedisWithWrites(FakeRedis):
        def __init__(self):
            super().__init__({}, 0)
            self.published = []

        def xadd(self, _key, fields):
            self.published.append(json.loads(fields["data"]))
            return "1-0"

    redis = RedisWithWrites()
    assert stop_paper("test", redis, FakeSettings(), tmp_path / "audit.jsonl") == 2
    assert len(redis.published) == 1
    assert redis.published[0]["payload"] == {
        "consumer_name": "momentum-paper", "mode": "replace", "symbols": [],
    }


def test_systemd_guard_has_watchdog_and_failure_stop():
    root = Path(__file__).resolve().parents[2]
    service = (root / "ops/systemd/project-mai-tai-option-a-guard@.service").read_text()
    fallback = (root / "ops/systemd/project-mai-tai-option-a-guard-failure@.service").read_text()
    assert "Type=notify" in service and "WatchdogSec=15s" in service
    assert "OnFailure=project-mai-tai-option-a-guard-failure@%i.service" in service
    assert "systemctl stop project-mai-tai-momentum-paper.service" in fallback
    assert "Priority: low" in fallback

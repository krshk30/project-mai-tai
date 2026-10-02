from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ops.health.option_a_treatment_guard import (
    Blind,
    LiveSignals,
    LogTail,
    SamplerEvidence,
    SlowdownRules,
    _release_confirmed,
    _route_sampler_exit,
    _union_line_confirms,
    run_guard,
    stop_paper,
)


NOW = datetime(2026, 10, 1, 11, 10, tzinfo=UTC)


def sampler_row(stamped: datetime, status: str, *, offset: int = 100,
                size: int = 100, new_1008: int = 0) -> dict:
    return {
        "sampled_at_utc": stamped.isoformat(), "status": status,
        "device": 1, "inode": 2, "read_from_offset": offset,
        "size_bytes": size, "new_1008_lines": new_1008,
    }


def test_new_gateway_1008_route_is_not_a_successful_observation():
    # The collector's rc=3 is handled by the supervisor; a bare collector cannot stop paper.
    from scripts.option_a_treatment_1008_sampler import sample_log

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "market-data.log"
        log.write_text("old received 1008 (policy violation); then sent 1008 (policy violation)\n")
        _initial, cursor = sample_log(log, None, sampled_at=NOW)
        log.write_text(
            "old received 1008 (policy violation); then sent 1008 (policy violation)\n"
            "new received 1008 (policy violation); then sent 1008 (policy violation)\n"
        )
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


def test_heartbeat_unhealthy_streak_resets_on_a_healthy_sample():
    rules = SlowdownRules()
    assert rules.heartbeat("degraded", 1) is None
    assert rules.heartbeat("healthy", 1) is None
    assert rules.heartbeat("degraded", 1) is None
    assert "consecutive=2" in rules.heartbeat("degraded", 1)


def test_latest_25_heartbeats_without_gateway_are_blind():
    class OtherServicesOnly:
        def xrevrange(self, _key: str, count: int):
            assert count == 25
            return [
                (f"{index}-0", {"data": json.dumps({
                    "source_service": "strategy-engine" if index % 2 else "schwab-1m-v2",
                    "produced_at": NOW.isoformat(),
                    "payload": {"status": "healthy"},
                })})
                for index in range(count)
            ]

    signals = object.__new__(LiveSignals)
    signals.redis = OtherServicesOnly()
    signals.prefix = "mai_tai"
    with pytest.raises(Blind, match="no gateway heartbeat in latest 25 events"):
        signals.heartbeat(NOW)


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


def test_snapshot_exact_bound_does_not_stop():
    rules = SlowdownRules()
    for minute in range(5):
        trigger, _ = rules.minute(
            NOW.replace(minute=minute), snapshot_intervals=[14.076] * 20,
            probe_lags={}, oms_refusals=0, snapshot_warmup_complete=True,
        )
        assert trigger is None


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


def test_oms_two_refusals_do_not_stop_or_carry_a_prior_streak():
    rules = SlowdownRules()
    assert rules.minute(
        NOW, snapshot_intervals=[5.0] * 20, probe_lags={},
        oms_refusals=3, snapshot_warmup_complete=True,
    )[0] is None
    assert rules.minute(
        NOW.replace(minute=11), snapshot_intervals=[5.0] * 20, probe_lags={},
        oms_refusals=2, snapshot_warmup_complete=True,
    )[0] is None
    assert rules.minute(
        NOW.replace(minute=12), snapshot_intervals=[5.0] * 20, probe_lags={},
        oms_refusals=3, snapshot_warmup_complete=True,
    )[0] is None


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


def test_v2_exact_bound_and_unseen_symbol_pooled_bound():
    rules = SlowdownRules()
    for minute in range(5):
        trigger, _ = rules.minute(
            NOW.replace(minute=minute), snapshot_intervals=[5.0] * 20,
            probe_lags={"LGHL": [6.018] * 3, "NEW": [6.096] * 3},
            oms_refusals=0, snapshot_warmup_complete=True,
        )
        assert trigger is None
    rules = SlowdownRules()
    for minute in range(4):
        assert rules.minute(
            NOW.replace(minute=minute), snapshot_intervals=[5.0] * 20,
            probe_lags={"NEW": [6.097] * 3}, oms_refusals=0,
            snapshot_warmup_complete=True,
        )[0] is None
    assert "v2_symbol=NEW" in rules.minute(
        NOW.replace(minute=4), snapshot_intervals=[5.0] * 20,
        probe_lags={"NEW": [6.097] * 3}, oms_refusals=0,
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
    redis.count = 2
    redis.owners = {"momentum-paper": [], "strategy-engine": ["A"],
                    "schwab-1m-v2": ["A", "C"]}
    assert not _release_confirmed(
        redis, FakeSettings(),
        before={"momentum-paper": {"C"}, "strategy-engine": {"A"},
                "schwab-1m-v2": {"A", "B"}},
        stopped_at=NOW, log_offset=0,
    )


def test_stop_release_cannot_hide_lost_orb_ownership_behind_same_union_count(monkeypatch):
    redis = FakeRedis({"momentum-paper": [], "strategy-engine": ["A"],
                       "schwab-1m-v2": ["B"], "orb": []}, count=2)
    monkeypatch.setattr(
        "ops.health.option_a_treatment_guard._union_line_confirms",
        lambda *_args: True,
    )
    assert not _release_confirmed(
        redis, FakeSettings(),
        before={"momentum-paper": {"C"}, "strategy-engine": {"A"},
                "schwab-1m-v2": {"B"}, "orb": {"B", "C"}},
        stopped_at=NOW, log_offset=0,
    )


def test_stop_release_requires_paper_owner_hash_to_be_empty(monkeypatch):
    redis = FakeRedis({"momentum-paper": ["C"], "strategy-engine": ["A"],
                       "schwab-1m-v2": ["B"]}, count=2)
    monkeypatch.setattr(
        "ops.health.option_a_treatment_guard._union_line_confirms",
        lambda *_args: True,
    )
    assert not _release_confirmed(
        redis, FakeSettings(),
        before={"momentum-paper": {"C"}, "strategy-engine": {"A"},
                "schwab-1m-v2": {"B"}},
        stopped_at=NOW, log_offset=0,
    )


def test_overlap_only_paper_release_still_requires_gateway_update_line(tmp_path: Path, monkeypatch):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("prior line\n")
    offset = log.stat().st_size
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    redis = FakeRedis({"momentum-paper": [], "strategy-engine": ["A"]}, count=1)
    before = {"momentum-paper": {"A"}, "strategy-engine": {"A"}}
    assert not _release_confirmed(
        redis, FakeSettings(), before=before, stopped_at=NOW, log_offset=offset,
    )
    log.write_text(log.read_text() + "[MARKET-DATA-SUBSCRIPTION-UNION] "
                   "consumer=momentum-paper added=- removed=- count=1\n")
    assert _union_line_confirms(log, offset, set())
    assert _release_confirmed(
        redis, FakeSettings(), before=before, stopped_at=NOW, log_offset=offset,
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
    baseline_at = NOW - timedelta(seconds=1)
    output.write_text(json.dumps(sampler_row(baseline_at, "BASELINE")) + "\n")
    evidence = SamplerEvidence(
        output, start=NOW, end=NOW.replace(hour=13), launched_at=baseline_at,
    )
    assert evidence.read(baseline_at) is None
    output.write_text(output.read_text() + json.dumps(
        sampler_row(NOW, "STOP_TRIGGER", size=120, new_1008=1)
    ) + "\n")
    assert evidence.read(NOW) == "gateway_1008"
    assert evidence.treatment_count == 1

    gap = tmp_path / "gap.jsonl"
    gap.write_text(json.dumps(sampler_row(baseline_at, "BASELINE")) + "\n")
    missing = SamplerEvidence(gap, start=NOW, end=NOW.replace(hour=13), launched_at=baseline_at)
    assert missing.read(baseline_at) is None
    gap.write_text(gap.read_text() + json.dumps(
        sampler_row(NOW.replace(second=2), "OK", size=120)
    ) + "\n")
    with pytest.raises(Blind, match="row gap"):
        missing.read(NOW.replace(second=2))


def test_sampler_refuses_missing_or_late_initial_baseline(tmp_path: Path):
    output = tmp_path / "1008.jsonl"
    output.write_text(json.dumps(sampler_row(NOW, "OK")) + "\n")
    evidence = SamplerEvidence(output, start=NOW, end=NOW + timedelta(hours=1),
                               launched_at=NOW - timedelta(seconds=1))
    with pytest.raises(Blind, match="missing initial .* baseline"):
        evidence.read(NOW)
    output.write_text(json.dumps(sampler_row(NOW, "BASELINE")) + "\n")
    evidence = SamplerEvidence(output, start=NOW, end=NOW + timedelta(hours=1),
                               launched_at=NOW - timedelta(seconds=1))
    with pytest.raises(Blind, match="baseline at or after treatment start"):
        evidence.read(NOW)


def test_sampler_cursor_and_count_disagreement_fail_closed(tmp_path: Path):
    baseline_at = NOW - timedelta(seconds=1)
    output = tmp_path / "1008.jsonl"
    for bad_baseline in (
        sampler_row(baseline_at, "BASELINE", offset=99),
        {"sampled_at_utc": baseline_at.isoformat(), "status": "BASELINE"},
    ):
        output.write_text(json.dumps(bad_baseline) + "\n")
        evidence = SamplerEvidence(output, start=NOW, end=NOW + timedelta(hours=1),
                                   launched_at=baseline_at)
        with pytest.raises(Blind, match="offset unconfirmed|malformed"):
            evidence.read(baseline_at)
    for bad_next in (
        sampler_row(NOW, "OK", offset=99, size=120),
        {**sampler_row(NOW, "OK", size=120), "inode": 3},
        sampler_row(NOW, "OK", size=120, new_1008=1),
        sampler_row(NOW, "STOP_TRIGGER", size=120, new_1008=0),
    ):
        output.write_text(json.dumps(sampler_row(baseline_at, "BASELINE")) + "\n")
        evidence = SamplerEvidence(output, start=NOW, end=NOW + timedelta(hours=1),
                                   launched_at=baseline_at)
        assert evidence.read(baseline_at) is None
        output.write_text(output.read_text() + json.dumps(bad_next) + "\n")
        with pytest.raises(Blind, match="cursor discontinuity|suppressed|no new"):
            evidence.read(NOW)


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
    page_row = json.loads(audit.read_text().splitlines()[-1])
    assert page_row["action"] == "page_delivery"
    assert page_row["title"] == "Option A paper STOP"
    assert page_row["delivered"] is True
    assert page_row["at_utc"]


def test_page_delivery_failure_is_audited_and_not_reported_as_success(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: True)
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        subprocess.CompletedProcess(command, 0)
    ))
    monkeypatch.setattr(guard, "_page", lambda *_args, **_kw: False)
    audit = tmp_path / "audit.jsonl"
    assert stop_paper("test", FakeRedis({}, 0), FakeSettings(), audit) == 2
    page_row = json.loads(audit.read_text().splitlines()[-1])
    assert page_row["action"] == "page_delivery"
    assert page_row["delivered"] is False


def test_page_delivery_audit_failure_is_not_reported_as_success(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: True)
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        subprocess.CompletedProcess(command, 0)
    ))
    monkeypatch.setattr(guard, "_page", lambda *_args, **_kw: True)
    original_audit = guard._audit

    def audit_except_delivery(path, item):
        if item["action"] == "page_delivery":
            raise OSError("audit disk full")
        original_audit(path, item)

    monkeypatch.setattr(guard, "_audit", audit_except_delivery)
    audit = tmp_path / "audit.jsonl"
    assert stop_paper("test", FakeRedis({}, 0), FakeSettings(), audit) == 2
    assert [json.loads(line)["action"] for line in audit.read_text().splitlines()] == [
        "stop_paper", "owner_release_confirmed",
    ]


def test_refused_paper_stop_is_unknown_and_never_claims_owner_release(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: pytest.fail(
        "owner release cannot be claimed when systemctl stop was refused"
    ))
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        subprocess.CompletedProcess(command, 1)
    ))
    pages = []
    monkeypatch.setattr(guard, "_page", lambda title, *_args, **_kw: (
        pages.append(title) or True
    ))
    assert stop_paper("gateway_1008", FakeRedis({}, 0), FakeSettings(),
                      tmp_path / "audit.jsonl") == 2
    assert pages == ["Option A paper STOP UNKNOWN"]


def test_audit_write_error_after_stop_still_pages_unknown(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    commands = []
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        commands.append(command) or subprocess.CompletedProcess(command, 0)
    ))
    monkeypatch.setattr(guard, "_audit", lambda *_args, **_kw: (_ for _ in ()).throw(
        OSError("disk full")
    ))
    pages = []
    monkeypatch.setattr(guard, "_page", lambda title, *_args, **_kw: (
        pages.append(title) or True
    ))
    assert stop_paper("monitor_UNKNOWN", FakeRedis({}, 0), FakeSettings(),
                      tmp_path / "audit.jsonl") == 2
    assert commands == [["systemctl", "stop", "project-mai-tai-momentum-paper.service"]]
    assert pages == ["Option A audit UNKNOWN"]


def test_fallback_release_is_verified_and_paged_once(monkeypatch, tmp_path: Path):
    import ops.health.option_a_treatment_guard as guard

    log = tmp_path / "gateway.log"
    log.write_text("")
    monkeypatch.setattr(guard, "GATEWAY_LOG", log)
    monkeypatch.setattr(guard, "_owners", lambda *_: {"momentum-paper": {"A"}})
    monkeypatch.setattr(guard.subprocess, "run", lambda command, **_kw: (
        subprocess.CompletedProcess(command, 0)
    ))
    monkeypatch.setattr(guard.time, "monotonic", iter((0, 40, 50, 51)).__next__)
    pages = []
    monkeypatch.setattr(guard, "_page", lambda title, *_args, **_kw: (
        pages.append(title) or True
    ))

    class RedisWithRelease(FakeRedis):
        def __init__(self):
            super().__init__({}, 0)
            self.published = []

        def xadd(self, _key, fields):
            self.published.append(json.loads(fields["data"]))
            return "2-0"

    redis = RedisWithRelease()
    monkeypatch.setattr(guard, "_release_confirmed", lambda *_args, **_kw: bool(redis.published))
    assert stop_paper("gateway_1008", redis, FakeSettings(), tmp_path / "audit.jsonl") == 0
    assert len(redis.published) == 1
    assert redis.published[0]["payload"] == {
        "consumer_name": "momentum-paper", "mode": "replace", "symbols": [],
    }
    assert pages == ["Option A paper STOP"]


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
    pages = []
    monkeypatch.setattr(guard, "_page", lambda title, *_args, **_kw: (
        pages.append(title) or True
    ))
    monkeypatch.setattr(guard.time, "monotonic", iter((0, 1, 40, 50, 51, 90)).__next__)
    monkeypatch.setattr(guard.time, "sleep", lambda *_args: None)
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
    assert pages == ["Option A owner release UNKNOWN"]
    assert not any(json.loads(line)["action"] == "owner_release_confirmed"
                   for line in (tmp_path / "audit.jsonl").read_text().splitlines())


def test_systemd_guard_has_watchdog_and_failure_stop():
    root = Path(__file__).resolve().parents[2]
    service = (root / "ops/systemd/project-mai-tai-option-a-guard@.service").read_text()
    fallback = (root / "ops/systemd/project-mai-tai-option-a-guard-failure@.service").read_text()
    assert "Type=notify" in service and "WatchdogSec=15s" in service
    assert "OnFailure=project-mai-tai-option-a-guard-failure@%i.service" in service
    assert "systemctl stop project-mai-tai-momentum-paper.service" in fallback
    assert "Priority: low" in fallback
    actions = [line for line in fallback.splitlines() if line.startswith("ExecStart=")]
    assert len(actions) == 3
    assert actions[0].startswith("ExecStart=-/usr/bin/systemctl stop project-mai-tai-momentum-paper.service")
    assert "--emergency-stop" in actions[1]
    assert actions[2].startswith("ExecStart=-/usr/bin/curl ")
    assert "Priority: low" in actions[2]


def _run_one_guard_tick(monkeypatch, tmp_path: Path, *, late: bool) -> tuple[int, list[str]]:
    import ops.health.option_a_treatment_guard as guard

    before = datetime(2026, 10, 1, 10, 59, 58, tzinfo=UTC)
    during = datetime(2026, 10, 1, 11, 0, 1, tzinfo=UTC)
    times = iter([before, before, before, before, during])

    class Clock:
        @staticmethod
        def combine(*args, **kwargs):
            return datetime.combine(*args, **kwargs)

        @staticmethod
        def now(_tz):
            return next(times)

    class PaperSampler:
        pid = 123

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, **_kwargs):
            return 0

    class GatewayRedis:
        def close(self):
            pass

    class Signals:
        def __init__(self, *_args):
            pass

        def read_logs(self, *_args):
            raise RuntimeError("monitor source blew up")

    fake_redis = GatewayRedis()
    monkeypatch.setattr(guard, "datetime", Clock)
    monkeypatch.setattr(guard, "Settings", lambda **_kw: SimpleNamespace(
        redis_url="redis://unused", redis_stream_prefix="mai_tai",
    ))
    monkeypatch.setattr(guard, "Redis", SimpleNamespace(from_url=lambda *_args, **_kw: fake_redis))
    monkeypatch.setattr(guard, "LiveSignals", Signals)
    monkeypatch.setattr(guard, "SamplerEvidence", lambda *_args, **_kw: SimpleNamespace(
        read=lambda *_a: None,
    ))
    monkeypatch.setattr(guard.subprocess, "Popen", lambda *_args, **_kw: PaperSampler())
    monkeypatch.setattr(guard.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(guard, "_notify_systemd", lambda *_args: None)
    monkeypatch.setattr(guard.time, "sleep", lambda *_args: None)
    monkeypatch.setattr(guard.time, "monotonic", iter((10, 11, 11.6 if late else 11)).__next__)
    monkeypatch.setattr(guard.os, "getloadavg", lambda: (1.0, 1.0, 1.0))
    stopped = []
    monkeypatch.setattr(guard, "stop_paper", lambda reason, *_args: (
        stopped.append(reason) or 2
    ))
    return run_guard(during.date(), tmp_path), stopped


def test_monitor_exception_stops_paper_instead_of_bare_exit(monkeypatch, tmp_path: Path):
    rc, stopped = _run_one_guard_tick(monkeypatch, tmp_path, late=False)
    assert rc == 2
    assert len(stopped) == 1
    assert "monitor_UNKNOWN:RuntimeError:monitor source blew up" in stopped[0]


def test_late_one_hz_tick_stops_paper_as_unknown(monkeypatch, tmp_path: Path):
    rc, stopped = _run_one_guard_tick(monkeypatch, tmp_path, late=True)
    assert rc == 2
    assert len(stopped) == 1
    assert "monitor_UNKNOWN:Blind:1Hz monitor tick late" in stopped[0]

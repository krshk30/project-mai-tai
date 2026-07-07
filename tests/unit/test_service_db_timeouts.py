"""PR-E — fleet-wide per-service DB timeouts (build_timed_session_factory).

Rolls #391's Postgres statement/lock/connect/pool timeout treatment to the non-OMS
services so a stalled DB connection RAISES within seconds instead of hanging them
unbounded. Timeouts only (no off-loop restructuring). These tests spy on create_engine
(mirroring the Fix-1 tests) and assert per-profile values, the fleet flag, the per-service
disabled list, and SQLite-safety. build_engine is lru-cached, so distinct URLs + cache_clear.
"""
from __future__ import annotations

from project_mai_tai.db import session as db_session
from project_mai_tai.settings import Settings


def _spy(monkeypatch):
    calls: list = []
    monkeypatch.setattr(db_session, "create_engine", lambda url, **kw: calls.append((url, kw)) or "E")
    db_session.build_engine.cache_clear()
    return calls


def test_timed_factory_fast_profile_applies_5s(monkeypatch):
    calls = _spy(monkeypatch)
    db_session.build_timed_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_fast"), service="orb", profile="fast",
    )
    db_session.build_engine.cache_clear()
    _, kw = calls[0]
    assert kw["pool_pre_ping"] is True
    assert kw["pool_timeout"] == 5 and kw["pool_recycle"] == 1800
    assert kw["connect_args"]["connect_timeout"] == 5
    assert "-c statement_timeout=5000" in kw["connect_args"]["options"]
    assert "-c lock_timeout=3000" in kw["connect_args"]["options"]


def test_timed_factory_slow_profile_applies_60s(monkeypatch):
    calls = _spy(monkeypatch)
    db_session.build_timed_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_slow"), service="reconciler", profile="slow",
    )
    db_session.build_engine.cache_clear()
    _, kw = calls[0]
    assert kw["pool_timeout"] == 10
    assert "-c statement_timeout=60000" in kw["connect_args"]["options"]
    assert "-c lock_timeout=10000" in kw["connect_args"]["options"]


def test_timed_factory_fleet_flag_off_is_byte_identical_untimed(monkeypatch):
    calls = _spy(monkeypatch)
    db_session.build_timed_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_off", service_db_timeouts_enabled=False),
        service="orb", profile="fast",
    )
    db_session.build_engine.cache_clear()
    assert calls[0][1] == {"pool_pre_ping": True}  # rollback lever: untimed, byte-identical


def test_timed_factory_per_service_disabled_list(monkeypatch):
    calls = _spy(monkeypatch)
    # 'orb' is in the per-service disabled list -> untimed ...
    db_session.build_timed_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_dis1",
                 service_db_timeouts_disabled_services="orb, control"),
        service="orb", profile="fast",
    )
    db_session.build_engine.cache_clear()
    assert calls[-1][1] == {"pool_pre_ping": True}
    # ... but a service NOT in the list is still timed.
    db_session.build_timed_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_dis2",
                 service_db_timeouts_disabled_services="orb, control"),
        service="reconciler", profile="slow",
    )
    db_session.build_engine.cache_clear()
    assert "connect_args" in calls[-1][1]


def test_timed_factory_sqlite_is_untimed(monkeypatch):
    # The timeouts are Postgres GUCs -> a non-Postgres URL falls back to untimed (SQLite-safe).
    calls = _spy(monkeypatch)
    db_session.build_timed_session_factory(
        Settings(database_url="sqlite:///:memory:"), service="orb", profile="fast",
    )
    db_session.build_engine.cache_clear()
    assert calls[0][1] == {"pool_pre_ping": True}


def test_build_session_factory_and_oms_factory_unchanged(monkeypatch):
    """PR-E must not alter the untimed default or the OMS factory."""
    calls = _spy(monkeypatch)
    db_session.build_session_factory(Settings(database_url="postgresql+psycopg://u:p@h/e_plain"))
    db_session.build_engine.cache_clear()
    assert calls[-1][1] == {"pool_pre_ping": True}  # untimed default unchanged
    db_session.build_oms_session_factory(
        Settings(database_url="postgresql+psycopg://u:p@h/e_oms", oms_db_timeouts_enabled=True)
    )
    db_session.build_engine.cache_clear()
    assert "-c statement_timeout=5000" in calls[-1][1]["connect_args"]["options"]  # OMS still 5s

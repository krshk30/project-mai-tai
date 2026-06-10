"""Unit tests for the dedicated Schwab token refresher (P0)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters import schwab_token_manager as token_manager
from project_mai_tai.services import schwab_token_refresher as refresher_mod
from project_mai_tai.services.schwab_token_refresher import (
    RefresherHealth,
    RefreshOutcome,
    SchwabTokenRefresher,
)
from project_mai_tai.settings import Settings


FIXED_NOW = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)


def _refresher(tmp_path: Path, *, store: dict | None = None, now=lambda: FIXED_NOW, **overrides):
    store_path = tmp_path / "schwab-token-store.json"
    if store is not None:
        store_path.write_text(json.dumps(store), encoding="utf-8")
    settings = Settings(
        schwab_client_id="client-id",
        schwab_client_secret="client-secret",
        schwab_token_store_path=str(store_path),
        **overrides,
    )
    return SchwabTokenRefresher(settings, now=now), store_path


def _grant_patch(monkeypatch, fake):
    monkeypatch.setattr(refresher_mod, "token_grant_request", fake)


@pytest.mark.asyncio
async def test_skips_refresh_when_access_token_is_fresh(monkeypatch, tmp_path: Path) -> None:
    far_future = (FIXED_NOW + timedelta(hours=1)).isoformat()
    refresher, _ = _refresher(
        tmp_path, store={"access_token": "a", "refresh_token": "r", "expires_at": far_future}
    )

    async def fail(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("grant must not be called when token is fresh")

    _grant_patch(monkeypatch, fail)

    assert await refresher.refresh_once() == RefreshOutcome.SKIPPED_FRESH


@pytest.mark.asyncio
async def test_refreshes_when_within_margin_and_writes_store(monkeypatch, tmp_path: Path) -> None:
    near = (FIXED_NOW + timedelta(seconds=10)).isoformat()  # within 60s margin
    refresher, store_path = _refresher(
        tmp_path, store={"access_token": "old", "refresh_token": "refresh-old", "expires_at": near}
    )

    async def fake(*, token_url, client_id, client_secret, form_data, request_timeout_seconds):
        assert form_data == {"grant_type": "refresh_token", "refresh_token": "refresh-old"}
        return (200, {}, {"access_token": "access-new", "refresh_token": "refresh-new", "expires_in": 1800})

    _grant_patch(monkeypatch, fake)

    assert await refresher.refresh_once() == RefreshOutcome.REFRESHED
    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "access-new"
    assert persisted["refresh_token"] == "refresh-new"
    assert refresher.health is RefresherHealth.HEALTHY
    assert refresher.last_refresh_at == FIXED_NOW


@pytest.mark.asyncio
async def test_refreshes_when_no_access_token_in_store(monkeypatch, tmp_path: Path) -> None:
    refresher, _ = _refresher(tmp_path, store={"refresh_token": "refresh-old"})

    async def fake(**kwargs):
        return (200, {}, {"access_token": "access-new", "expires_in": 1800})

    _grant_patch(monkeypatch, fake)
    assert await refresher.refresh_once() == RefreshOutcome.REFRESHED


@pytest.mark.asyncio
async def test_idle_when_credentials_absent(monkeypatch, tmp_path: Path) -> None:
    store_path = tmp_path / "s.json"
    store_path.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    settings = Settings(schwab_token_store_path=str(store_path))  # no client_id/secret
    refresher = SchwabTokenRefresher(settings)
    assert await refresher.refresh_once() == RefreshOutcome.IDLE_NO_CREDENTIALS


@pytest.mark.asyncio
async def test_idle_when_no_refresh_token_in_store(tmp_path: Path) -> None:
    refresher, _ = _refresher(tmp_path, store={"access_token": "a"})
    assert await refresher.refresh_once() == RefreshOutcome.IDLE_NO_CREDENTIALS


@pytest.mark.asyncio
async def test_dead_token_unchanged_disk_records_dead_and_backs_off(monkeypatch, tmp_path: Path) -> None:
    refresher, store_path = _refresher(
        tmp_path,
        store={"refresh_token": "refresh-dead", "expires_at": "2000-01-01T00:00:00+00:00"},
    )

    calls = {"n": 0}

    async def fake(**kwargs):
        calls["n"] += 1
        return (400, {}, {"error": "invalid_grant", "error_description": "dead"})

    _grant_patch(monkeypatch, fake)

    assert await refresher.refresh_once() == RefreshOutcome.DEAD_TOKEN
    # disk unchanged -> no pointless second grant call this cycle (loop backoff handles it)
    assert calls["n"] == 1
    assert refresher.dead_token_retries == 1
    assert refresher.health is RefresherHealth.RECOVERING
    # store untouched
    assert json.loads(store_path.read_text(encoding="utf-8"))["refresh_token"] == "refresh-dead"


@pytest.mark.asyncio
async def test_dead_token_escalates_to_degraded_persistent(monkeypatch, tmp_path: Path) -> None:
    refresher, _ = _refresher(
        tmp_path,
        store={"refresh_token": "refresh-dead", "expires_at": "2000-01-01T00:00:00+00:00"},
        schwab_token_refresher_max_dead_token_retries=2,
    )

    async def fake(**kwargs):
        return (400, {}, {"error": "invalid_grant", "error_description": "dead"})

    _grant_patch(monkeypatch, fake)

    assert await refresher.refresh_once() == RefreshOutcome.DEAD_TOKEN
    assert refresher.health is RefresherHealth.RECOVERING
    assert await refresher.refresh_once() == RefreshOutcome.DEAD_TOKEN
    assert refresher.health is RefresherHealth.DEGRADED_PERSISTENT
    assert refresher.dead_token_retries == 2


@pytest.mark.asyncio
async def test_dead_token_then_reauth_recovers_without_restart(monkeypatch, tmp_path: Path) -> None:
    refresher, store_path = _refresher(
        tmp_path,
        store={"refresh_token": "refresh-dead", "expires_at": "2000-01-01T00:00:00+00:00"},
    )

    async def fake(*, form_data, **kwargs):
        if form_data["refresh_token"] == "refresh-dead":
            # simulate a control re-auth writing a fresh refresh_token to disk
            store_path.write_text(
                json.dumps({"refresh_token": "refresh-fresh", "expires_at": "2000-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )
            return (400, {}, {"error": "invalid_grant", "error_description": "dead"})
        return (200, {}, {"access_token": "access-recovered", "refresh_token": "refresh-fresh", "expires_in": 1800})

    _grant_patch(monkeypatch, fake)

    assert await refresher.refresh_once() == RefreshOutcome.REFRESHED
    assert refresher.health is RefresherHealth.HEALTHY
    assert json.loads(store_path.read_text(encoding="utf-8"))["access_token"] == "access-recovered"


@pytest.mark.asyncio
async def test_success_after_degraded_resets_health(monkeypatch, tmp_path: Path) -> None:
    refresher, _ = _refresher(
        tmp_path,
        store={"refresh_token": "refresh-old", "expires_at": "2000-01-01T00:00:00+00:00"},
        schwab_token_refresher_max_dead_token_retries=1,
    )

    async def dead(**kwargs):
        return (400, {}, {"error": "invalid_grant", "error_description": "dead"})

    _grant_patch(monkeypatch, dead)
    await refresher.refresh_once()
    assert refresher.health is RefresherHealth.DEGRADED_PERSISTENT

    async def ok(**kwargs):
        return (200, {}, {"access_token": "access-new", "expires_in": 1800})

    _grant_patch(monkeypatch, ok)
    assert await refresher.refresh_once() == RefreshOutcome.REFRESHED
    assert refresher.health is RefresherHealth.HEALTHY
    assert refresher.dead_token_retries == 0


@pytest.mark.asyncio
async def test_fault_injection_env_drives_dead_token(monkeypatch, tmp_path: Path) -> None:
    """The survival-test hook: env-gated invalid_grant with no network."""
    monkeypatch.setenv(token_manager.FAULT_INJECT_ENV, "invalid_grant")
    refresher, _ = _refresher(
        tmp_path,
        store={"refresh_token": "refresh-real", "expires_at": "2000-01-01T00:00:00+00:00"},
    )
    assert await refresher.refresh_once() == RefreshOutcome.DEAD_TOKEN
    assert refresher.dead_token_retries == 1


def test_next_delay_uses_backoff_for_dead_and_error(tmp_path: Path) -> None:
    refresher, _ = _refresher(
        tmp_path,
        store={"refresh_token": "r"},
        schwab_token_refresher_check_interval_seconds=60,
        schwab_token_refresher_dead_token_backoff_seconds=30,
    )
    assert refresher._next_delay(RefreshOutcome.REFRESHED) == 60
    assert refresher._next_delay(RefreshOutcome.SKIPPED_FRESH) == 60
    assert refresher._next_delay(RefreshOutcome.DEAD_TOKEN) == 30
    assert refresher._next_delay(RefreshOutcome.ERROR) == 30
    assert refresher._next_delay(RefreshOutcome.IDLE_NO_CREDENTIALS) >= 60


@pytest.mark.asyncio
async def test_run_loop_propagates_cancellation(monkeypatch, tmp_path: Path) -> None:
    refresher, _ = _refresher(
        tmp_path, store={"refresh_token": "r", "access_token": "a", "expires_at": "2000-01-01T00:00:00+00:00"}
    )

    async def ok(**kwargs):
        return (200, {}, {"access_token": "x", "expires_in": 1800})

    _grant_patch(monkeypatch, ok)

    task = asyncio.create_task(refresher.run())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_continues_after_unexpected_error(monkeypatch, tmp_path: Path) -> None:
    refresher, _ = _refresher(
        tmp_path, store={"refresh_token": "r", "expires_at": "2000-01-01T00:00:00+00:00"}
    )
    seen = {"cycles": 0}

    async def boom(self):
        seen["cycles"] += 1
        raise RuntimeError("kaboom")

    # patch the bound method so every cycle raises; the loop must keep going
    monkeypatch.setattr(SchwabTokenRefresher, "refresh_once", boom)

    sleeps = {"n": 0}

    async def fake_sleep(_delay):
        sleeps["n"] += 1
        if sleeps["n"] >= 3:
            raise asyncio.CancelledError

    refresher._sleep = fake_sleep
    with pytest.raises(asyncio.CancelledError):
        await refresher.run()
    assert seen["cycles"] >= 3  # loop survived repeated exceptions

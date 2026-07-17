"""Characterization tests for the CURRENT Schwab token-grant behavior.

These pin the exact input/output behavior of ``SchwabBrokerAdapter``'s
refresh-grant + token-store load/save BEFORE the shared-token-manager refactor
(P0). The refactor must keep every assertion here green unchanged — that is the
"a move, not a rewrite" proof the code-review gate asks for. Do not relax these
to accommodate the refactor; if one fails after the refactor, the refactor
changed behavior.

Covers: success (refresh + rotate + persist), expiry-margin skip, forced
refresh within margin, invalid_grant (raises + keeps the cached refresh_token —
the documented __init__-cache bug), empty-access-token, and torn-read on load.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.settings import Settings


def test_safe_default_adapter_token_refresh_is_disabled():
    """PIN THE SAFE DEFAULT (2026-07-17). A default is what happens when configuration FAILS, and
    the True path is the shared-token SPOF #274 removed (the 2026-06-03..06-05 ~2.6-day outage). This
    asserts the VALUE, not a fixture derived from it — so a silent flip back to True fails loudly here
    instead of resurrecting the SPOF with a green suite. ('every threshold needs a test that pins it')."""
    assert Settings().schwab_adapter_token_refresh_enabled is False


def _adapter(tmp_path: Path, *, store: dict | None = None, **overrides) -> tuple[SchwabBrokerAdapter, Path]:
    token_store_path = tmp_path / "schwab-token-store.json"
    if store is not None:
        token_store_path.write_text(json.dumps(store), encoding="utf-8")
    # DECLARE the token-refresh mode this fixture exercises, rather than inheriting the settings.py
    # default (Rule 0: a test whose fixture comes from the thing under test cannot test it — these
    # characterization tests were silently ASSERTING the old True default; when the safe-default PR
    # flipped it to False they all broke, which is the tell). Pop-with-default so a pure-reader test
    # can still override to False (a bare `=True` here + the same key in **overrides is a TypeError).
    overrides.setdefault("schwab_adapter_token_refresh_enabled", True)
    settings = Settings(
        oms_adapter="schwab",
        schwab_client_id="client-id",
        schwab_client_secret="client-secret",
        schwab_token_store_path=str(token_store_path),
        schwab_account_hash="hash-123",
        **overrides,
    )
    return SchwabBrokerAdapter(settings), token_store_path


@pytest.mark.asyncio
async def test_refresh_success_rotates_refresh_token_and_persists(monkeypatch, tmp_path: Path) -> None:
    adapter, store_path = _adapter(
        tmp_path,
        store={"refresh_token": "refresh-old", "expires_at": "2026-03-28T13:00:00+00:00"},
    )

    seen: dict = {}

    async def fake_token_request_json(*, form_data):
        seen["form_data"] = form_data
        return (
            200,
            {},
            {
                "access_token": "access-new",
                "refresh_token": "refresh-new",
                "expires_in": 1800,
                "token_type": "Bearer",
                "scope": "api",
            },
        )

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    token = await adapter._get_access_token()

    assert token == "access-new"
    assert seen["form_data"] == {"grant_type": "refresh_token", "refresh_token": "refresh-old"}
    # rotated refresh_token captured in memory AND on disk
    assert adapter._refresh_token == "refresh-new"
    persisted = json.loads(store_path.read_text(encoding="utf-8"))
    assert persisted["access_token"] == "access-new"
    assert persisted["refresh_token"] == "refresh-new"
    assert persisted["token_type"] == "Bearer"
    assert persisted["scope"] == "api"
    assert set(persisted) == {"access_token", "refresh_token", "expires_at", "token_type", "scope", "updated_at"}
    assert adapter.last_error == ""


@pytest.mark.asyncio
async def test_fresh_access_token_skips_refresh_grant(monkeypatch, tmp_path: Path) -> None:
    far_future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    adapter, _ = _adapter(
        tmp_path,
        schwab_access_token="access-cached",
        schwab_access_token_expires_at=far_future,
        schwab_refresh_token="refresh-old",
    )

    async def fail_if_called(*, form_data):  # pragma: no cover - must not run
        raise AssertionError("refresh grant must not be called for a fresh token")

    monkeypatch.setattr(adapter, "_token_request_json", fail_if_called)

    assert await adapter._get_access_token() == "access-cached"


@pytest.mark.asyncio
async def test_access_token_within_margin_triggers_refresh(monkeypatch, tmp_path: Path) -> None:
    near_expiry = (datetime.now(UTC) + timedelta(seconds=10)).isoformat()  # < 60s margin
    adapter, _ = _adapter(
        tmp_path,
        schwab_access_token="access-cached",
        schwab_access_token_expires_at=near_expiry,
        schwab_refresh_token="refresh-old",
    )

    called = {"n": 0}

    async def fake_token_request_json(*, form_data):
        called["n"] += 1
        return (200, {}, {"access_token": "access-fresh", "expires_in": 1800})

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    assert await adapter._get_access_token() == "access-fresh"
    assert called["n"] == 1


@pytest.mark.asyncio
async def test_invalid_grant_with_no_reauth_reloads_retries_once_then_raises(
    monkeypatch, tmp_path: Path
) -> None:
    """#2 defensive reload: on the dead-token signature the adapter reloads the
    store and retries the grant exactly ONCE. With no re-auth (disk unchanged) the
    retry fails the same way and it raises — bounded to a single retry, no hot-spin."""
    adapter, store_path = _adapter(
        tmp_path,
        store={"refresh_token": "refresh-dead", "expires_at": "2000-01-01T00:00:00+00:00"},
    )

    calls = {"n": 0}

    async def fake_token_request_json(*, form_data):
        calls["n"] += 1
        assert form_data["refresh_token"] == "refresh-dead"
        return (
            400,
            {},
            {"error": "invalid_grant", "error_description": "Refresh token is invalid, expired or revoked"},
        )

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._get_access_token()

    assert "failed refreshing Schwab token" in str(excinfo.value)
    assert "invalid_grant" in str(excinfo.value)
    assert calls["n"] == 2  # original attempt + one bounded retry after reload
    assert adapter._refresh_token == "refresh-dead"  # disk unchanged → same token
    assert adapter.last_error
    assert json.loads(store_path.read_text(encoding="utf-8"))["refresh_token"] == "refresh-dead"


@pytest.mark.asyncio
async def test_invalid_grant_recovers_without_restart_when_reauth_writes_fresh_token(
    monkeypatch, tmp_path: Path
) -> None:
    """#2 defensive reload, recovery path: a control-service re-auth writes a fresh
    refresh_token to disk; the adapter's dead-token reload picks it up and the retry
    succeeds — recovery WITHOUT a process restart (same adapter instance)."""
    adapter, store_path = _adapter(
        tmp_path,
        store={"refresh_token": "refresh-dead", "expires_at": "2000-01-01T00:00:00+00:00"},
    )

    async def fake_token_request_json(*, form_data):
        if form_data["refresh_token"] == "refresh-dead":
            # simulate a re-auth landing a fresh refresh_token between attempts
            store_path.write_text(
                json.dumps({"refresh_token": "refresh-fresh", "expires_at": "2000-01-01T00:00:00+00:00"}),
                encoding="utf-8",
            )
            return (400, {}, {"error": "invalid_grant", "error_description": "dead"})
        return (
            200,
            {},
            {
                "access_token": "access-recovered",
                "refresh_token": "refresh-fresh",
                "expires_in": 1800,
                "token_type": "Bearer",
            },
        )

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    token = await adapter._get_access_token()

    assert token == "access-recovered"
    assert adapter._refresh_token == "refresh-fresh"
    assert json.loads(store_path.read_text(encoding="utf-8"))["access_token"] == "access-recovered"
    assert adapter.last_error == ""


@pytest.mark.asyncio
async def test_refresh_with_empty_access_token_raises(monkeypatch, tmp_path: Path) -> None:
    adapter, _ = _adapter(
        tmp_path,
        store={"refresh_token": "refresh-old", "expires_at": "2000-01-01T00:00:00+00:00"},
    )

    async def fake_token_request_json(*, form_data):
        return (200, {}, {"access_token": "", "expires_in": 1800})

    monkeypatch.setattr(adapter, "_token_request_json", fake_token_request_json)

    with pytest.raises(RuntimeError) as excinfo:
        await adapter._get_access_token()
    assert "no access_token" in str(excinfo.value)


@pytest.mark.asyncio
async def test_pure_reader_mode_reloads_from_disk_and_never_grants(monkeypatch, tmp_path: Path) -> None:
    """Single-writer mode (schwab_adapter_token_refresh_enabled=False): on a forced
    refresh the adapter reloads the refresher's token from disk and NEVER runs a
    grant or writes the store — a pure reader."""
    adapter, store_path = _adapter(
        tmp_path,
        store={"access_token": "v1", "refresh_token": "r", "expires_at": "2000-01-01T00:00:00+00:00"},
        schwab_adapter_token_refresh_enabled=False,
    )
    # the refresher updates the on-disk token out of band
    store_path.write_text(
        json.dumps({"access_token": "v2", "refresh_token": "r", "expires_at": "2099-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )

    async def fail(*, form_data):  # pragma: no cover - must not run
        raise AssertionError("pure-reader mode must not call the refresh grant")

    monkeypatch.setattr(adapter, "_token_request_json", fail)

    assert await adapter._get_access_token(force_refresh=True) == "v2"


@pytest.mark.asyncio
async def test_pure_reader_mode_warns_loudly_when_disk_token_is_stale(
    monkeypatch, tmp_path: Path, caplog
) -> None:
    """If the refresher is down, the on-disk token goes past expires_at. Pure-reader
    mode must log [SCHWAB-TOKEN-STALE] (loud diagnosis) and still return it, rather
    than emit a silent token that triggers a downstream 401 storm."""
    adapter, _ = _adapter(
        tmp_path,
        store={"access_token": "stale-token", "refresh_token": "r", "expires_at": "2000-01-01T00:00:00+00:00"},
        schwab_adapter_token_refresh_enabled=False,
    )

    async def fail(*, form_data):  # pragma: no cover - must not run
        raise AssertionError("pure-reader mode must not call the refresh grant")

    monkeypatch.setattr(adapter, "_token_request_json", fail)

    with caplog.at_level(logging.WARNING):
        token = await adapter._get_access_token(force_refresh=True)

    assert token == "stale-token"
    assert any("SCHWAB-TOKEN-STALE" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_pure_reader_mode_raises_when_disk_has_no_access_token(tmp_path: Path) -> None:
    adapter, _ = _adapter(
        tmp_path,
        store={"refresh_token": "r"},
        schwab_adapter_token_refresh_enabled=False,
    )
    with pytest.raises(RuntimeError) as excinfo:
        await adapter._get_access_token(force_refresh=True)
    assert "refresher-owned" in str(excinfo.value)


def test_torn_read_on_load_does_not_crash_init(tmp_path: Path) -> None:
    token_store_path = tmp_path / "schwab-token-store.json"
    token_store_path.write_text('{"access_token": "partial', encoding="utf-8")  # truncated JSON

    adapter = SchwabBrokerAdapter(
        Settings(
            oms_adapter="schwab",
            schwab_client_id="client-id",
            schwab_client_secret="client-secret",
            schwab_token_store_path=str(token_store_path),
            schwab_account_hash="hash-123",
            schwab_refresh_token="refresh-from-settings",
        )
    )

    # torn read is swallowed; falls back to the settings-provided refresh_token
    assert adapter._refresh_token == "refresh-from-settings"

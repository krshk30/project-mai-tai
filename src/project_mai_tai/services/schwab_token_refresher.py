"""Dedicated Schwab token refresher (P0 — Workstream B resilience half).

Runs as a background asyncio task inside the **control service** (which already
owns the OAuth write side: authorize -> exchange -> persist). It keeps the
on-disk ``access_token`` fresh **proactively on its own timer**, fully
independent of strategy registrations, OMS broker-sync, or account hashes.

Why it exists: post-cutover there is NO token-refresh path in config — the OMS
broker-sync short-circuits on a missing account hash before it ever refreshes,
and the only live Schwab bot (v2) reads the token disk-fresh but never refreshes
it. Before this component, token freshness depended on incidental
retired/dormant-bot plumbing (a trap that caused the 2026-06-09 SPOF). This
refresher is the single owner of token freshness.

Single-writer invariant: once this is live, the OMS adapter's incidental refresh
is disabled (it becomes a pure disk reader). All writers use atomic writes.

Loud by design: every successful refresh logs ``[SCHWAB-TOKEN-REFRESHED]`` — the
2026-06-09 zombie was invisible precisely because refreshes were silent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import Enum
from pathlib import Path

from project_mai_tai.broker_adapters.schwab_token_manager import (
    SchwabTokenError,
    atomic_write_json,
    build_token_store_document,
    is_dead_token_payload,
    parse_datetime,
    parse_token_grant_response,
    read_token_store,
    token_grant_request,
)
from project_mai_tai.settings import Settings


logger = logging.getLogger(__name__)


class RefreshOutcome(str, Enum):
    REFRESHED = "refreshed"
    SKIPPED_FRESH = "skipped_fresh"
    IDLE_NO_CREDENTIALS = "idle_no_credentials"
    DEAD_TOKEN = "dead_token"
    ERROR = "error"


class RefresherHealth(str, Enum):
    HEALTHY = "healthy"
    RECOVERING = "recovering"
    DEGRADED_PERSISTENT = "degraded-persistent"


class SchwabTokenRefresher:
    def __init__(
        self,
        settings: Settings,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._sleep = sleep
        self._now = now

        self._enabled = settings.schwab_token_refresher_enabled
        self._token_url = settings.schwab_token_url
        self._client_id = settings.schwab_client_id
        self._client_secret = settings.schwab_client_secret
        self._token_store_path = (
            Path(settings.schwab_token_store_path).expanduser()
            if settings.schwab_token_store_path
            else None
        )
        self._request_timeout = settings.schwab_request_timeout_seconds
        self._check_interval = max(5, settings.schwab_token_refresher_check_interval_seconds)
        self._refresh_margin = max(0, settings.schwab_token_refresh_margin_seconds)
        self._dead_token_backoff = max(5, settings.schwab_token_refresher_dead_token_backoff_seconds)
        self._max_dead_token_retries = max(1, settings.schwab_token_refresher_max_dead_token_retries)
        self._idle_interval = max(self._check_interval, 60)

        self._dead_token_retries = 0
        self._health = RefresherHealth.HEALTHY
        self._last_refresh_at: datetime | None = None
        self._last_error = ""
        self._idle_logged = False

    # ----- observable state (for the visibility half / heartbeat / tests) -----
    @property
    def health(self) -> RefresherHealth:
        return self._health

    @property
    def last_refresh_at(self) -> datetime | None:
        return self._last_refresh_at

    @property
    def dead_token_retries(self) -> int:
        return self._dead_token_retries

    def status(self) -> dict[str, object]:
        return {
            "enabled": self._enabled,
            "health": self._health.value,
            "dead_token_retries": self._dead_token_retries,
            "last_refresh_at": self._last_refresh_at.isoformat() if self._last_refresh_at else None,
            "last_error": self._last_error,
        }

    # ----- the loop -----
    async def run(self) -> None:
        if not self._enabled:
            logger.info("[SCHWAB-TOKEN-REFRESHER] disabled by config; not starting")
            return
        if self._token_store_path is None:
            logger.warning(
                "[SCHWAB-TOKEN-REFRESHER] no token store path configured; not starting"
            )
            return
        logger.info(
            "[SCHWAB-TOKEN-REFRESHER] starting (check_interval=%ss, refresh_margin=%ss, store=%s)",
            self._check_interval,
            self._refresh_margin,
            self._token_store_path,
        )
        try:
            while True:
                try:
                    outcome = await self.refresh_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception(
                        "[SCHWAB-TOKEN-REFRESHER] unexpected error in refresh cycle; loop continues"
                    )
                    outcome = RefreshOutcome.ERROR
                await self._sleep(self._next_delay(outcome))
        except asyncio.CancelledError:
            logger.info("[SCHWAB-TOKEN-REFRESHER] cancelled; shutting down")
            raise

    def _next_delay(self, outcome: RefreshOutcome) -> float:
        if outcome is RefreshOutcome.IDLE_NO_CREDENTIALS:
            return float(self._idle_interval)
        if outcome in {RefreshOutcome.DEAD_TOKEN, RefreshOutcome.ERROR}:
            return float(self._dead_token_backoff)
        return float(self._check_interval)

    # ----- one cycle (pure-ish; directly unit-tested) -----
    async def refresh_once(self) -> RefreshOutcome:
        if not self._client_id or not self._client_secret:
            self._log_idle("missing Schwab client_id/client_secret")
            return RefreshOutcome.IDLE_NO_CREDENTIALS

        store = read_token_store(self._token_store_path) or {}
        refresh_token = str(store.get("refresh_token", "")).strip()
        if not refresh_token:
            self._log_idle("no refresh_token in token store")
            return RefreshOutcome.IDLE_NO_CREDENTIALS

        self._idle_logged = False
        if not self._needs_refresh(store):
            return RefreshOutcome.SKIPPED_FRESH

        status_code, _headers, payload = await self._grant(refresh_token)

        if is_dead_token_payload(payload):
            # A control-service re-auth may have just written a fresh refresh_token
            # to disk. Reload and retry ONCE — but only if the token actually
            # changed (don't hammer Schwab with a known-dead token; the loop's
            # bounded backoff handles the unchanged case).
            store = read_token_store(self._token_store_path) or {}
            reloaded = str(store.get("refresh_token", "")).strip()
            if reloaded and reloaded != refresh_token:
                logger.warning(
                    "[SCHWAB-TOKEN-RELOADED] refresher picked up a new refresh_token after "
                    "dead-token; retrying refresh"
                )
                refresh_token = reloaded
                status_code, _headers, payload = await self._grant(refresh_token)

        try:
            result = parse_token_grant_response(
                payload, status_code=status_code, previous_refresh_token=refresh_token
            )
        except SchwabTokenError as exc:
            if is_dead_token_payload(getattr(exc, "payload", None)):
                self._record_dead_token(str(exc))
                return RefreshOutcome.DEAD_TOKEN
            self._last_error = str(exc)
            logger.warning("[SCHWAB-TOKEN-REFRESH-ERROR] %s", exc)
            return RefreshOutcome.ERROR

        atomic_write_json(self._token_store_path, build_token_store_document(result))
        self._record_success(result.expires_at)
        return RefreshOutcome.REFRESHED

    async def _grant(self, refresh_token: str):
        return await token_grant_request(
            token_url=self._token_url,
            client_id=self._client_id,
            client_secret=self._client_secret,
            form_data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            request_timeout_seconds=self._request_timeout,
        )

    def _needs_refresh(self, store: dict[str, object]) -> bool:
        access_token = str(store.get("access_token", "")).strip()
        if not access_token:
            return True
        expires_at = parse_datetime(store.get("expires_at"))
        if expires_at is None:
            return True
        return self._now() >= expires_at - timedelta(seconds=self._refresh_margin)

    def _record_success(self, expires_at: datetime | None) -> None:
        was_degraded = self._health is not RefresherHealth.HEALTHY
        self._dead_token_retries = 0
        self._health = RefresherHealth.HEALTHY
        self._last_refresh_at = self._now()
        self._last_error = ""
        if was_degraded:
            logger.info("[SCHWAB-TOKEN-REFRESHER-RECOVERED] token refresh succeeded; back to healthy")
        logger.info(
            "[SCHWAB-TOKEN-REFRESHED] access_token refreshed; expires_at=%s",
            expires_at.isoformat() if expires_at else "unknown",
        )

    def _record_dead_token(self, message: str) -> None:
        self._dead_token_retries += 1
        self._last_error = message
        if self._dead_token_retries >= self._max_dead_token_retries:
            self._health = RefresherHealth.DEGRADED_PERSISTENT
            logger.error(
                "[SCHWAB-TOKEN-REFRESHER-DEGRADED-PERSISTENT] Schwab token DEAD after %d consecutive "
                "refresh attempts — re-auth required (the dedicated refresher is alive and retrying "
                "with backoff; v2 will lose its token at the next access-token expiry). reason=%s",
                self._dead_token_retries,
                message,
            )
        else:
            self._health = RefresherHealth.RECOVERING
            logger.warning(
                "[SCHWAB-TOKEN-DEAD] refresh grant returned a dead-token signature "
                "(attempt %d/%d); backing off. reason=%s",
                self._dead_token_retries,
                self._max_dead_token_retries,
                message,
            )

    def _log_idle(self, reason: str) -> None:
        if not self._idle_logged:
            logger.info("[SCHWAB-TOKEN-REFRESHER-IDLE] %s; waiting", reason)
            self._idle_logged = True

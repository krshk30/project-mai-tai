"""Shared Schwab token-lifecycle helpers.

Extracted (P0) from ``SchwabBrokerAdapter`` so the dedicated token refresher in
the control service and the OMS adapter use ONE implementation of the
refresh-grant + token-store read/write. This is a *move, not a rewrite*: the
logic mirrors the adapter's prior inline implementation exactly, and the
adapter delegates to these functions. The characterization tests in
``test_schwab_token_grant_characterization.py`` pin behavior-identity.

Writers go through :func:`atomic_write_json` (temp + ``os.replace``) so a reader
never observes a torn token store. Readers stay torn-safe (:func:`read_token_store`).

Env-gated fault injection (default OFF) drives the survival test: set
``MAI_TAI_SCHWAB_TOKEN_FAULT_INJECT=invalid_grant`` to make the refresh grant
return a simulated dead-token 400 without touching the network.
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import urlencode


logger = logging.getLogger(__name__)

FAULT_INJECT_ENV = "MAI_TAI_SCHWAB_TOKEN_FAULT_INJECT"


class SchwabTokenError(RuntimeError):
    """Raised when a refresh grant cannot produce a usable access token.

    Subclasses ``RuntimeError`` so existing ``except RuntimeError`` / test
    expectations around the adapter grant keep holding.
    """

    def __init__(self, message: str, *, payload: object = None) -> None:
        super().__init__(message)
        self.payload = payload


def is_dead_token_payload(payload: object) -> bool:
    """True for Schwab's dead-refresh-token signature (invalid_grant /
    unsupported_token_type) — the signal to reload-from-disk and retry."""
    if not isinstance(payload, dict):
        return False
    error = str(payload.get("error", "") or "").strip().lower()
    return error in {"invalid_grant", "unsupported_token_type"}


def _fault_inject_mode() -> str:
    return (os.environ.get(FAULT_INJECT_ENV, "") or "").strip().lower()


def atomic_write_json(path: Path, document: dict[str, object]) -> None:
    """Write ``document`` as pretty JSON atomically.

    Temp file in the same directory + ``os.replace`` (atomic on the same
    filesystem, POSIX and Windows) so a concurrent reader sees either the old
    or the new file, never a partial write.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(document, indent=2, sort_keys=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def read_token_store(path: Path | None) -> dict[str, object] | None:
    """Torn-safe read of the token store. Returns the parsed dict, or None on a
    missing/unreadable/partial file (logged, never raised)."""
    if path is None:
        return None
    path = Path(path)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("failed reading Schwab token store %s", path)
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def parse_datetime(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def extract_error_reason(payload: object) -> str:
    if isinstance(payload, dict):
        error = str(payload.get("error", "") or "").strip()
        error_description = str(payload.get("error_description", "") or "").strip()
        if error and error_description:
            return f"{error}: {error_description}"
        for key in ("message", "error", "error_description", "statusDescription", "description"):
            value = payload.get(key)
            if value:
                return str(value)
    if payload is None:
        return "unknown Schwab error"
    return str(payload)


def decode_json(raw: str) -> object:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"message": raw}


def decode_http_body(raw: bytes, headers: dict[str, str]) -> str:
    if not raw:
        return ""
    encoding = str(headers.get("Content-Encoding", "") or "").strip().lower()
    if encoding == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", errors="replace")


def _blocking_token_request(
    url: str,
    headers: dict[str, str],
    data: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], object]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response_headers = dict(response.headers.items())
            raw = decode_http_body(response.read(), response_headers)
            return response.getcode(), response_headers, decode_json(raw)
    except urllib.error.HTTPError as exc:
        response_headers = dict(exc.headers.items())
        raw = decode_http_body(exc.read() if exc.fp else b"", response_headers)
        return exc.code, response_headers, decode_json(raw)
    except Exception as exc:  # pragma: no cover - exercised via rejection fallback
        return 599, {}, {"message": str(exc)}


async def token_grant_request(
    *,
    token_url: str,
    client_id: str | None,
    client_secret: str | None,
    form_data: dict[str, str],
    request_timeout_seconds: float,
) -> tuple[int, dict[str, str], object]:
    """Perform a Basic-auth OAuth token grant (refresh or authorization_code).

    Mirrors the adapter's prior ``_token_request_json`` exactly. Honors the
    env-gated fault-injection hook (default off) for the survival test.
    """
    if not client_id or not client_secret:
        raise RuntimeError("missing Schwab client_id or client_secret")

    if _fault_inject_mode() == "invalid_grant":
        logger.warning("[SCHWAB-TOKEN-FAULT-INJECT] returning simulated invalid_grant (env %s)", FAULT_INJECT_ENV)
        return (
            400,
            {},
            {
                "error": "invalid_grant",
                "error_description": "injected fault: Refresh token is invalid, expired or revoked",
            },
        )

    basic_auth = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    headers = {
        "Authorization": f"Basic {basic_auth}",
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = urlencode(form_data).encode("utf-8")
    return await asyncio.to_thread(
        _blocking_token_request, token_url, headers, data, request_timeout_seconds
    )


@dataclass(frozen=True)
class TokenGrantResult:
    access_token: str
    refresh_token: str
    expires_at: datetime | None
    token_type: object
    scope: object
    raw: dict[str, object]
    # Refresh-token expiry clock (~7d, non-rotating). Only a re-auth
    # (authorization_code grant) carries `refresh_token_expires_in`; the periodic
    # refresh grant omits it, so these are CARRIED FORWARD from the prior store on
    # a refresh and only reset on a genuine re-auth. Used by the expiry-warning cron.
    refresh_token_expires_at: datetime | None = None
    refresh_token_obtained_at: datetime | None = None


def parse_token_grant_response(
    payload: object,
    *,
    status_code: int,
    previous_refresh_token: str,
    previous_refresh_token_expires_at: datetime | None = None,
    previous_refresh_token_obtained_at: datetime | None = None,
) -> TokenGrantResult:
    """Parse a refresh-grant response into a :class:`TokenGrantResult`.

    Mirrors the adapter's prior inline parse exactly: raises
    :class:`SchwabTokenError` (a RuntimeError) on a >=400 status, a
    non-dict payload, or an empty access token; carries the prior
    refresh_token forward when the response omits a rotated one.

    Refresh-token expiry: if the payload carries ``refresh_token_expires_in`` (>0)
    — i.e. a re-auth — set a fresh expiry (now + it) and obtained_at=now; otherwise
    (routine refresh) carry the previous values forward unchanged.
    """
    if status_code >= 400 or not isinstance(payload, dict):
        raise SchwabTokenError(
            f"failed refreshing Schwab token: {extract_error_reason(payload)}",
            payload=payload,
        )
    access_token = str(payload.get("access_token", "")).strip()
    if not access_token:
        raise SchwabTokenError("Schwab token refresh returned no access_token", payload=payload)
    now = datetime.now(UTC)
    rotated = str(payload.get("refresh_token", "")).strip()
    refresh_token = rotated or previous_refresh_token
    expires_in = int(payload.get("expires_in", 0) or 0)
    expires_at = now + timedelta(seconds=expires_in) if expires_in > 0 else None
    rt_expires_in = int(payload.get("refresh_token_expires_in", 0) or 0)
    if rt_expires_in > 0:
        refresh_token_expires_at: datetime | None = now + timedelta(seconds=rt_expires_in)
        refresh_token_obtained_at: datetime | None = now
    else:
        refresh_token_expires_at = previous_refresh_token_expires_at
        refresh_token_obtained_at = previous_refresh_token_obtained_at
    return TokenGrantResult(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=payload.get("token_type"),
        scope=payload.get("scope"),
        raw=payload,
        refresh_token_expires_at=refresh_token_expires_at,
        refresh_token_obtained_at=refresh_token_obtained_at,
    )


def build_token_store_document(result: TokenGrantResult) -> dict[str, object]:
    """The on-disk token-store document for a refreshed token. Matches the key
    set and shapes the adapter / control re-auth callback already write."""
    document: dict[str, object] = {
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "expires_at": result.expires_at.isoformat() if result.expires_at is not None else None,
        "token_type": result.token_type,
        "scope": result.scope,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    # Preserve the refresh-token expiry clock across routine refreshes (only present
    # once a re-auth has captured it; None-safe for pre-capture stores).
    if result.refresh_token_expires_at is not None:
        document["refresh_token_expires_at"] = result.refresh_token_expires_at.isoformat()
    if result.refresh_token_obtained_at is not None:
        document["refresh_token_obtained_at"] = result.refresh_token_obtained_at.isoformat()
    return document

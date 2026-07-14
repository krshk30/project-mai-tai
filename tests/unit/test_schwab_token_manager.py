"""Tests for the shared Schwab token-manager helpers (P0)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from project_mai_tai.broker_adapters.schwab_token_manager import (
    SchwabTokenError,
    TokenGrantResult,
    atomic_write_json,
    build_token_store_document,
    is_dead_token_payload,
    parse_token_grant_response,
    read_token_store,
)


def test_atomic_write_creates_parents_and_valid_json(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "dir" / "token.json"
    atomic_write_json(target, {"a": 1, "b": "x"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1, "b": "x"}


def test_atomic_write_replaces_existing_and_leaves_no_temp(tmp_path: Path) -> None:
    target = tmp_path / "token.json"
    target.write_text("old", encoding="utf-8")
    atomic_write_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}
    # no leftover temp files in the directory
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != "token.json"]
    assert leftovers == []


def test_read_token_store_missing_returns_none(tmp_path: Path) -> None:
    assert read_token_store(tmp_path / "nope.json") is None
    assert read_token_store(None) is None


def test_read_token_store_torn_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "torn.json"
    p.write_text('{"access_token": "partial', encoding="utf-8")  # truncated
    assert read_token_store(p) is None


def test_read_token_store_non_dict_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_token_store(p) is None


def test_read_token_store_valid_returns_dict(tmp_path: Path) -> None:
    p = tmp_path / "ok.json"
    p.write_text(json.dumps({"refresh_token": "r"}), encoding="utf-8")
    assert read_token_store(p) == {"refresh_token": "r"}


def test_is_dead_token_payload() -> None:
    assert is_dead_token_payload({"error": "invalid_grant"})
    assert is_dead_token_payload({"error": "unsupported_token_type"})
    assert not is_dead_token_payload({"error": "rate_limited"})
    assert not is_dead_token_payload({"access_token": "x"})
    assert not is_dead_token_payload("not a dict")


def test_parse_grant_carries_forward_refresh_token_when_omitted() -> None:
    result = parse_token_grant_response(
        {"access_token": "a", "expires_in": 1800}, status_code=200, previous_refresh_token="keep-me"
    )
    assert result.refresh_token == "keep-me"
    assert result.access_token == "a"
    assert result.expires_at is not None


def test_parse_grant_raises_on_dead_token_with_payload() -> None:
    with pytest.raises(SchwabTokenError) as excinfo:
        parse_token_grant_response(
            {"error": "invalid_grant", "error_description": "dead"},
            status_code=400,
            previous_refresh_token="r",
        )
    assert is_dead_token_payload(excinfo.value.payload)
    assert "invalid_grant" in str(excinfo.value)


def test_parse_grant_raises_on_empty_access_token() -> None:
    with pytest.raises(SchwabTokenError):
        parse_token_grant_response(
            {"access_token": "", "expires_in": 1800}, status_code=200, previous_refresh_token="r"
        )


def test_build_token_store_document_shape() -> None:
    result = TokenGrantResult(
        access_token="a",
        refresh_token="r",
        expires_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
        token_type="Bearer",
        scope="api",
        raw={},
    )
    doc = build_token_store_document(result)
    assert set(doc) == {"access_token", "refresh_token", "expires_at", "token_type", "scope", "updated_at"}
    assert doc["access_token"] == "a"
    assert doc["expires_at"] == "2026-06-10T12:00:00+00:00"


# --- refresh-token expiry capture (2026-07-14) ---


def test_parse_grant_captures_refresh_token_expires_in() -> None:
    """An auth-code grant carries refresh_token_expires_in -> fresh expiry + obtained_at."""
    before = datetime.now(UTC)
    result = parse_token_grant_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 1800,
         "refresh_token_expires_in": 7 * 24 * 3600},
        status_code=200,
        previous_refresh_token="old",
    )
    after = datetime.now(UTC)
    assert result.refresh_token_obtained_at is not None
    assert before <= result.refresh_token_obtained_at <= after
    # expires_at ~ now + 7d (allow a few seconds of test execution)
    delta_days = (result.refresh_token_expires_at - result.refresh_token_obtained_at).total_seconds() / 86400
    assert abs(delta_days - 7) < 0.001


def test_parse_grant_carries_forward_refresh_expiry_when_omitted() -> None:
    """A routine refresh grant omits refresh_token_expires_in -> carry the prior clock."""
    prev_exp = datetime(2026, 7, 21, 11, 43, tzinfo=UTC)
    prev_obt = datetime(2026, 7, 14, 11, 43, tzinfo=UTC)
    result = parse_token_grant_response(
        {"access_token": "a", "expires_in": 1800},  # no refresh_token_expires_in
        status_code=200,
        previous_refresh_token="r",
        previous_refresh_token_expires_at=prev_exp,
        previous_refresh_token_obtained_at=prev_obt,
    )
    assert result.refresh_token_expires_at == prev_exp
    assert result.refresh_token_obtained_at == prev_obt


def test_build_doc_includes_refresh_expiry_when_present_and_omits_when_none() -> None:
    exp = datetime(2026, 7, 21, 11, 43, tzinfo=UTC)
    with_exp = build_token_store_document(TokenGrantResult(
        access_token="a", refresh_token="r", expires_at=None, token_type="Bearer",
        scope="api", raw={}, refresh_token_expires_at=exp,
        refresh_token_obtained_at=datetime(2026, 7, 14, 11, 43, tzinfo=UTC)))
    assert with_exp["refresh_token_expires_at"] == exp.isoformat()
    assert with_exp["refresh_token_obtained_at"] == "2026-07-14T11:43:00+00:00"

    without = build_token_store_document(TokenGrantResult(
        access_token="a", refresh_token="r", expires_at=None, token_type="Bearer",
        scope="api", raw={}))  # both refresh-expiry fields default None
    assert "refresh_token_expires_at" not in without
    assert "refresh_token_obtained_at" not in without

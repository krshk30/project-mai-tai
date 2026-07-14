"""Threshold tests for the Schwab refresh-token expiry warning check
(ops/health/schwab_token_expiry_check.py — a standalone stdlib script, imported by path)."""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

UTC = timezone.utc
_PATH = Path(__file__).resolve().parents[2] / "ops" / "health" / "schwab_token_expiry_check.py"
_spec = importlib.util.spec_from_file_location("schwab_token_expiry_check", _PATH)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)

NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)


def test_verdict_green_over_48h() -> None:
    code, level, _ = mod.verdict(NOW + timedelta(days=5), NOW)
    assert (code, level) == (0, "GREEN")


def test_verdict_amber_within_48h() -> None:
    code, level, _ = mod.verdict(NOW + timedelta(hours=40), NOW)
    assert (code, level) == (1, "AMBER")


def test_verdict_red_within_12h() -> None:
    code, level, _ = mod.verdict(NOW + timedelta(hours=6), NOW)
    assert (code, level) == (2, "RED")


def test_verdict_red_when_expired() -> None:
    code, level, _ = mod.verdict(NOW - timedelta(hours=1), NOW)
    assert (code, level) == (2, "RED")


def test_verdict_amber_when_unknown() -> None:
    code, level, detail = mod.verdict(None, NOW)
    assert (code, level) == (1, "AMBER")
    assert "UNKNOWN" in detail


def test_boundary_48h_is_amber_and_just_over_is_green() -> None:
    assert mod.verdict(NOW + timedelta(hours=48), NOW)[1] == "AMBER"
    assert mod.verdict(NOW + timedelta(hours=48, seconds=1), NOW)[1] == "GREEN"

"""The env-vs-default drift tool must report a divergence, and must not cry wolf on feature flags.

⭐ WHY IT EXISTS: reading `settings.py` gave the WRONG live value three times in one evening
(2026-07-28), once causing a threshold decision on a false premise. Aligning every default is not
the fix (one flip broke 43 tests); making the divergence cheap to SEE is.
"""
from __future__ import annotations

from unittest.mock import patch

from project_mai_tai.settings import Settings

import ops.health.env_default_drift as mod


def _live(**over):
    return Settings(**over)


def test_numeric_divergence_is_reported() -> None:
    """THE HAZARD CLASS: a number that differs reads as plausible and is wrong."""
    with patch.object(mod, "get_settings", lambda: _live(strategy_schwab_1m_v2_atr_flip_quantity=2)):
        rows = dict((n, (d, c)) for n, d, c in mod.drift(numeric_only=True))
    assert "strategy_schwab_1m_v2_atr_flip_quantity" in rows
    default, current = rows["strategy_schwab_1m_v2_atr_flip_quantity"]
    assert current == 2 and default != 2


def test_a_plain_feature_flag_is_NOT_in_the_numeric_view() -> None:
    """A bool going False->True is an ordinary enable and reads honestly — it must not add noise,
    or the signal drowns (74 total divergences vs 14 numeric ones on the live box)."""
    with patch.object(mod, "get_settings", lambda: _live(orb_enabled=True)):
        names = [n for n, _, _ in mod.drift(numeric_only=True)]
    assert "orb_enabled" not in names


def test_all_view_does_include_flags() -> None:
    with patch.object(mod, "get_settings", lambda: _live(orb_enabled=True)):
        names = [n for n, _, _ in mod.drift(numeric_only=False)]
    assert "orb_enabled" in names


def test_no_drift_when_live_equals_defaults() -> None:
    """PINS THE OTHER DIRECTION — a tool that always reports drift is as useless as one that never
    does. Bare Settings() in CI has no env, so the numeric view must be empty."""
    with patch.object(mod, "get_settings", lambda: Settings()):
        assert mod.drift(numeric_only=True) == []

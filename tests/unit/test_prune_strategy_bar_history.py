"""The dead-bot pruner must never be able to delete a code something still reads.

⭐ CONTEXT (measured 2026-07-29): `strategy_bar_history` is 2,103,233 rows / 1955 MB, and 52% of it
belongs to six bots that stopped writing between April and 2026-06-09. Only TWO codes are ever read
back anywhere in the codebase — `schwab_1m_v2` (the backtest bar source) and `polygon_30s` — so the
right axis is OWNERSHIP, not age.

⛔ This is a DELETE against real history, so the tests are about what it REFUSES to do.
"""
from __future__ import annotations

from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "prune_strategy_bar_history.py"

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("_prune_sbh", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mod)


def test_the_backtest_bar_source_is_protected() -> None:
    """⛔ THE ONE THAT MATTERS. `schwab_1m_v2` IS the backtest bar source — deleting it would
    destroy the history the backtest-vs-live parity work depends on."""
    assert "schwab_1m_v2" in mod.PROTECTED_CODES
    assert "schwab_1m_v2" not in mod.DEAD_CODES


def test_the_paper_bot_is_protected_too() -> None:
    assert "polygon_30s" in mod.PROTECTED_CODES
    assert "polygon_30s" not in mod.DEAD_CODES


def test_dead_and_protected_never_overlap() -> None:
    assert not (set(mod.DEAD_CODES) & set(mod.PROTECTED_CODES))


def test_the_dead_list_is_the_reviewed_six() -> None:
    """PINS THE LIST. Adding a code here is a decision to delete real data; it must be deliberate."""
    assert set(mod.DEAD_CODES) == {
        "macd_30s", "schwab_1m", "webull_30s", "tos", "macd_1m", "macd_30s_reclaim",
    }


@pytest.mark.parametrize("bad", ["schwab_1m_v2", "polygon_30s"])
def test_refuses_a_protected_code(bad: str) -> None:
    """A denylist is only safe if --codes cannot smuggle a live code past it."""
    with pytest.raises(SystemExit, match="still READ"):
        mod.validate_codes((bad,))


def test_refuses_an_unreviewed_code() -> None:
    """A code nobody reviewed might be live; refusing is the safe default."""
    with pytest.raises(SystemExit, match="reviewed dead list"):
        mod.validate_codes(("some_new_bot",))


def test_accepts_the_reviewed_dead_codes() -> None:
    """PINS THE OTHER DIRECTION — a guard that refuses everything is useless."""
    assert mod.validate_codes(mod.DEAD_CODES) == mod.DEAD_CODES


def test_one_bad_code_blocks_the_whole_run() -> None:
    """Mixed input must fail closed, not delete the valid half."""
    with pytest.raises(SystemExit):
        mod.validate_codes(("macd_30s", "schwab_1m_v2"))


def test_dry_run_is_the_default() -> None:
    """Deleting must require an explicit --go; the safe path must be the one you get by accident."""
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"--go", action="store_true"' in src
    assert "--dry-run" not in src, "dry-run must be the DEFAULT, not an opt-in flag"

"""Durable controls for corrections to append-only handoff history."""
from __future__ import annotations

from pathlib import Path


DOC = Path(__file__).parents[2] / "docs" / "handoff-log.md"
MARKER = "[HANDOFF-CORRECTION-777]"


def _correction_is_complete(text: str) -> bool:
    """True only for the exact polarity and all four blockers, never for the old claim alone."""
    lines = [line for line in text.splitlines() if MARKER in line]
    if len(lines) != 1:
        return False
    marker = lines[0]
    return all(token in marker for token in (
        "trigger=post-merge-independent-review",
        "original_claim_valid=0",
        "blockers_at_merge=4",
        "correction_recorded=1",
    )) and all(token in text for token in (
        "carrier-blob equality with final main",
        "only the manifest carrier was pinned",
        "partial-rotation retry dropped companion PRs",
        "selftest was",
        "80/4",
    ))


def test_pr777_false_claim_has_one_complete_success_marker() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert _correction_is_complete(text) is True
    # Append-only means the historical statement remains visible and is corrected, not rewritten.
    assert "The requirement was unsatisfiable, not merely unmet." in text


def test_known_bad_old_record_without_correction_stays_false() -> None:
    """Firing control: the exact pre-correction shape must not be accepted as corrected."""
    text = DOC.read_text(encoding="utf-8")
    before_correction = text.split("> **CORRECTION (independent review, 2026-08-25):**", 1)[0]
    assert _correction_is_complete(before_correction) is False


def test_wrong_polarity_stays_quiet() -> None:
    """Quiet control: a marker that blesses the original claim must never count as success."""
    text = DOC.read_text(encoding="utf-8").replace(
        "original_claim_valid=0", "original_claim_valid=1"
    )
    assert _correction_is_complete(text) is False


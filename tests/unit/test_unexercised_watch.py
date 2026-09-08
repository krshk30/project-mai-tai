"""Controls for the unexercised-condition watcher.

⛔ The defect this file exists to prevent is a FALSE CLEAN: a watcher that reports a tidy zero when
its query failed, or when it never had a denominator at all. Every test below must go RED if that
distinction is collapsed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

def _locate() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "ops" / "health" / "unexercised_watch.py"
        if candidate.is_file():
            return candidate
    raise AssertionError("unexercised_watch.py not found above " + str(here))


_SPEC = importlib.util.spec_from_file_location("unexercised_watch", _locate())
uw = importlib.util.module_from_spec(_SPEC)
# @dataclass resolves cls.__module__ through sys.modules; a manually loaded module must be
# registered there FIRST or the decorator raises during import.
sys.modules["unexercised_watch"] = uw
_SPEC.loader.exec_module(uw)


@pytest.mark.parametrize(
    ("fired", "denominator", "expected"),
    [
        (1, 100, uw.OCCURRED),
        (5, 0, uw.OCCURRED),          # a real occurrence outranks a missing denominator
        (0, 100, uw.NEVER_OCCURRED),  # a MEASURED zero
        (0, 1, uw.NEVER_OCCURRED),    # one observation is still a denominator
        (0, 0, uw.NEVER_LOOKED),      # ⛔ the whole point: this is NOT a clean zero
    ],
)
def test_zero_is_only_a_result_against_a_denominator(fired, denominator, expected):
    assert uw.classify(fired, denominator) == expected


def test_never_looked_is_not_never_occurred():
    """⛔ The discriminating case. If these two ever collapse, the watcher can report 'clean' for a
    condition it has never once been able to see."""
    assert uw.classify(0, 0) != uw.classify(0, 1)


def test_a_failed_query_is_could_not_tell_not_a_zero(tmp_path, monkeypatch):
    """A raising condition must NOT be reported as NEVER_LOOKED or NEVER_OCCURRED."""
    def boom():
        raise RuntimeError("psql failed: connection refused")

    monkeypatch.setattr(uw, "CONDITIONS", {"BOOM": boom})
    state, status = tmp_path / "s.json", tmp_path / "STATUS.txt"
    rc = uw.main(["--state", str(state), "--status", str(status), "--no-page"])
    assert rc == 2, "a failed query must be a non-zero exit, not a silent pass"
    body = status.read_text(encoding="utf-8")
    assert uw.COULD_NOT_TELL in body
    assert uw.NEVER_OCCURRED not in body


def test_first_occurrence_pages_once_and_only_once(tmp_path, monkeypatch):
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda title, body: pages.append(title) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "STATUS.txt"
    uw.main(["--state", str(state), "--status", str(status)])
    assert len(pages) == 1, "the 0 -> 1 transition must page"
    uw.main(["--state", str(state), "--status", str(status)])
    assert len(pages) == 1, "a condition already fired must not page again every run"


def test_a_condition_that_never_fires_never_pages(tmp_path, monkeypatch):
    """CONTROL for the test above: without an occurrence there must be no page at all."""
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda title, body: pages.append(title) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (0, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "STATUS.txt"
    uw.main(["--state", str(state), "--status", str(status)])
    assert pages == []
    assert uw.NEVER_OCCURRED in status.read_text(encoding="utf-8")


def test_state_records_the_denominator_so_silence_can_be_read_later(tmp_path, monkeypatch):
    monkeypatch.setattr(uw, "CONDITIONS", {"PEX1_RESTING_FILL": lambda: (0, 0, "d")})
    state, status = tmp_path / "s.json", tmp_path / "STATUS.txt"
    uw.main(["--state", str(state), "--status", str(status), "--no-page"])
    saved = json.loads(state.read_text(encoding="utf-8"))["PEX1_RESTING_FILL"]
    assert saved["verdict"] == uw.NEVER_LOOKED
    assert saved["denominator"] == 0
    assert saved["blind_since"], "a blind condition must record WHEN it went blind"

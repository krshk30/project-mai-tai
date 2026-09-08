"""Controls for the unexercised-condition watcher.

⛔ The defect this file exists to prevent is a FALSE CLEAN: a watcher that reports a tidy zero when
its query failed, or when it never had a denominator at all. Every test below must go RED if that
distinction is collapsed.
"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta
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


def test_no_page_is_sent_before_the_state_is_durable(tmp_path, monkeypatch):
    """⛔ The wallpaper control, measured on the box 2026-09-08.

    With the send ahead of the state write, a write failure raised AFTER paging and the identical
    "first occurrence" page went out on every single run -- 3 pages in 3 crashed invocations. A
    once-only alarm that repeats every 15 minutes is wallpaper. This test fails if the order is
    ever put back.
    """
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda title, body: pages.append(title) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    unwritable = tmp_path / "state_is_a_directory"
    unwritable.mkdir()
    with pytest.raises(IsADirectoryError):
        uw.main(["--state", str(unwritable), "--status", str(tmp_path / "S.txt")])
    assert pages == [], "a page was sent even though its state could never be persisted"


def test_a_grep_error_raises_instead_of_reporting_zero(tmp_path, monkeypatch):
    """⛔ THE DEFECT THAT SHIPPED. Every marker is bracketed, e.g. "[V2-HALT-CONFIRMED]". As a basic
    regex that is a character class containing the invalid range T-C, so grep exits 2 with an EMPTY
    stdout -- which the first version parsed as 0. Three markers, three false zeros, reported as
    NEVER_OCCURRED against a denominator of 2,080.
    """
    log = tmp_path / "svc.log"
    log.write_text("[V2-HALT-CONFIRMED] one\nunrelated\n", encoding="utf-8")

    # The real thing: a bracketed marker must be counted literally, not as a regex.
    assert uw._log_count(log, "[V2-HALT-CONFIRMED]") == 1

    class _Err:
        returncode, stdout, stderr = 2, "", "grep: Invalid range end"

    monkeypatch.setattr(uw.subprocess, "run", lambda *a, **k: _Err())
    with pytest.raises(RuntimeError, match="rc=2"):
        uw._log_count(log, "[V2-HALT-CONFIRMED]")


def test_a_failed_delivery_is_retried_rather_than_suppressed(tmp_path, monkeypatch):
    """⛔ The first version recorded the transition regardless of the send, so a failed delivery was
    suppressed forever: the alarm believed it had spoken when nothing was sent."""
    attempts: list[str] = []
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"

    monkeypatch.setattr(uw, "page", lambda t, b: attempts.append(t) or False)   # delivery FAILS
    uw.main(["--state", str(state), "--status", str(status)])
    assert len(attempts) == 1
    assert json.loads(state.read_text(encoding="utf-8"))["HALT_REAL"]["delivered"] is False

    uw.main(["--state", str(state), "--status", str(status)])
    assert len(attempts) == 2, "an undelivered alarm must be retried, not silently dropped"

    monkeypatch.setattr(uw, "page", lambda t, b: attempts.append(t) or True)    # delivery SUCCEEDS
    uw.main(["--state", str(state), "--status", str(status)])
    assert len(attempts) == 3
    assert json.loads(state.read_text(encoding="utf-8"))["HALT_REAL"]["delivered"] is True

    uw.main(["--state", str(state), "--status", str(status)])
    assert len(attempts) == 3, "a delivered alarm must never page again"


def test_could_not_tell_does_not_re_arm_a_delivered_occurrence(tmp_path, monkeypatch):
    """⛔ The first version wrote fired=0 on COULD_NOT_TELL, resetting the transition memory, so a
    condition that had already paged would page AGAIN as soon as its query recovered."""
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    state, status = tmp_path / "s.json", tmp_path / "S.txt"

    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    uw.main(["--state", str(state), "--status", str(status)])
    assert [p for p in pages if p.startswith("FIRED")] == ["FIRED HALT_REAL -- first occurrence"]

    def broken():
        raise RuntimeError("psql failed")

    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": broken})
    uw.main(["--state", str(state), "--status", str(status)])
    saved = json.loads(state.read_text(encoding="utf-8"))["HALT_REAL"]
    assert saved["fired"] == 1, "COULD_NOT_TELL overwrote the remembered occurrence"
    assert saved["delivered"] is True

    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    uw.main(["--state", str(state), "--status", str(status)])
    assert len([p for p in pages if p.startswith("FIRED")]) == 1, "the recovered query re-paged"


def test_could_not_tell_pages_once_so_a_blind_watcher_is_not_silent(tmp_path, monkeypatch):
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)

    def broken():
        raise RuntimeError("psql failed: connection refused")

    monkeypatch.setattr(uw, "CONDITIONS", {"SIL1_REJECT_STORM": broken})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"
    uw.main(["--state", str(state), "--status", str(status)])
    uw.main(["--state", str(state), "--status", str(status)])
    assert len([p for p in pages if p.startswith("CANNOT TELL")]) == 1


def test_a_count_that_dips_and_returns_does_not_re_announce(tmp_path, monkeypatch):
    """⛔ MEASURED DEFECT. Announcement memory was computed as `prior_fired > 0 and delivered`, so a
    1 -> 0 -> 1 count sequence lost it at the dip and paged the SAME first occurrence twice. Log
    rotation and row pruning both make a count fall."""
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    state, status = tmp_path / "s.json", tmp_path / "S.txt"

    for count in (1, 0, 1, 0, 1):
        monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": (lambda c=count: (c, 500, "d"))})
        uw.main(["--state", str(state), "--status", str(status)])

    first = [p for p in pages if p.startswith("FIRED")]
    assert len(first) == 1, f"a dipping count re-announced: {first}"


def test_a_corrupt_state_file_suppresses_rather_than_replays(tmp_path, monkeypatch):
    """⛔ A MISSING file is a first run; an UNPARSEABLE one is LOST MEMORY. Collapsing them made a
    truncated state replay every historical alert as a fresh first occurrence."""
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"
    state.write_text('{"HALT_REAL": {"fired": 1, "deliv', encoding="utf-8")

    uw.main(["--state", str(state), "--status", str(status)])

    assert [p for p in pages if p.startswith("FIRED")] == [], "a corrupt state replayed history"
    assert any(p.startswith("STATE LOST") for p in pages), "losing memory must be reported"

    pages.clear()
    uw.main(["--state", str(state), "--status", str(status)])
    assert pages == [], "the rebuilt state must not announce afterwards either"


def test_the_state_write_is_atomic(tmp_path, monkeypatch):
    """A crash mid-write must leave the PREVIOUS state intact, not half a JSON document."""
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"
    uw.main(["--state", str(state), "--status", str(status), "--no-page"])
    good = state.read_text(encoding="utf-8")

    real_replace = uw.os.replace

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(uw.os, "replace", boom)
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (9, 900, "d")})
    with pytest.raises(OSError):
        uw.main(["--state", str(state), "--status", str(status), "--no-page"])

    monkeypatch.setattr(uw.os, "replace", real_replace)
    assert state.read_text(encoding="utf-8") == good, "a failed write corrupted the previous state"
    assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"


def test_pre_upgrade_state_without_announced_is_not_re_announced(tmp_path, monkeypatch):
    """⛔ MEASURED IN PRODUCTION. State written before `announced` existed carries only `delivered`.
    Defaulting the missing flag to False re-sent PEX1 as a first occurrence on the first cron run
    after the upgrade, and that duplicate reached the operator's phone at 12:45 UTC 2026-09-08.
    """
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"PEX1_RESTING_FILL": lambda: (4, 41, "d")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"

    # EXACTLY the legacy shape: delivered, no `announced` key at all.
    state.write_text(json.dumps({"PEX1_RESTING_FILL": {
        "fired": 4, "denominator": 41, "verdict": "OCCURRED",
        "delivered": True, "blind_since": None, "blind_paged": False,
    }}), encoding="utf-8")

    uw.main(["--state", str(state), "--status", str(status)])

    assert pages == [], f"a pre-upgrade state re-announced: {pages}"
    assert json.loads(state.read_text(encoding="utf-8"))["PEX1_RESTING_FILL"]["announced"] is True


def test_state_lost_page_is_retried_until_delivered(tmp_path, monkeypatch):
    """⛔ The STATE LOST alarm queued once and never persisted its outcome, so a refused send was
    lost: the next run read valid reconstructed JSON and never spoke again."""
    attempts: list[str] = []
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (0, 500, "d")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"
    state.write_text("{ truncated", encoding="utf-8")

    monkeypatch.setattr(uw, "page", lambda t, b: attempts.append(t) or False)   # refused
    uw.main(["--state", str(state), "--status", str(status)])
    assert [a for a in attempts if a.startswith("STATE LOST")]

    attempts.clear()
    uw.main(["--state", str(state), "--status", str(status)])   # state is now VALID json
    assert [a for a in attempts if a.startswith("STATE LOST")], "an undelivered STATE LOST was dropped"

    attempts.clear()
    monkeypatch.setattr(uw, "page", lambda t, b: attempts.append(t) or True)    # accepted
    uw.main(["--state", str(state), "--status", str(status)])
    assert [a for a in attempts if a.startswith("STATE LOST")]

    attempts.clear()
    uw.main(["--state", str(state), "--status", str(status)])
    assert attempts == [], "STATE LOST kept paging after delivery"


def test_corrupt_state_plus_a_failed_query_still_suppresses(tmp_path, monkeypatch):
    """⛔ The COULD_NOT_TELL path returned early, so corrupt state plus a failed query rebuilt an
    empty record and emitted a historical FIRED page the moment the query recovered."""
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    state, status = tmp_path / "s.json", tmp_path / "S.txt"
    state.write_text('{"HALT_REAL": {"fired": 1, "deliv', encoding="utf-8")

    def broken():
        raise RuntimeError("psql failed")

    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": broken})
    uw.main(["--state", str(state), "--status", str(status)])

    pages.clear()
    monkeypatch.setattr(uw, "CONDITIONS", {"HALT_REAL": lambda: (1, 500, "d")})   # query recovers
    uw.main(["--state", str(state), "--status", str(status)])

    assert [p for p in pages if p.startswith("FIRED")] == [], "a historical occurrence was replayed"


def test_each_blind_episode_pages_once_and_recovery_re_arms_it(tmp_path, monkeypatch):
    """⛔ MEASURED DEFECT. A real denominator cleared `blind_since` but left `blind_paged` True, so
    a SECOND blind episode never paged. The two are one episode marker and must end together.

    ⭐ The mirror of the `announced` defect: that flag was too volatile, this one too sticky. Both
    are a flag whose lifetime does not match the episode it describes.
    """
    pages: list[str] = []
    monkeypatch.setattr(uw, "page", lambda t, b: pages.append(t) or True)
    monkeypatch.setattr(uw, "CONDITIONS", {"X": lambda: (0, 0, "no denominator")})
    state, status = tmp_path / "s.json", tmp_path / "S.txt"

    def age_the_episode(days: int) -> None:
        saved = json.loads(state.read_text(encoding="utf-8"))
        saved["X"]["blind_since"] = (
            datetime.fromisoformat(saved["X"]["blind_since"]) - timedelta(days=days)
        ).isoformat()
        state.write_text(json.dumps(saved), encoding="utf-8")

    # Episode 1
    uw.main(["--state", str(state), "--status", str(status)])
    age_the_episode(uw.BLIND_DAYS_BEFORE_PAGE + 1)
    uw.main(["--state", str(state), "--status", str(status)])
    uw.main(["--state", str(state), "--status", str(status)])   # still blind, must stay silent
    assert len([p for p in pages if p.startswith("BLIND")]) == 1, "one episode paged more than once"

    # A real denominator ends the episode.
    monkeypatch.setattr(uw, "CONDITIONS", {"X": lambda: (0, 500, "recovered")})
    uw.main(["--state", str(state), "--status", str(status)])
    saved = json.loads(state.read_text(encoding="utf-8"))["X"]
    assert saved["blind_since"] is None
    assert saved["blind_paged"] is False, "recovery did not re-arm the blind alarm"

    # Episode 2 must page on its own merits.
    pages.clear()
    monkeypatch.setattr(uw, "CONDITIONS", {"X": lambda: (0, 0, "blind again")})
    uw.main(["--state", str(state), "--status", str(status)])
    age_the_episode(uw.BLIND_DAYS_BEFORE_PAGE + 1)
    uw.main(["--state", str(state), "--status", str(status)])
    assert len([p for p in pages if p.startswith("BLIND")]) == 1, "a second blind episode was silent"

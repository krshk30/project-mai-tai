"""⛔⭐⭐ WRAPPER-LEVEL controls for the GATE1 watch — delivery transitions, cooldown, and wording.

codex-2 named the gap on #931: the committed tests all exercised `qualifying_rows`, so NOTHING
executed the wrapper. Delivery transitions, cooldown behaviour, failed delivery and selftest wording
could all change without a test failing — and all three defects he found lived exactly there.

⛔ This is the THIRD time in one day I shipped count-only or unit-only controls on a pager and had
the wrapper defects found by review. These drive the REAL script through a stubbed HTTP layer.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "ops" / "health" / "gate1_shape_watch_cron.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class _Harness:
    def __init__(self, tmp: Path) -> None:
        self.out = tmp / "out"
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True)
        self.payloads = tmp / "payloads"
        self.curl_rc = tmp / "curl-rc"
        self.eval_rc = tmp / "eval-rc"
        self.curl_rc.write_text("0", encoding="utf-8")
        self.eval_rc.write_text("0", encoding="utf-8")
        # curl stub records the FULL argv — a delivery COUNT cannot see what a message says.
        _exe(self.bin / "curl",
             '#!/usr/bin/env bash\nprintf "%s\\n---END---\\n" "$*" >> "$FAKE_PAYLOADS"\n'
             'exit "$(cat "$FAKE_CURL_RC")"\n')
        # stand-in evaluator: prints a report and exits with the chosen verdict code
        self.python = tmp / "fake_eval"
        _exe(self.python,
             '#!/usr/bin/env bash\nprintf "stubbed gate1 report\\n"\nexit "$(cat "$FAKE_EVAL_RC")"\n')

    def run(self, *, eval_rc: int, curl_rc: int = 0, selftest: bool = False):
        self.eval_rc.write_text(str(eval_rc), encoding="utf-8")
        self.curl_rc.write_text(str(curl_rc), encoding="utf-8")
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
            "GATE1_REPO": str(ROOT), "GATE1_OUT": str(self.out),
            "GATE1_PYTHON": str(self.python),
            "GATE1_NTFY_URL": "http://127.0.0.1:1/stub",
            "FAKE_PAYLOADS": str(self.payloads), "FAKE_CURL_RC": str(self.curl_rc),
            "FAKE_EVAL_RC": str(self.eval_rc),
        })
        argv = [str(BASH), str(WRAPPER)] + (["--selftest"] if selftest else [])
        return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    @property
    def messages(self) -> list[str]:
        if not self.payloads.exists():
            return []
        raw = self.payloads.read_text(encoding="utf-8")
        return [m.strip() for m in raw.split("---END---") if m.strip()]


# ---------------------------------------------------------------------------
# P1 — a transition between distinct actionable levels must page immediately.
# ---------------------------------------------------------------------------

def test_CANNOT_SEE_then_SHAPE_pages_TWICE(tmp_path: Path) -> None:
    """⛔ THE CONTROL codex-2 ASKED FOR. A delivered CANNOT_SEE must not swallow the real shape:
    the qualifying position can close inside the 30-minute cooldown."""
    h = _Harness(tmp_path)
    h.run(eval_rc=2)                     # CANNOT_SEE, delivered
    h.run(eval_rc=1)                     # SHAPE, seconds later
    assert len(h.messages) == 2, "a distinct actionable level must page immediately"
    assert "Title: Gate1 shape is LIVE" in h.messages[1]


def test_the_SAME_level_repeating_is_still_suppressed(tmp_path: Path) -> None:
    """PINS THE OTHER DIRECTION: the cooldown must still suppress a REPEAT, or a live shape pages
    every minute for as long as the position is open."""
    h = _Harness(tmp_path)
    h.run(eval_rc=1)
    h.run(eval_rc=1)
    assert len(h.messages) == 1


# ---------------------------------------------------------------------------
# P2 — a CLEAN selftest must not claim a live shape or instruct the preview.
# ---------------------------------------------------------------------------

def test_a_clean_selftest_does_NOT_claim_a_live_shape(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=0, selftest=True)
    (msg,) = h.messages
    assert "Title: SELFTEST Gate1 watch delivery" in msg
    assert "NO QUALIFYING SHAPE WAS OBSERVED" in msg
    assert "exists RIGHT NOW" not in msg
    assert "take the preview now" not in msg.split("Title:")[1].split("-H")[0]
    assert "Title: Gate1 shape is LIVE" not in msg


def test_a_selftest_while_a_shape_IS_live_still_says_the_shape_is_live(tmp_path: Path) -> None:
    """⛔ The discriminator is the MEASURED LEVEL, not the flag — suppressing a real shape because
    the run happened to be a selftest would be the same contradiction pointing the other way."""
    h = _Harness(tmp_path)
    h.run(eval_rc=1, selftest=True)
    (msg,) = h.messages
    assert "Title: Gate1 shape is LIVE" in msg
    assert "[SELFTEST]" in msg


# ---------------------------------------------------------------------------
# Delivery is verified, not assumed.
# ---------------------------------------------------------------------------

def test_an_undelivered_page_retries_instead_of_entering_the_cooldown(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=1, curl_rc=7)          # refused
    h.run(eval_rc=1, curl_rc=0)          # must try again, not sit silent
    assert len(h.messages) == 2
    assert "DELIVERY FAILED" in (h.out / "alert.log").read_text(encoding="utf-8")


def test_a_clean_run_pages_nobody(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=0)
    assert h.messages == []


def test_CANNOT_SEE_says_it_could_not_see_and_does_not_claim_absence(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=2)
    (msg,) = h.messages
    assert "Title: AMBER Gate1 watch CANNOT SEE" in msg
    assert "UNKNOWN is not PASS" in msg
    assert "Title: Gate1 shape is LIVE" not in msg

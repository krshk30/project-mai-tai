"""⛔⭐⭐ WRAPPER-LEVEL controls for the GATE1 watch — delivery transitions, cooldown, and wording.

codex-2 named the gap on #931: the committed tests all exercised `qualifying_rows`, so NOTHING
executed the wrapper. Delivery transitions, cooldown behaviour, failed delivery and selftest wording
could all change without a test failing — and all three defects he found lived exactly there.

⛔ This is the THIRD time in one day I shipped count-only or unit-only controls on a pager and had
the wrapper defects found by review. These drive the REAL script through a stubbed HTTP layer.

⛔⭐⭐ AND THE FIRST VERSION OF THIS FILE WAS A TIME BOMB (codex-2 P1, second round).
Every non-selftest test drove the wrapper with the REAL clock, so outside 09:30-16:00 ET the script
exited at its own RTH guard before reaching a single assertion. Run after the close the suite was
`4 failed / 10 passed`; CI passed only because it happened to run during regular hours. The four
tests that stopped running were exactly the ones covering the transition logic. A control whose
result depends on WHEN it runs is not a control — so `date` is now stubbed and the ET wall clock is
an INPUT. The window guard itself is asserted here instead of silently disabling the suite.
"""
from __future__ import annotations

import http.server
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
WRAPPER = ROOT / "ops" / "health" / "gate1_shape_watch_cron.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")

# A Wednesday inside the RTH window, and an epoch to match. Both are INPUTS, never `now`.
# ⛔ The epoch must AGREE with the wall clock beside it. 1789045200 was 2026-09-10 09:00 ET,
# dow 4 — contradicting every other field (codex-2, #931). No test read it as an absolute
# instant, only as deltas, so nothing went red: a self-inconsistent fixture that lies
# quietly is exactly the kind that misleads the next reader.
IN_WINDOW = {"day": "2026-09-09", "hms": "11:00:00", "dow": "3", "epoch": 1788966000}
COOLDOWN_SECS = 1800


def _exe(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class _Harness:
    def __init__(self, tmp: Path, *, ntfy_url: str = "http://127.0.0.1:1/stub") -> None:
        self.out = tmp / "out"
        self.bin = tmp / "bin"
        self.bin.mkdir(parents=True)
        self.payloads = tmp / "payloads"
        self.curl_rc = tmp / "curl-rc"
        self.eval_rc = tmp / "eval-rc"
        self.ntfy_url = ntfy_url
        self.curl_rc.write_text("0", encoding="utf-8")
        self.eval_rc.write_text("0", encoding="utf-8")
        self.clock = dict(IN_WINDOW)
        # curl stub records the FULL argv — a delivery COUNT cannot see what a message says.
        _exe(self.bin / "curl",
             '#!/usr/bin/env bash\nprintf "%s\\n---END---\\n" "$*" >> "$FAKE_PAYLOADS"\n'
             'exit "$(cat "$FAKE_CURL_RC")"\n')
        # ⛔ FIXED CLOCK. Every format the wrapper actually asks for is answered from the
        # environment; anything else EXITS LOUDLY rather than returning an empty string, so a new
        # `date` call in the wrapper cannot silently start reading a blank value in the tests.
        _exe(self.bin / "date",
             '#!/usr/bin/env bash\n'
             'case "$1" in\n'
             '  "+%F %H:%M:%S %Z") printf "%s %s EDT\\n" "$FAKE_ET_DAY" "$FAKE_ET_HMS" ;;\n'
             '  "+%H") printf "%s\\n" "${FAKE_ET_HMS%%:*}" ;;\n'
             '  "+%M") t="${FAKE_ET_HMS#*:}"; printf "%s\\n" "${t%%:*}" ;;\n'
             '  "+%u") printf "%s\\n" "$FAKE_ET_DOW" ;;\n'
             '  "+%F") printf "%s\\n" "$FAKE_ET_DAY" ;;\n'
             '  "+%s") printf "%s\\n" "$FAKE_EPOCH" ;;\n'
             '  *) printf "UNEXPECTED date format: %s\\n" "$1" >&2; exit 64 ;;\n'
             'esac\n')
        # stand-in evaluator: prints a report and exits with the chosen verdict code
        self.python = tmp / "fake_eval"
        _exe(self.python,
             '#!/usr/bin/env bash\nprintf "stubbed gate1 report\\n"\nexit "$(cat "$FAKE_EVAL_RC")"\n')

    def at(self, *, day: str | None = None, hms: str | None = None, dow: str | None = None):
        """Move the fixed ET wall clock. Used to assert the window guard, not to skip it."""
        if day is not None:
            self.clock["day"] = day
        if hms is not None:
            self.clock["hms"] = hms
        if dow is not None:
            self.clock["dow"] = dow
        return self

    def advance(self, seconds: int):
        self.clock["epoch"] = int(self.clock["epoch"]) + int(seconds)
        return self

    def run(self, *, eval_rc: int, curl_rc: int = 0, selftest: bool = False, real_curl: bool = False):
        self.eval_rc.write_text(str(eval_rc), encoding="utf-8")
        self.curl_rc.write_text(str(curl_rc), encoding="utf-8")
        if real_curl and (self.bin / "curl").exists():
            (self.bin / "curl").unlink()
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
            "GATE1_REPO": str(ROOT), "GATE1_OUT": str(self.out),
            "GATE1_PYTHON": str(self.python),
            "GATE1_NTFY_URL": self.ntfy_url,
            "FAKE_PAYLOADS": str(self.payloads), "FAKE_CURL_RC": str(self.curl_rc),
            "FAKE_EVAL_RC": str(self.eval_rc),
            "FAKE_ET_DAY": self.clock["day"], "FAKE_ET_HMS": self.clock["hms"],
            "FAKE_ET_DOW": self.clock["dow"], "FAKE_EPOCH": str(self.clock["epoch"]),
        })
        argv = [str(BASH), str(WRAPPER)] + (["--selftest"] if selftest else [])
        return subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)

    @property
    def messages(self) -> list[str]:
        if not self.payloads.exists():
            return []
        raw = self.payloads.read_text(encoding="utf-8")
        return [m.strip() for m in raw.split("---END---") if m.strip()]

    @property
    def state(self) -> tuple[str, int] | None:
        f = self.out / "state"
        if not f.exists():
            return None
        status, _, last = f.read_text(encoding="utf-8").strip().partition(" ")
        return status, int(last or 0)

    @property
    def alert_log(self) -> str:
        f = self.out / "alert.log"
        return f.read_text(encoding="utf-8") if f.exists() else ""


# ---------------------------------------------------------------------------
# P1 — a transition between distinct actionable levels must page immediately.
# ---------------------------------------------------------------------------

def test_CANNOT_SEE_then_SHAPE_pages_TWICE(tmp_path: Path) -> None:
    """⛔ THE CONTROL codex-2 ASKED FOR. A delivered CANNOT_SEE must not swallow the real shape:
    the qualifying position can close inside the 30-minute cooldown."""
    h = _Harness(tmp_path)
    h.run(eval_rc=2)                     # CANNOT_SEE, delivered
    h.advance(60).run(eval_rc=1)         # SHAPE, a minute later
    assert len(h.messages) == 2, "a distinct actionable level must page immediately"
    assert "Title: Gate1 shape is LIVE" in h.messages[1]


def test_the_SAME_level_repeating_is_still_suppressed(tmp_path: Path) -> None:
    """PINS THE OTHER DIRECTION: the cooldown must still suppress a REPEAT, or a live shape pages
    every minute for as long as the position is open."""
    h = _Harness(tmp_path)
    h.run(eval_rc=1)
    h.advance(60).run(eval_rc=1)
    assert len(h.messages) == 1


def test_the_cooldown_EXPIRING_pages_the_same_level_again(tmp_path: Path) -> None:
    """The suppression above must be a cooldown, not a latch. Without this, `SHOULD_PAGE` could be
    hard-wired to 'never repeat' and every test above would still pass."""
    h = _Harness(tmp_path)
    h.run(eval_rc=1)
    h.advance(COOLDOWN_SECS + 1).run(eval_rc=1)
    assert len(h.messages) == 2


# ---------------------------------------------------------------------------
# P1 (second round) — a FAILED transition alert must still be retried.
# ---------------------------------------------------------------------------

def test_a_FAILED_transition_alert_is_retried_on_the_next_run(tmp_path: Path) -> None:
    """⛔⭐⭐ codex-2's EXACT REPRO. Holding LAST_ALERT back is only half the retry: the wrapper also
    persisted the level it had just FAILED to send, which consumed the transition. The next run then
    saw LEVEL == PREV_STATUS and fell through to a cooldown that the earlier CANNOT_SEE delivery had
    already refreshed — 2 attempts where 3 were required.

    ⛔ Every run here is well inside the 30-minute cooldown, so the ONLY thing that can produce the
    third attempt is the preserved transition."""
    h = _Harness(tmp_path)
    h.run(eval_rc=2, curl_rc=0)                      # CANNOT_SEE delivered -> cooldown refreshed
    h.advance(60).run(eval_rc=1, curl_rc=7)          # SHAPE, delivery REFUSED
    h.advance(60).run(eval_rc=1, curl_rc=0)          # SHAPE again, must try a THIRD time
    assert len(h.messages) == 3, (
        "a transition that failed to deliver must remain pending, not be consumed by the write"
    )
    assert "Title: Gate1 shape is LIVE" in h.messages[2]


def test_a_failed_delivery_does_not_advance_the_persisted_level(tmp_path: Path) -> None:
    """The state file is the mechanism; assert it directly so the fix cannot be re-broken by a
    change that happens to keep the message count right for one sequence."""
    h = _Harness(tmp_path)
    h.run(eval_rc=2, curl_rc=0)
    assert h.state is not None and h.state[0] == "CANNOT_SEE"
    first_alert = h.state[1]
    h.advance(60).run(eval_rc=1, curl_rc=7)
    assert h.state is not None and h.state[0] == "CANNOT_SEE", (
        "an undelivered SHAPE must not be recorded as the new level"
    )
    assert h.state[1] == first_alert, "an undelivered page must not advance the cooldown either"


def test_a_failed_REPEAT_does_not_burn_the_cooldown_it_never_used(tmp_path: Path) -> None:
    """⛔ ISOLATES THE OTHER HALF OF THE FIX. The failed-transition test above still passes if the
    cooldown is wrongly advanced, because the preserved TRANSITION carries the retry on its own.
    Only a SAME-LEVEL repeat depends on the cooldown half:

        run A  SHAPE delivered              -> cooldown starts
        run B  SHAPE, cooldown expired, FAILED
        run C  SHAPE, one second later      -> must retry

    If run B advances LAST_ALERT it buys another 30 minutes of silence for a page that never
    landed. This is why both halves are required, and why each now has its own control."""
    h = _Harness(tmp_path)
    h.run(eval_rc=1, curl_rc=0)                              # delivered
    h.advance(COOLDOWN_SECS + 1).run(eval_rc=1, curl_rc=7)   # repeat, refused
    h.advance(1).run(eval_rc=1, curl_rc=0)                   # must retry immediately
    assert len(h.messages) == 3, "an undelivered repeat must not start a fresh cooldown"


def test_an_undelivered_page_retries_instead_of_entering_the_cooldown(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=1, curl_rc=7)          # refused
    h.advance(60).run(eval_rc=1, curl_rc=0)   # must try again, not sit silent
    assert len(h.messages) == 2
    assert "DELIVERY FAILED" in h.alert_log


# ---------------------------------------------------------------------------
# The ET window guard — now ASSERTED, instead of silently disabling the suite.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "label,kw",
    [
        ("before the open", {"hms": "09:29:00"}),
        ("after the close", {"hms": "16:00:00"}),
        ("saturday", {"dow": "6"}),
        ("thanksgiving", {"day": "2026-11-26", "dow": "4"}),
    ],
)
def test_outside_the_live_window_the_watch_does_nothing(tmp_path: Path, label: str, kw: dict) -> None:
    """⛔ The shape can only exist while the market is open, so the guard is correct — but it must
    be PROVEN, not assumed. These four cases were the reason the old suite quietly stopped testing
    anything outside RTH."""
    h = _Harness(tmp_path).at(**kw)
    h.run(eval_rc=1)
    assert h.messages == [], f"{label}: the watch must not page outside its window"
    assert h.state is None, f"{label}: no state may be written outside the window"


def test_INSIDE_the_window_the_same_input_DOES_page(tmp_path: Path) -> None:
    """⛔ The discriminating control for the four above: identical evaluator verdict, only the clock
    differs. Without this a guard that refused everything would look correct."""
    h = _Harness(tmp_path).at(hms="09:30:00")
    h.run(eval_rc=1)
    assert len(h.messages) == 1


def test_a_selftest_runs_outside_the_window(tmp_path: Path) -> None:
    """The selftest is the operator's delivery proof and must not depend on market hours."""
    h = _Harness(tmp_path).at(hms="22:00:00")
    h.run(eval_rc=0, selftest=True)
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


def test_a_selftest_does_not_write_state(tmp_path: Path) -> None:
    h = _Harness(tmp_path)
    h.run(eval_rc=1, selftest=True)
    assert h.state is None


# ---------------------------------------------------------------------------
# Delivery is verified, not assumed.
# ---------------------------------------------------------------------------

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


def test_a_real_HTTP_500_is_a_delivery_FAILURE(tmp_path: Path) -> None:
    """⛔ END-TO-END WITH THE REAL CURL (codex-2 P1, #931). Every test above uses a stub whose exit
    code the harness chooses, so they pin the state machine while saying NOTHING about the semantics
    it rests on: with `curl -s`, an HTTP 500 exits 0 and a DROPPED page is recorded as delivered and
    then suppressed for the whole cooldown. That was the #924 defect, rebuilt here. This test fails
    if `--fail-with-body` is ever dropped."""

    class _Always500(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(500)
            self.send_header("Content-Length", "5")
            self.end_headers()
            self.wfile.write(b"oops!")

        def log_message(self, *args):  # silence the test log
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Always500)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        h = _Harness(tmp_path, ntfy_url=f"http://127.0.0.1:{server.server_port}/gate1")
        h.run(eval_rc=1, real_curl=True)
        assert h.state == ("NONE", 0), "HTTP 500 must not be recorded as a delivered page"
        assert "DELIVERY FAILED" in h.alert_log
        assert "DELIVERED" not in h.alert_log.replace("DELIVERY FAILED", "")
    finally:
        server.shutdown()


def test_a_real_HTTP_200_is_a_delivery_SUCCESS(tmp_path: Path) -> None:
    """⛔ The discriminating half. Without it, a wrapper that treated EVERY real response as a
    failure would pass the 500 test above and page forever."""

    class _Always200(http.server.BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), _Always200)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        h = _Harness(tmp_path, ntfy_url=f"http://127.0.0.1:{server.server_port}/gate1")
        h.run(eval_rc=1, real_curl=True)
        assert h.state is not None and h.state[0] == "SHAPE"
        assert "DELIVERED" in h.alert_log
        assert "DELIVERY FAILED" not in h.alert_log
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# The fixture must not be able to drift away from production.
# ---------------------------------------------------------------------------

def _code_lines() -> str:
    """⛔ Check CODE, not prose. The wrapper's comments NAME these flags to explain them, so a
    whole-file substring check passes even after the flag is deleted from the curl call — that
    exact mistake shipped on #924 and a mutant restoring plain `curl -s` did not turn it red."""
    source = WRAPPER.read_text(encoding="utf-8")
    return "\n".join(ln for ln in source.splitlines() if not ln.lstrip().startswith("#"))


def test_the_delivery_flags_are_present_and_no_failure_is_swallowed() -> None:
    code = _code_lines()
    assert "--fail-with-body" in code, "non-2xx must be an error; plain -s returns 0 on HTTP 500"
    assert "--connect-timeout" in code and "--max-time" in code, (
        "a hang is a delivery failure, not an indefinite block in cron"
    )
    curl_line = next(ln for ln in code.splitlines() if "curl " in ln)
    assert "|| true" not in curl_line, "the delivery path must never swallow its own failure"


def test_the_production_defaults_are_unchanged_by_the_test_overrides() -> None:
    """⛔ FIXTURE MUST MATCH PRODUCTION. The GATE1_* overrides exist for the harness above; if one
    ever changed what cron actually runs, these tests would be proving the wrong thing."""
    source = WRAPPER.read_text(encoding="utf-8")
    assert '${GATE1_REPO:-/home/trader/project-mai-tai}' in source
    assert '${GATE1_OUT:-/home/trader/gate1_watch}' in source
    assert '${GATE1_PYTHON:-$REPO/.venv/bin/python}' in source
    assert '${GATE1_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}' in source
    assert "COOLDOWN_SECS=1800" in source

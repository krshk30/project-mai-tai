"""⛔⭐⭐ DRIFT1 wrapper — DELIVERY IS VERIFIED, NEVER ASSUMED.

Raised by codex-2 as a P1 on #924 (2026-09-09). The first version of the wrapper called
`curl -s`, then logged "sent", advanced `LAST_ALERT` and persisted the RED state regardless of
the result. Measured on this machine against a local server:

    curl -s                  on HTTP 500  -> exit 0     <- the defect
    curl --fail-with-body    on HTTP 500  -> exit 22
    curl -s                  refused      -> exit 7

So an ntfy outage would have been recorded as a delivered page and then suppressed for the full
six-hour cooldown. The watchdog written to stop a false clean would itself have failed to one, one
layer down at the delivery boundary.

⛔ These drive the REAL wrapper through a stubbed HTTP layer. A fixture that cannot reach the real
code path proves nothing about it — `DRIFT1_*` overrides exist only to point the wrapper at a temp
state dir and a stub; the production defaults are unchanged and are asserted below.
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
WRAPPER = ROOT / "ops" / "health" / "live_locked_drift_cron.sh"
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(BASH is None, reason="bash is required")


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    path.chmod(0o755)


class _Harness:
    """Runs the real wrapper with a stubbed `curl` and a stubbed audit."""

    def __init__(self, tmp_path: Path, ntfy_url: str = "http://127.0.0.1:1/stub") -> None:
        self.out = tmp_path / "out"
        self.bin = tmp_path / "bin"
        self.bin.mkdir(parents=True)
        self.attempts = tmp_path / "curl-attempts"
        self.curl_rc = tmp_path / "curl-rc"
        self.audit_rc = tmp_path / "audit-rc"
        self.ntfy_url = ntfy_url
        self.curl_rc.write_text("0", encoding="utf-8")
        self.audit_rc.write_text("1", encoding="utf-8")
        # ⛔ The stub records EVERY invocation, so "silence" is provable as an absent attempt
        # rather than merely an absent log line.
        _write_executable(
            self.bin / "curl",
            '#!/usr/bin/env bash\nprintf "call\\n" >> "$FAKE_CURL_ATTEMPTS"\n'
            'exit "$(cat "$FAKE_CURL_RC")"\n',
        )
        _write_executable(
            tmp_path / "fake_audit_python",
            '#!/usr/bin/env bash\nprintf "stubbed audit report\\n"\n'
            'exit "$(cat "$FAKE_AUDIT_RC")"\n',
        )
        self.python = tmp_path / "fake_audit_python"

    def run(self, *, audit_rc: int | None = None, curl_rc: int | None = None) -> subprocess.CompletedProcess:
        if audit_rc is not None:
            self.audit_rc.write_text(str(audit_rc), encoding="utf-8")
        if curl_rc is not None:
            self.curl_rc.write_text(str(curl_rc), encoding="utf-8")
        env = os.environ.copy()
        env.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{env['PATH']}",
                "DRIFT1_REPO": str(ROOT),
                "DRIFT1_OUT": str(self.out),
                "DRIFT1_PYTHON": str(self.python),
                "DRIFT1_NTFY_URL": self.ntfy_url,
                "FAKE_CURL_ATTEMPTS": str(self.attempts),
                "FAKE_CURL_RC": str(self.curl_rc),
                "FAKE_AUDIT_RC": str(self.audit_rc),
            }
        )
        return subprocess.run(
            [str(BASH), str(WRAPPER)],
            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60, check=False,
        )

    @property
    def attempt_count(self) -> int:
        if not self.attempts.exists():
            return 0
        return len([ln for ln in self.attempts.read_text(encoding="utf-8").splitlines() if ln])

    @property
    def state(self) -> tuple[str, int]:
        status, last_alert = (self.out / "state").read_text(encoding="utf-8").split()
        return status, int(last_alert)

    @property
    def alert_log(self) -> str:
        path = self.out / "alert.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# ---------------------------------------------------------------------------
# ⛔⭐⭐ THE SEQUENCE codex-2 ASKED FOR: refusal -> refusal -> acceptance -> silence.
# ---------------------------------------------------------------------------


def test_refusal_refusal_acceptance_then_silence(tmp_path: Path) -> None:
    h = _Harness(tmp_path)

    # 1 — RED, delivery REFUSED (curl exit 7). Must not claim sent, must not start the cooldown.
    h.run(audit_rc=1, curl_rc=7)
    assert h.attempt_count == 1
    assert h.state == ("RED", 0), "a failed delivery must NOT advance LAST_ALERT"
    assert "DELIVERY FAILED (curl exit 7)" in h.alert_log
    assert "DELIVERED" not in h.alert_log

    # 2 — still RED, refused again. THE POINT: it retries instead of sitting in a 6h cooldown.
    h.run(audit_rc=1, curl_rc=7)
    assert h.attempt_count == 2, "an undelivered page must be retried on the next run"
    assert h.state == ("RED", 0)

    # 3 — accepted. Only now does the cooldown begin.
    h.run(audit_rc=1, curl_rc=0)
    assert h.attempt_count == 3
    status, last_alert = h.state
    assert status == "RED"
    assert last_alert > 0, "confirmed delivery must start the cooldown"
    assert "ALERT[RED] DELIVERED" in h.alert_log

    # 4 — still RED, inside the cooldown. Silence, proven as an ABSENT ATTEMPT.
    h.run(audit_rc=1, curl_rc=0)
    assert h.attempt_count == 3, "a delivered page must suppress re-paging for the cooldown"
    assert h.state[1] == last_alert


def test_a_real_HTTP_500_is_a_delivery_FAILURE(tmp_path: Path) -> None:
    """⛔ END-TO-END WITH THE REAL CURL. The stub above pins the state machine; this pins the
    semantics the state machine depends on. `curl -s` returns 0 here — that WAS the defect — so
    this test fails if `--fail-with-body` is ever dropped from the wrapper."""

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
        h = _Harness(tmp_path, ntfy_url=f"http://127.0.0.1:{server.server_port}/drift1")
        # ⛔ Use the REAL curl: remove the stub so PATH resolves the system binary.
        (h.bin / "curl").unlink()
        h.run(audit_rc=1)
        assert h.state == ("RED", 0), "HTTP 500 must not be recorded as a delivered page"
        assert "DELIVERY FAILED" in h.alert_log
        assert "DELIVERED" not in h.alert_log
    finally:
        server.shutdown()


def test_an_undelivered_RECOVERY_is_not_recorded_as_sent(tmp_path: Path) -> None:
    """The all-clear has the same contract. If it does not land, the previous status is HELD so the
    next run retries it — clearing to OK here would lose the recovery silently."""
    h = _Harness(tmp_path)
    h.run(audit_rc=1, curl_rc=0)          # RED, delivered
    assert h.state[0] == "RED"

    h.run(audit_rc=0, curl_rc=7)          # recovered, but the all-clear does NOT land
    assert h.state[0] == "RED", "an undelivered recovery must not clear the state to OK"
    assert "RECOVERY DELIVERY FAILED (curl exit 7)" in h.alert_log

    before = h.attempt_count
    h.run(audit_rc=0, curl_rc=0)          # retried and accepted
    assert h.attempt_count == before + 1
    assert h.state[0] == "OK"
    assert "ALERT[GREEN] recovery DELIVERED" in h.alert_log


def test_a_green_run_pages_nobody(tmp_path: Path) -> None:
    """PINS THE OTHER DIRECTION. A watchdog that pages on green is muted within a week."""
    h = _Harness(tmp_path)
    h.run(audit_rc=0, curl_rc=0)
    assert h.attempt_count == 0
    assert h.state == ("OK", 0)


def test_CANNOT_SEE_pages_rather_than_passing(tmp_path: Path) -> None:
    """⛔ UNKNOWN IS NOT PASS. Exit 2 means the audit refused to measure; it must alert."""
    h = _Harness(tmp_path)
    h.run(audit_rc=2, curl_rc=0)
    assert h.attempt_count == 1
    assert h.state[0] == "CANNOT_SEE"


# ---------------------------------------------------------------------------
# The fixture must not be able to drift away from production.
# ---------------------------------------------------------------------------


def test_the_production_defaults_are_unchanged_by_the_test_overrides() -> None:
    """⛔ FIXTURE MUST MATCH PRODUCTION. The DRIFT1_* overrides exist for the harness above; if one
    ever changed what cron actually runs, these tests would be proving the wrong thing."""
    source = WRAPPER.read_text(encoding="utf-8")
    assert '${DRIFT1_REPO:-/home/trader/project-mai-tai}' in source
    assert '${DRIFT1_OUT:-/home/trader/live_locked_drift}' in source
    assert '${DRIFT1_PYTHON:-$REPO/.venv/bin/python}' in source
    assert '${DRIFT1_NTFY_URL:-https://ntfy.sh/mai-tai-preopen-28806a5a97b7}' in source


def test_the_delivery_flags_are_present_and_no_failure_is_swallowed() -> None:
    source = WRAPPER.read_text(encoding="utf-8")
    # ⛔ Check CODE, not prose. The wrapper's comments NAME these flags to explain them, so a
    # whole-file substring check passes even after the flag is deleted from the curl call — it was
    # written that way first, and a mutant restoring plain `curl -s` did not turn it red.
    code = [line for line in source.splitlines() if not line.lstrip().startswith("#")]
    joined_code = "\n".join(code)
    assert "--fail-with-body" in joined_code, "non-2xx must be an error; plain -s returns 0 on HTTP 500"
    assert "--connect-timeout" in joined_code and "--max-time" in joined_code, (
        "a hang is a delivery failure, not an indefinite block in cron"
    )

    # ⛔ NOT a blanket ban: `|| true` is correct on the state-file READ, where a missing file is
    # the expected first-run case. It is banned on the DELIVERY path, where it would restore
    # exactly the defect this fix removes. Naming the one permitted site keeps the guard honest —
    # a blanket ban would have to be loosened later and would then guard nothing.
    swallowed = [ln for ln in code if "|| true" in ln]
    assert len(swallowed) == 1 and "read -r PREV_STATUS" in swallowed[0], (
        f"the only permitted `|| true` is the state-file read; found: {swallowed}"
    )
    assert not any("send_ntfy" in ln and "|| true" in ln for ln in code)
    joined = "\n".join(code)
    assert "CURL_RC" in joined and "DELIVERY_RC" in joined, "the exit code must be captured, not dropped"

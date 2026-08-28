#!/usr/bin/env python3
"""Run the D6 fan-out outcome acceptance once for each completed ET session day.

The paired-leg, matched-fill, and refused-exit controls use ET calendar-day boundaries, so the
scheduled target uses that same exact [00:00, next 00:00) shape.  The duplicate-cost control keeps
its published historical incident interval, while its requested-window target is still the same
calendar day.  The root cron invokes this file across the UTC hours that can be 00:17-01:17 ET. The runner
skips weekends/full-closure holidays and writes one durable result per session day.  PASS is the
only success marker.  FAIL, COULD_NOT_TELL, and UNEXERCISED are all notified and remain visibly
non-green.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, time, timedelta
import hashlib
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType
from typing import Callable, Sequence
from zoneinfo import ZoneInfo


PASS = 0
COULD_NOT_TELL = 2
SESSION_SLICE_START = time(0, 0)
EASTERN_TZ = ZoneInfo("America/New_York")
DEFAULT_OUT_DIR = Path("/home/trader/fanout_outcome_acceptance")
DEFAULT_NTFY_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
HISTORY_MAX_BYTES = 5_000_000


@dataclass(frozen=True)
class SessionWindow:
    session_date: str
    since: datetime
    until: datetime


@dataclass(frozen=True)
class ScheduledResult:
    exit_code: int
    lines: tuple[str, ...]


def _is_session_day(candidate) -> bool:  # type: ignore[no-untyped-def]
    # Lazy by design. A stale production venv must still be able to start this runner and replace
    # yesterday's SUCCESS with a durable IN_PROGRESS record before an application import can fail.
    from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

    return candidate.weekday() < 5 and candidate not in US_MARKET_HOLIDAYS


def completed_session_window(now: datetime) -> SessionWindow:
    """Return the last completed ET calendar-day slice used by the compiled D6 baselines."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must carry an explicit timezone")
    now_et = now.astimezone(EASTERN_TZ)
    candidate = now_et.date() - timedelta(days=1)
    while not _is_session_day(candidate):
        candidate -= timedelta(days=1)
    return SessionWindow(
        session_date=candidate.isoformat(),
        since=datetime.combine(candidate, SESSION_SLICE_START, tzinfo=EASTERN_TZ),
        until=datetime.combine(candidate + timedelta(days=1), SESSION_SLICE_START, tzinfo=EASTERN_TZ),
    )


def _load_acceptance(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("installed_fanout_outcome_acceptance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load acceptance report from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _default_acceptance_path() -> Path:
    installed = Path(__file__).with_name("check.py")
    if installed.exists():
        return installed
    return Path(__file__).with_name("fanout_outcome_acceptance.py")


def _atomic_write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        handle.write(contents)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)


def _begin_status(path: Path, contents: str) -> str:
    """Detach any old result before publishing IN_PROGRESS, then read it off the live path.

    The rename comes before both the read and the replacement write.  A read failure therefore
    leaves current IN_PROGRESS in place; a write failure leaves the canonical STATUS absent.  In
    neither case can an old SUCCESS survive at the path read by the independent monitor.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    prior = path.with_name(f".{path.name}.prior-{os.getpid()}")
    detached = False
    try:
        os.replace(path, prior)
        detached = True
    except FileNotFoundError:
        pass
    _atomic_write(path, contents)
    if not detached:
        return ""
    try:
        return prior.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return ""
    finally:
        try:
            prior.unlink()
        except OSError:
            pass


def _rotate_history(path: Path, *, max_bytes: int | None = None) -> None:
    if max_bytes is None:
        max_bytes = HISTORY_MAX_BYTES
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return
    if size < max_bytes:
        return
    os.replace(path, path.with_name(f"{path.name}.1"))


def _append_history(path: Path, *, now: datetime, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate_history(path)
    with path.open("a", encoding="utf-8") as history:
        history.write(f"===== {now.astimezone(EASTERN_TZ).isoformat()} =====\n{contents}")
        history.flush()
        os.fsync(history.fileno())


def _is_success_for_session(contents: str, session_date: str) -> bool:
    return (
        "[D6-OUTCOME-ACCEPTANCE-SUCCESS]" in contents
        and f"session={session_date}" in contents
    )


def _verify_acceptance_artifact(path: Path, expected_sha256: str) -> str:
    normalized = expected_sha256.strip().lower()
    try:
        valid_hex = len(normalized) == 64 and len(bytes.fromhex(normalized)) == 32
    except ValueError:
        valid_hex = False
    if not valid_hex:
        raise ValueError("acceptance SHA-256 must be exactly 64 hexadecimal characters")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != normalized:
        raise RuntimeError(
            f"installed acceptance artifact SHA-256 mismatch: expected={normalized} actual={actual}"
        )
    return actual


def _denominator_contract(lines: Sequence[str]) -> tuple[bool, str]:
    requirements = {
        "paired_legs": ("usable=", " of "),
        "fill_rate": ("mirror=", "schwab="),
        "duplicate_legs": (" of ",),
        "refused_exits": ("post_exit_episodes=",),
    }
    for metric, needles in requirements.items():
        matches = [line for line in lines if line.startswith(f"metric={metric} ")]
        if len(matches) != 1:
            return False, f"expected one metric={metric} line, found {len(matches)}"
        if any(needle not in matches[0] for needle in needles):
            return False, f"metric={metric} omitted its denominator"
    return True, "all four metric denominators present"


def send_notification(title: str, body: str, *, url: str = DEFAULT_NTFY_URL) -> bool:
    result = subprocess.run(
        [
            "curl",
            "-sS",
            "--fail-with-body",
            "--max-time",
            "20",
            "-H",
            f"Title: {title}",
            "-H",
            "Priority: high",
            "-d",
            body,
            url,
        ],
        capture_output=True,
        text=True,
        timeout=25,
        check=False,
    )
    return result.returncode == 0


def run_once(
    *,
    now: datetime,
    acceptance: ModuleType | None,
    out_dir: Path,
    notify: Callable[[str, str], bool],
    acceptance_path: Path | None = None,
    acceptance_sha256: str | None = None,
) -> ScheduledResult:
    status_path = out_dir / "STATUS.txt"
    bootstrap = (
        "[D6-OUTCOME-ACCEPTANCE-STARTED] session=pending_calendar "
        "verdict=IN_PROGRESS success_marker=absent "
        "denominator=pending completed ET calendar-day session\n"
    )
    # This precedes the lazy application-calendar import in completed_session_window(). A stale
    # venv can fail after this point, but it cannot leave yesterday's SUCCESS looking current.
    prior_status = _begin_status(status_path, bootstrap)
    _append_history(out_dir / "history.log", now=now, contents=bootstrap)

    window = completed_session_window(now)
    attempted_path = out_dir / "last_attempted_session.txt"
    if (
        attempted_path.exists()
        and attempted_path.read_text(encoding="utf-8").strip() == window.session_date
        and _is_success_for_session(prior_status, window.session_date)
    ):
        # Restore the already-completed result after recording this duplicate invocation. A prior
        # NONPASS is never skipped: the next scheduled attempt reruns and can notify again.
        skipped = (
            f"[D6-OUTCOME-ACCEPTANCE-SKIPPED] session={window.session_date} "
            "reason=already_reported denominator=one completed ET calendar-day session"
        )
        _atomic_write(status_path, prior_status)
        _append_history(out_dir / "history.log", now=now, contents=skipped + "\n")
        return ScheduledResult(
            PASS,
            (skipped,),
        )

    # Clear any prior PASS before loading or running the acceptance module.  If import, query, or
    # process execution crashes, STATUS remains bound to this window as IN_PROGRESS; yesterday's
    # success can never survive as the apparent current result.
    started = (
        f"[D6-OUTCOME-ACCEPTANCE-STARTED] session={window.session_date} "
        f"window=[{window.since.isoformat()}, {window.until.isoformat()}) "
        "verdict=IN_PROGRESS success_marker=absent "
        "denominator=one completed ET calendar-day session\n"
    )
    _atomic_write(status_path, started)
    _append_history(out_dir / "history.log", now=now, contents=started)
    if acceptance is None:
        if acceptance_path is None:
            raise ValueError("acceptance_path is required when acceptance is not supplied")
        if acceptance_sha256 is None:
            raise ValueError("acceptance_sha256 is required when acceptance is not supplied")
        _verify_acceptance_artifact(acceptance_path, acceptance_sha256)
        acceptance = _load_acceptance(acceptance_path)

    report = acceptance.run_report(since=window.since, until=window.until)
    report_lines = tuple(str(line) for line in report.lines)
    denominator_ok, denominator_reason = _denominator_contract(report_lines)
    effective_verdict = str(report.verdict)
    effective_code = int(report.exit_code)
    if effective_code == PASS and not denominator_ok:
        effective_verdict = "COULD_NOT_TELL"
        effective_code = COULD_NOT_TELL

    common = (
        f"session={window.session_date} "
        f"window=[{window.since.isoformat()}, {window.until.isoformat()}) "
        f"verdict={effective_verdict} denominators={'present' if denominator_ok else 'invalid'}"
    )
    if effective_code == PASS:
        marker = f"[D6-OUTCOME-ACCEPTANCE-SUCCESS] {common}"
    else:
        marker = f"[D6-OUTCOME-ACCEPTANCE-NONPASS] {common}"
    output_lines = (marker, f"denominator_contract={denominator_reason}", *report_lines)
    rendered = "\n".join(output_lines) + "\n"

    out_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(status_path, rendered)
    _append_history(out_dir / "history.log", now=now, contents=rendered)

    if effective_code != PASS:
        report_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
        body = f"{marker}\nreport_sha256={report_hash}\n{rendered}"
        if not notify("D6 outcome acceptance NONPASS", body):
            failed_lines = (*output_lines, "notification=FAILED session_not_marked=1")
            failed_rendered = "\n".join(failed_lines) + "\n"
            _atomic_write(out_dir / "STATUS.txt", failed_rendered)
            _append_history(out_dir / "history.log", now=now, contents=failed_rendered)
            return ScheduledResult(
                COULD_NOT_TELL,
                failed_lines,
            )

    _atomic_write(attempted_path, window.session_date + "\n")
    return ScheduledResult(effective_code, output_lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    parser.add_argument("--acceptance", type=Path, default=_default_acceptance_path())
    parser.add_argument("--acceptance-sha256", required=True)
    parser.add_argument("--verify-artifact-only", action="store_true")
    args = parser.parse_args(argv)
    if args.verify_artifact_only:
        actual = _verify_acceptance_artifact(args.acceptance, args.acceptance_sha256)
        print(f"[D6-INSTALL-ARTIFACT-VERIFIED] acceptance_sha256={actual}")
        return PASS
    result = run_once(
        now=datetime.now(EASTERN_TZ),
        acceptance=None,
        out_dir=args.out_dir,
        notify=lambda title, body: send_notification(title, body),
        acceptance_path=args.acceptance,
        acceptance_sha256=args.acceptance_sha256,
    )
    print("\n".join(result.lines))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

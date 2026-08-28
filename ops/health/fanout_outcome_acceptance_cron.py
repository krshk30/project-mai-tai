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

from project_mai_tai.strategy_core.time_utils import EASTERN_TZ, US_MARKET_HOLIDAYS


PASS = 0
COULD_NOT_TELL = 2
SESSION_SLICE_START = time(0, 0)
DEFAULT_OUT_DIR = Path("/home/trader/fanout_outcome_acceptance")
DEFAULT_NTFY_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"


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
    temporary.write_text(contents, encoding="utf-8")
    os.replace(temporary, path)


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
) -> ScheduledResult:
    window = completed_session_window(now)
    attempted_path = out_dir / "last_attempted_session.txt"
    if attempted_path.exists() and attempted_path.read_text(encoding="utf-8").strip() == window.session_date:
        return ScheduledResult(
            PASS,
            (
                f"[D6-OUTCOME-ACCEPTANCE-SKIPPED] session={window.session_date} "
                "reason=already_reported denominator=one completed ET calendar-day session",
            ),
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
    _atomic_write(out_dir / "STATUS.txt", started)
    if acceptance is None:
        if acceptance_path is None:
            raise ValueError("acceptance_path is required when acceptance is not supplied")
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
    _atomic_write(out_dir / "STATUS.txt", rendered)
    with (out_dir / "history.log").open("a", encoding="utf-8") as history:
        history.write(f"===== {now.astimezone(EASTERN_TZ).isoformat()} =====\n{rendered}")

    if effective_code != PASS:
        report_hash = hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]
        body = f"{marker}\nreport_sha256={report_hash}\n{rendered}"
        if not notify("D6 outcome acceptance NONPASS", body):
            failed_lines = (*output_lines, "notification=FAILED session_not_marked=1")
            _atomic_write(out_dir / "STATUS.txt", "\n".join(failed_lines) + "\n")
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
    args = parser.parse_args(argv)
    result = run_once(
        now=datetime.now(EASTERN_TZ),
        acceptance=None,
        out_dir=args.out_dir,
        notify=lambda title, body: send_notification(title, body),
        acceptance_path=args.acceptance,
    )
    print("\n".join(result.lines))
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())

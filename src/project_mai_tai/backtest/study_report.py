"""study_report.py -- the VALUE rules as CODE (the output constraint).

WHY THIS EXISTS (2026-07-17)
---------------------------
Prose rules get recorded and then violated. The "percentages, not dollars" rule was written
2026-07-15, endorsed by the operator, and BROKEN by the next two studies: the floor sweep and the
#467 port both printed bare dollar totals, each of which flipped a conclusion under a drop-one. A
discipline that lives only in prose is a note, not a discipline.

The distinction that motivates this module (operator, 2026-07-17):
  - VALUE rules are assertable -> make them CODE. They fire without anyone remembering.
  - JUDGMENT rules ("could I have known this at entry?", "has the other bot solved this?",
    "does the live DECISION act on it?") are NOT automatable -> they stay prose. This module
    deliberately does not try to encode them.

Every value-rule failure this month was one of these five; each is refused here at the API level:
  1. PERCENTAGES, MEDIAN-FIRST, DROP-ONE (by name).  -> report_returns() is the only sanctioned
     reporter. It requires per-trade PERCENTAGES, leads with the median, always prints a drop-one,
     and has NO dollar-only entry point. Dollars may appear only BESIDE the %, never alone.
  2. PIN BOTH DATE BOUNDS (a universe that moves with the wall clock is not a study).
     -> pinned_window() raises on a missing/unbounded bound.
  3. READ THE LIVE ENV, NOT THE DEFAULT (a threshold was code-5 vs live-15).
     -> read_threshold_from_proc() reads /proc and reports the source.
  4. A LIVE-STATE CLAIM EXPIRES (a 07:42 "zero" repeated for six hours while v2 traded).
     -> as_of() stamps a query with max(ts) so a stale claim self-dates.
  5. READ THE CLOCK, DON'T CARRY IT (drifted three times in one session).
     -> study_header() prints the wall clock at run time.
"""
from __future__ import annotations

import statistics as st
import subprocess
from datetime import UTC, date, datetime
from typing import Sequence


class BareDollarError(ValueError):
    """Raised when a study tries to report a bare dollar total. Percentages, not dollars."""


class UnboundedWindowError(ValueError):
    """Raised when a study window is missing a bound. Pin BOTH date bounds, always."""


def _as_date(x, *, name: str):
    if x is None:
        raise UnboundedWindowError(
            f"{name} bound is None -- a universe that moves with the wall clock is not a study. "
            f"Pin BOTH bounds explicitly."
        )
    if isinstance(x, (date, datetime)):
        return x
    s = str(x).strip()
    if not s or s.lower() in {"now", "current_date", "today", "infinity"}:
        raise UnboundedWindowError(
            f"{name} bound {s!r} is relative/open -- pin an explicit calendar date "
            f"(CURRENT_DATE-8 silently changed the population on 2026-07-17)."
        )
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as exc:
        raise UnboundedWindowError(f"{name} bound {s!r} is not an ISO date: {exc}") from exc


def pinned_window(lo, hi) -> tuple[str, str]:
    """Validate a study window has BOTH bounds, both explicit dates, lo < hi. Returns (lo, hi) as
    ISO strings for interpolation into SQL. Raises UnboundedWindowError otherwise. This is the code
    form of "pin both date bounds" -- an unbounded upper bound silently grew the reach-curve universe
    from 39 to 42 on 2026-07-17 (caught only because the count didn't match a known value)."""
    dlo, dhi = _as_date(lo, name="lower"), _as_date(hi, name="upper")
    if not dlo < dhi:
        raise UnboundedWindowError(f"window lower ({lo}) must be < upper ({hi})")
    return str(lo), str(hi)


def _dropone_by_name(values: Sequence[float], names: Sequence[str] | None):
    """Return (min, max) of the MEDIAN across leave-one-NAME-out removals. Name-level, not
    trade-level: on 2026-07-17 a trade-level drop-one moved CW's median the *flattering* way
    (+0.75 -> +1.05) while removing whole names flipped it negative 4 of 9 times. If names is None,
    falls back to leave-one-TRADE-out and says so."""
    vals = list(values)
    if len(vals) < 3:
        return None
    if not names:
        outs = [st.median(vals[:i] + vals[i + 1:]) for i in range(len(vals))]
        return (round(min(outs), 3), round(max(outs), 3), "trade-level (no names given)")
    by = {}
    for v, nm in zip(vals, names):
        by.setdefault(nm, []).append(v)
    if len(by) < 3:
        return None
    outs = []
    for drop in by:
        rest = [v for v, nm in zip(vals, names) if nm != drop]
        outs.append(st.median(rest))
    return (round(min(outs), 3), round(max(outs), 3), "name-level")


def report_returns(
    title: str,
    pct: Sequence[float],
    *,
    names: Sequence[str] | None = None,
    dollars_beside: Sequence[float] | None = None,
) -> dict:
    """THE ONLY SANCTIONED RETURN REPORTER. Requires per-trade PERCENTAGES (non-empty). Leads with
    the MEDIAN, shows the mean beside it (their divergence is the bimodality tell), always prints a
    drop-one by NAME, and reports win%. There is deliberately NO dollar-only entry point: dollars may
    be passed as `dollars_beside` (printed AFTER the %, as context) but can never be the headline.

    Refuses:
      - an empty/None pct list (nothing to report);
      - a `dollars_beside` without `pct` (that is a bare dollar total) -> BareDollarError.

    Returns the computed stats dict (median/mean/win/n/dropone) so callers can assert on it.
    """
    vals = [float(x) for x in (pct or [])]
    if not vals:
        raise BareDollarError(
            f"{title}: report_returns requires per-trade PERCENTAGES and got none. "
            f"A dollar total with no per-trade % is exactly the banned output."
        )
    n = len(vals)
    median = round(st.median(vals), 3)
    mean = round(sum(vals) / n, 3)
    win = round(100 * sum(1 for v in vals if v > 0) / n, 1)
    d1 = _dropone_by_name(vals, names)
    # NB: printed strings are ASCII-only on purpose -- this module runs on the box (UTF-8) AND may be
    # run locally on a Windows cp1252 console, where a stray unicode glyph raises UnicodeEncodeError
    # (a guardrail that crashes on the platform it guards is a liability; capsys hid it in tests).
    print(f"=== {title} -- n={n} (per-trade %, median-first) ===")
    print(f"  MEDIAN {median:+.3f}%   mean {mean:+.3f}%   win {win:.1f}%"
          + ("   !! median/mean SIGN DISAGREE -- inspect the distribution (bimodal?)"
             if (median > 0) != (mean > 0) else ""))
    if d1:
        lo, hi, kind = d1
        flip = "  !! drop-one FLIPS the sign" if (lo > 0) != (hi > 0) else ""
        print(f"  drop-one ({kind}): median in [{lo:+.3f}, {hi:+.3f}]%{flip}")
    else:
        print("  drop-one: n<3 (not enough to drop)")
    if dollars_beside is not None:
        dv = [float(x) for x in dollars_beside]
        print(f"  (context only, NOT the finding) dollars: total ${sum(dv):+.2f} over {len(dv)} "
              f"-- price-weighted, do not compare studies on this")
    return dict(n=n, median=median, mean=mean, win=win, dropone=d1)


def read_threshold_from_proc(env_name: str, *, unit: str = "project-mai-tai-oms", default=None):
    """Read MAI_TAI_<ENV_NAME> from the RUNNING process env (/proc/<pid>/environ), not settings.py.
    Returns (value:str|None, source:str). On 2026-07-17 oms_broker_sync_interval_seconds was 5 in
    code and 15 live -- the source understated it 3x. Always read the running truth and PRINT which."""
    key = f"MAI_TAI_{env_name.upper()}"
    try:
        pid = subprocess.run(["systemctl", "show", unit, "-p", "MainPID", "--value"],
                             capture_output=True, text=True, timeout=10).stdout.strip()
        if pid and pid != "0":
            env = subprocess.run(["sudo", "cat", f"/proc/{pid}/environ"],
                                 capture_output=True, timeout=10).stdout
            for c in env.split(b"\0"):
                if c.startswith(key.encode() + b"="):
                    return c.split(b"=", 1)[1].decode(errors="replace"), "PROC"
    except Exception:
        pass
    return (str(default) if default is not None else None), "DEFAULT(proc-miss)"


def as_of(conn, *, table: str = "fills", ts_col: str = "filled_at", where: str = "") -> str:
    """Return a printable 'as of <max(ts)> ET' stamp for a live-state table, so any claim built on it
    self-dates. On 2026-07-17 a 07:42 'v2 zero trades' was repeated for six hours while v2 traded at
    09:01/10:01/12:28 -- a live-state claim has a timestamp and it expires. Print this next to it."""
    clause = f" WHERE {where}" if where else ""
    row = conn.execute(f"SELECT max({ts_col}) FROM {table}{clause}").fetchone()
    ts = row[0] if row else None
    if ts is None:
        return f"as of: {table}.{ts_col} empty{(' (' + where + ')') if where else ''}"
    return f"as of max({table}.{ts_col}) = {ts.astimezone().isoformat(timespec='seconds')} " \
           f"(re-check before repeating this later)"


def study_header(title: str, *, box_clock: bool = True) -> None:
    """Print a study header with the WALL CLOCK at run time (from `date`, not a carried estimate) so
    a drifted time-claim contradicts its own report. Date.now() is used deliberately here -- this is a
    live diagnostic print, not a reproducible computation."""
    print("=" * 78)
    print(f"STUDY: {title}")
    if box_clock:
        try:
            now = subprocess.run(["date", "-u", "+%Y-%m-%d %H:%M:%S UTC"],
                                 capture_output=True, text=True, timeout=5).stdout.strip()
            et = subprocess.run(["date", "+%Y-%m-%d %H:%M:%S %Z"],
                                capture_output=True, text=True, timeout=5).stdout.strip()
            print(f"run at: {now}  |  local {et}  (read from the box, not carried)")
        except Exception:
            print(f"run at: {datetime.now(UTC).isoformat(timespec='seconds')} (subprocess clock unavailable)")
    print("=" * 78)

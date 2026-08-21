#!/usr/bin/env python3
"""§205 — find fixtures that pin a timestamp against a LIVE clock.

    python ops/health/audit_stale_fixture_clocks.py [--repo .]

⛔⭐⭐ THE DEFECT CLASS, found live 2026-08-21.
`tests/unit/test_v2_reclaim_resting_entry.py` stamped every fixture bar at a fixed
`datetime(2026, 8, 10, 11, 0)` while the code under test read the REAL wall clock. The bar was
therefore ELEVEN DAYS old, and the tests asserted that the strategy should ARM on it.

That passed for a month — not because the assertion was weak, but because the code had no
freshness gate. **The fixture had encoded the defect as intended behaviour.** It only surfaced
when a guard was added that contradicted it, and then four tests went red and looked like the
guard was wrong.

⇒ A fixture whose age is unbounded is not a fixture. It is a test of whatever today's date is.

## The rule, stated from the two live examples

CORRECT (`test_v2_managed_exit.py`) -- the age is SPECIFIED, against the clock the code reads:
    "received_at": datetime.now(UTC) - timedelta(seconds=age_s)

DEFECTIVE (`test_v2_reclaim_resting_entry.py`, before 2026-08-21):
    RTH = int(datetime(2026, 8, 10, 11, 0, tzinfo=_ET).timestamp() * 1000)
An absolute literal has no age of its own -- its age is "however long ago that happens to be",
which grows every day the test is not run.

⇒ **A fixture timestamp must be relative to the clock the code reads, or the clock must be
pinned.** Absolute literals are fine for calendar facts (a DST boundary, a session table) and
wrong for anything that will be aged.

## What this flags — THREE conditions, all required

  1. the file pins an ABSOLUTE timestamp — `datetime(YYYY, M, D...)` or a 13-digit epoch-ms; AND
  2. it never controls a clock; AND
  3. it exercises a module that compares a stored timestamp against a LIVE clock.

!! Condition 3 is what makes the list readable. Without it the first pass returned 50 files, of
which the very first one checked was a FALSE POSITIVE — `test_v2_db_seed_gap_truncation.py`
stubs `_missed_sessions_before_today`, i.e. it controls the clock by stubbing the clock-DEPENDENT
FUNCTION rather than the clock, which no clock regex will ever see.

## ⛔⭐⭐ READ THIS BEFORE TRUSTING THE LIST: THE FALSE-POSITIVE RATE IS HIGH

Three hits were checked by hand on the first run and ALL THREE were false positives, each by a
DIFFERENT legitimate mechanism:

  1. `test_v2_db_seed_gap_truncation.py` stubs `_missed_sessions_before_today` — it controls the
     clock by replacing the clock-DEPENDENT FUNCTION, which no clock regex can see.
  2. `test_polygon_30s_bot.py` injects `clock = {"now": ...}` and reassigns it between phases.
  3. `test_oms_fillable_window.py` passes the instant as an ARGUMENT — `_market_is_fillable(dt)`
     never reads a clock at all.

Each refinement removed one class and promoted a different false positive to the top. **A static
heuristic does not converge on this defect**, because the thing that makes a fixture safe is that
its timestamp passes through a SEAM, and a seam can be a stub, an injected clock, or a parameter.
That is a dataflow property, not a grep.

⇒ TREAT THIS AS A READING LIST, NOT A FINDING LIST. Its value is narrowing ~190 test files to a
dozen worth a human minute each. Anyone reporting its output as a defect count is doing the thing
this whole audit exists to warn about.

## ⛔⭐⭐ A DEAD END WORTH RECORDING: SHIFTING THE CLOCK DOES NOT FIND THIS

The obvious empirical check — run the suite with every project clock shifted +N days and see what
breaks — was built and DISCARDED. It shifts the code's clock but not the test's, so it breaks
precisely the tests that are written CORRECTLY (those deriving fixture times from `now`) and
leaves the defective absolute-literal ones untouched. It reported 77 failures across 19 files at
both +7d and +90d, and not one was a finding.
⇒ It measures the inverse of the target. Do not rebuild it.

⛔⭐ AND THE TARGET CLASS IS STRUCTURALLY INVISIBLE TO A GREEN SUITE. A fixture like this only
fails once someone ADDS the guard that contradicts it — which is exactly how the reclaim one
surfaced, and why it then looked like the new guard was wrong rather than the old fixture. No
test run can find it; only reading the pair can.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

FIXED_TS = re.compile(r"datetime\(\s*\d{4}\s*,\s*\d{1,2}\s*,\s*\d{1,2}|(?<!\d)1[6-9]\d{11}(?!\d)")
CLOCK_CONTROL = re.compile(
    r"_now_ms|utcnow|datetime\.now|freeze_time|freezegun|monkeypatch\.setattr\([^)]*now|"
    r"NOW_MS|time_machine|clock|[\"']now[\"']\s*[:\]]|_missed_sessions"
)
# a live-clock age comparison in SOURCE: now() minus something
AGE_CMP = re.compile(r"(_now_ms\(\)|utcnow\(\)|datetime\.now\([^)]*\))\s*-\s*")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=".")
    args = ap.parse_args()
    repo = Path(args.repo).resolve()

    tests = sorted((repo / "tests").rglob("test_*.py"))
    src = sorted((repo / "src").rglob("*.py"))

    # condition 3's input: which SOURCE modules age a stored timestamp against a live clock
    aging: dict[str, int] = {}
    for f in src:
        body = f.read_text(encoding="utf-8", errors="replace")
        c = sum(1 for ln in body.splitlines()
                if AGE_CMP.search(ln) and not ln.lstrip().startswith("#"))
        if c:
            aging[f.relative_to(repo).as_posix()] = c
    # map a module path to the dotted name a test would import
    aging_mods = {
        p.replace("src/", "").replace(".py", "").replace("/", "."): c
        for p, c in aging.items()
    }

    flagged, weak = [], []
    for f in tests:
        body = f.read_text(encoding="utf-8", errors="replace")
        if not FIXED_TS.search(body) or CLOCK_CONTROL.search(body):
            continue
        n = len(FIXED_TS.findall(body))
        # condition 3 — does this test reach a module that AGES a timestamp?
        # !! FULL DOTTED PATH ONLY. Matching the last component ("service", "events",
        # "models") hit almost every file and reported three aging modules for tests that
        # merely used the word — the loose version made the list longer and worthless.
        touched = sorted(m for m in aging_mods if m in body)
        rel = f.relative_to(repo).as_posix()
        (flagged if touched else weak).append((rel, n, touched))

    print("=" * 86)
    print("S205 — FIXTURES TO READ: absolute timestamp + no clock control + AGED by the code")
    print("=" * 86)
    if not flagged:
        print("  none - and that is a MEASURED none: "
              f"{len(tests)} test files scanned, {len(aging)} aging modules known.")
    for rel, n, touched in sorted(flagged, key=lambda x: -x[1]):
        print(f"  {n:>3} fixed ts   {rel}")
        for m in touched[:3]:
            print(f"                 -> ages a timestamp: {m} ({aging_mods[m]} comparison(s))")

    print()
    print(f"!! {len(weak)} further file(s) pin a timestamp and control no clock, but reach no")
    print("   module that ages one. NOT flagged: an absolute date is correct for a calendar fact.")
    print("!! NEITHER NUMBER IS A VERDICT. Read each flagged pair and ask: if this fixture's age")
    print("   is unbounded, what is the assertion actually claiming?")
    print("!! And a green suite CANNOT confirm this list — the defect only fails once someone adds")
    print("   the guard that contradicts it. Absence of failures is not absence of the defect.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Regression watch — has a defect we already PAID FOR come back?

⛔⭐⭐ WHY THIS EXISTS, AND WHY A MARKER LIST WOULD BE WORSE THAN NOTHING.
Operator, 2026-09-11: *"I don't want to see the same old bugs from the past."* The obvious build is
a list of markers from old defects. That build is a trap. On the morning he asked, THREE old-defect
markers fired before the open — `V2-DB-SEED-GAP`, `V2-CW-SEED-CAP`, `V2-BOOT-HOLD` — and ALL THREE
were the guard WORKING, not the defect returning. A marker-only watch would have paged three times
on correct behaviour, and by the following week nobody would read it.

⇒ SO EVERY ROW CARRIES THREE FIELDS, NOT ONE:
     marker      — what to look for
     benign      — the shape that means the GUARD IS WORKING (expected, silent)
     recurrence  — the shape that means the DEFECT IS BACK (alert)
⛔ A row whose `recurrence` shape we cannot state is UNARMED, and says so. An unarmed row is honest;
  a row that can never fire is the same failure as a control that can never come out false
  (see feedback: "a check that cannot come out false").

EXIT CODES — deliberately the seed detector's, so a cron can share one reading:
  0 = every ARMED row read OK            1 = a RECURRENCE was found
  2 = CANNOT TELL (fail closed — absence of evidence is never a pass)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field

OK = "OK"
RECURRENCE = "RECURRENCE"
UNARMED = "UNARMED"
CANNOT_TELL = "CANNOT_TELL"


@dataclass(frozen=True)
class Row:
    """One past defect, with the polarity that makes its marker readable."""

    id: str
    defect: str
    marker: str
    benign: str
    recurrence: str
    armed: bool = True


@dataclass
class Reading:
    row: Row
    verdict: str
    detail: str = ""
    evidence: dict[str, object] = field(default_factory=dict)


# ⛔ Each row names a defect we already paid for. `benign` and `recurrence` are the load-bearing
# fields — they are what stop this becoming the marker list that cries wolf.
REGISTRY: tuple[Row, ...] = (
    Row(
        id="SEED1",
        defect="db-seed selects the newest 250 bars BY COUNT, so a gap makes the window span weeks "
        "and the bot arms on stale bars (#721/#743)",
        marker="[V2-CW-SEED-CAP]",
        benign="the cap FIRES and consumes the segment — a pre-watch flip is refused as an arm",
        recurrence="a watchlist symbol the seed-exposure detector reports EXPOSED places a RESTING "
        "ENTRY in the same session without the cap having fired for it",
    ),
    Row(
        id="HOLD1",
        defect="boot-hold suppresses entries FLEET-WIDE and an unreleased hold silently costs a "
        "whole session",
        marker="[V2-BOOT-HOLD]",
        benign="HELD, then a `released ... restoration_complete=1` line",
        recurrence="HELD with NO release line by the time the entry window opens (07:00 ET)",
    ),
    Row(
        id="OWN1",
        defect="a stale flip-owner never retires, so the symbol is locked out for the session "
        "(TNON 09-10; DBGI 09-11 looped 6,440 warnings)",
        marker="[V2-FLIP-OWNER-RECOVERY]",
        benign="`flat_owner_kept_consumed` at INFO — a consumed owner waiting for its SELL flip",
        recurrence="`insufficient_unambiguous_evidence` repeating for one symbol beyond the "
        "unresolvable threshold: it can never satisfy the recovery and will not self-clear",
    ),
    Row(
        id="ROLL1",
        defect="the 04:00 session roll never reached v2, so state never cleared (#657)",
        marker="[V2-SESSION-ROLL]",
        benign="one `boundary_crossed=True` line per trading day",
        recurrence="NO session-roll line at all on a trading day after 04:00 ET",
    ),
    Row(
        id="EXIT1",
        defect="rejected-sell sawtooth — 145 rejects in 55 minutes on one symbol (#608)",
        marker="[OMS-V2-EXIT-REJECT-CEILING]",
        benign="isolated rejects; live:orb routinely refuses 4-19 per episode",
        recurrence="the ceiling EVENT fires for an (account, symbol) pair",
    ),
    # ⛔ UNARMED — kept visible ON PURPOSE. We know the defect; we cannot yet state a shape that
    # distinguishes recurrence from normal operation, so this row must not pretend to watch.
    Row(
        id="EVICT1",
        defect="Redis eviction drops whole keys, losing intents silently",
        marker="(none — eviction leaves no application marker)",
        benign="n/a",
        recurrence="UNSTATED: needs `evicted_keys` sampled against a baseline, which we do not "
        "collect yet",
        armed=False,
    ),
)


def evaluate(row: Row, facts: dict[str, object]) -> Reading:
    """Pure: turn collected facts into a verdict. No I/O, so every branch is testable.

    ⛔ A missing fact is CANNOT_TELL, never OK. That is the whole polarity discipline: this watch
    exists because absence was being read as safety.
    """
    if not row.armed:
        return Reading(row, UNARMED, "no recurrence shape stated; not watched")

    value = facts.get(row.id)
    if value is None:
        return Reading(row, CANNOT_TELL, "fact not collected")
    if not isinstance(value, dict):
        return Reading(row, CANNOT_TELL, f"malformed fact {type(value).__name__}")
    if value.get("readable") is not True:
        return Reading(row, CANNOT_TELL, str(value.get("detail") or "source unreadable"))

    # ⛔⭐⭐ `recurred` MUST BE PRESENT AND BOOLEAN. Reviewed 2026-09-11 by codex-2, and the finding
    # was mine to be embarrassed by: the first cut fell through to OK when `recurred` was MISSING,
    # None, or a non-bool, because it tested `is True` and let everything else past. That is this
    # module's own thesis — absence is not safety — violated inside the module that exists to
    # enforce it. A collector that reports "readable" without answering the question has NOT
    # answered it.
    recurred = value.get("recurred")
    if not isinstance(recurred, bool):
        kind = "missing" if "recurred" not in value else type(recurred).__name__
        return Reading(row, CANNOT_TELL, f"`recurred` is {kind}, not a bool", dict(value))

    if recurred:
        return Reading(row, RECURRENCE, str(value.get("detail") or ""), dict(value))
    return Reading(row, OK, str(value.get("detail") or ""), dict(value))


def render(readings: list[Reading]) -> str:
    lines = ["regression watch — has an old defect come back?", ""]
    for r in readings:
        lines.append(f"  [{r.verdict:<11}] {r.row.id:<7} {r.row.defect[:88]}")
        if r.verdict == RECURRENCE:
            lines.append(f"                        RECURRENCE: {r.row.recurrence}")
            lines.append(f"                        seen: {r.detail}")
        elif r.verdict == CANNOT_TELL:
            lines.append(f"                        ⛔ CANNOT TELL: {r.detail}")
        elif r.verdict == UNARMED:
            lines.append(f"                        (unarmed) {r.row.recurrence}")
    return "\n".join(lines)


def exit_code(readings: list[Reading]) -> int:
    """⛔ RECURRENCE outranks CANNOT_TELL: a found defect must not be masked by an unread row."""
    if any(r.verdict == RECURRENCE for r in readings):
        return 1
    if any(r.verdict == CANNOT_TELL for r in readings):
        return 2
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="regression watch (read-only)")
    ap.add_argument(
        "--facts",
        default="-",
        help="JSON mapping row id -> {readable, recurred, detail}; '-' reads stdin",
    )
    a = ap.parse_args(argv)
    try:
        raw = sys.stdin.read() if a.facts == "-" else open(a.facts, encoding="utf-8").read()
        facts = json.loads(raw or "{}")
        if not isinstance(facts, dict):
            raise ValueError("facts must be a JSON object")
    except Exception as exc:  # noqa: BLE001 — any failure to read is CANNOT TELL, never a pass
        print(f"⛔ CANNOT TELL — REFUSING: {type(exc).__name__}: {exc}")
        return 2

    readings = [evaluate(row, facts) for row in REGISTRY]
    print(render(readings))
    return exit_code(readings)


if __name__ == "__main__":
    raise SystemExit(main())

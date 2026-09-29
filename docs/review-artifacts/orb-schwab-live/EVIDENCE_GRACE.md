# ORB evidence delay: activation-blocker follow-up

Builder: codex. Base: 90106fb4ebbb1feda7247d420352114d14dcd084 (merged #1064).
Scope: ORB Schwab exit evidence, its read-only observation reporting, tests, and rollout docs.
No broker adapter, OMS execution logic, ATR/v2 strategy, paper exit rule, or flag default changed.
Production inspection was read-only. No service restart, order, config change or install occurred.

## Call against the design

Schwab bars can be written after their minute closes. Treating every absent bar at +3 seconds
as a critical incident is a checker-timing defect, not proof of a trading failure. The reviewer
supplied 7,220 RTH bars over seven days: p50 2.6s, p95 3.3s, p99 33s, maximum 228s; 1,351 exceeded
3s. Those figures were not remeasured in this change. The requested 90-second margin exceeds
that p99, but does not claim all evidence arrives on time.

- SCHWAB_BAR_EVIDENCE_GRACE_SECONDS=90. An absent completed break bar or latest ATR bar is
  PENDING through +90 seconds from its own minute close; still absent at +91 is incident-worthy
  missing evidence. A completed bar arriving at +3.5 is judged then, not held until +90.
- A pending or missing body never generates a strategy exit, even if a later ATR flip exists.
  Native target/stop orders stay untouched while waiting. The existing OMS exact-child-fill
  check, pair release and fresh held-quantity reread still decide whether a sell is needed.
- Remember specifically observed missing ATR minutes in the owned entry's persisted context.
  A 90-second timeout cannot use a moving "last minute" clock, which changes every 60 seconds.
  Newer bars do not resolve an older missing minute; that exact bar must arrive. Another fill
  does not inherit the previous trip's pending set. No new historic gap-filling rule is added.
- Query retries remain bounded at two seconds while evidence is missing, including an older
  outstanding minute when the newest read itself reports complete. The source remains the
  existing persisted Schwab series; no gateway or REST fallback is introduced.
- Database read errors, bad provenance and insufficient seed are not a normal arrival delay.
  They remain independently reported even while the break body is pending. Unreadable evidence
  is UNKNOWN, not a reason to cancel native protection or infer a fill.
- An incident is still durable and deduplicated per entry. Repeated polls/restarts do not page
  again. Missing minute IDs/deadline context are included. Observation mode records pending/
  expired states but still writes no incidents, intents, orders or gateway subscriptions.

## Verification

- Focused ORB/native bracket/v2 exits/fan-out/watch regression suites: **665 passed**.
- New core/route checks on base exit implementation: **8 failed / 100 passed** (RED control).
  This is a deliberately failing regression control, not a production failure.
- Nine new mutations killed: restore 3s grace; never expire; reset the deadline each minute;
  forget pending minutes across runs; allow pending-body sell; page pending ATR; hide read
  errors under body pending; drop incident dedup; remove the v2 DB collision flag guard.
- The formerly surviving v2 DB guard now has an ORB-owned position fixture. With the flag OFF
  it performs no ORB collision query and matches pre-ORB main fc68b238's accepted v2 outcome.
  No production OMS code was changed to achieve that result.
- Previous 13 guard mutations and four prior-review RED controls rerun successfully; the old
  flag-OFF baseline control still passes. The body-close mutation target was made compatible
  with the new explicit pending guard without weakening its assertion.
- Full unit suite: head **56 failed / 4,625 passed**; fresh exact base **56 failed / 4,615 passed**.
  All FAILED node names identical. This is local baseline parity, not a clean full-suite pass.
  Linux CI and independent review are separate; live broker and phone tests are unexercised.
- Ruff and whitespace checks pass.

Reproduce new controls with PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src python
docs/review-artifacts/orb-schwab-live/evidence_grace_controls.py. Mutations replace functions
in isolated processes, never repository files. Raw local logs: /tmp/orb-grace-focused.log,
/tmp/orb-grace-controls.log, /tmp/orb-grace-prior-controls.log, /tmp/orb-grace-head-unit.log,
/tmp/orb-grace-base-unit.log. Base suite was run in a fresh Git snapshot at 90106fb4.

## Remaining decisions

The follow-up needs its own review/pin before merge. Live ORB remains OFF; this change alone
does not clear attended Schwab place/reprice/cancel, early-close, phone delivery, or other
activation requirements. See OBSERVE_INSTALL_PLAN.md for the current exact-SHA candidate,
the unpinned #1065 exclusion, preflight, unit/PID inventory, /proc flags and rollback boundaries.
The read-only September 30 report is scheduled at 10:02 ET for the 09:25-10:00 window; it checks
actual installation first and cannot deploy or enable anything. No signals means NOT VALIDATED.

# Lane G frozen-source review receipt

Per-intent dispatch ONLY. Coarse SQL selects current 04:00 ET session buys,
same-segment buys across age, all working/unknown-status buys, NULL timestamps,
and explicit unreadable identity. Historical unrelated terminal rows are not
materialized. DAY does not prove old working orders expired; GTC never ages
out while working. SQL includes absent/null/zero status conservatively.

Fill retirement requires exact account/symbol/segment AND slot. The query's
broader same-segment history retention does not grant different-slot fill
retirement. Unreadable same-segment slot remains refused. Same-slot fill proof
is checked before an unrelated pending row can obscure it.

Each dispatch refusal emits WARNING:
`[OMS-MIRRORHOLD1] account=... symbol=... phase=refused reason=<code> orders_considered=N`.
N is the materialized candidate count, or zero when an earlier guard refuses
before the scan. Precheck, hold/resubmit, 8% guard, RPG abort proof, cache,
tick/queue/restore call paths, service, bot, strategy, parent C-row, and shared
handoff are unchanged. Source method protection receipt lists every unchanged
method and restricts changed existing methods to gate and dispatch.

## Controls and actual-row replays

- 134 passed in 19.59s: 65 new narrow controls + 69 existing MIRRORHOLD1 tests.
  Raw `/tmp/mirrorholdG-slot-final-focused.log` and `.xml`.
- AIXI recorded 09:35:04 intent: 48 actual earlier orders (47 prior-session and
  one same-session earlier unrelated fill), 11 actual earlier Fill rows.
  All-history gate reproduces `dispatch_uncertain`; dispatch-only session gate
  reserves successfully, then real adapter/fake SDK makes one CONTROLLED place
  and returns a CONTROLLED ACK. No additional observed Webull trade claimed.
- FLYE recorded 09:55:49 retry: 38 actual earlier broker-rejected orders, linked
  intents and audits preserved. Both all-history and dispatch gates clear;
  one CONTROLLED place/ACK. Real 10:00:40 fill is recorded in the fixture but
  excluded from the earlier replay. Raw `/tmp/mirrorholdG-replays.json` and `.log`.
- PostgreSQL-compiled query executed under bounded READ ONLY transaction on
  the VPS: AIXI retains only actual earlier current fill client
  `schwab_1m_v2-AIXI-open-ec407a4e45b0`; FLYE retains zero pre-intent buys.
  PostgreSQL VALUES controls separately retain 10 named current/working/same
  segment/null/unknown candidates and exclude three old unrelated terminals.
  Raw `/tmp/mirrorholdG-slot-final-postgres.json`; SQL `/tmp/mirrorholdG-postgres.sql`.
- 5/5 isolated mutations killed by assertion failures, each with a green
  baseline: removed session bound, removed current pending guard, excluded
  old same-segment fill, removed exact-slot retirement guard, removed refusal
  WARNING. No tracked source mutated. Raw `/tmp/mirrorholdG-slot-final-mutations.json`
  and `/tmp/mirrorholdG-mutants/*-{baseline,mutant}.{json,log}`.
- Both account names and pre/post-04:00 clocks are synthetic scan controls;
  secondary account is configured as the mirror only inside that test.
- Ruff and whitespace checks passed. Three expected SQLAlchemy warnings come
  from controlled aborted rows with no intent and the unchanged RPG proof.

Source SHA256: `fe621f0d7ac9f4851f205d6ca06f80aa5b9730b8f3bcb766189fd8ddc9cda1bd`.
Fixture SHA256: `615ba36a4642febac85847c8af26d9e4920d4a48d57e1be63f10c40379ce6233`.
Final capture: 135 orders, 25 time-bounded intents, 135 linked historical
intents, 237 audits, 21 fills; limit+1 complete within declared scopes.
Raw `/tmp/mirrorholdG-live-final-1008.json`. Earlier raw captures are preserved.

## Full Suite and Residual

The common exact-main receipt `/tmp/five-lane-main-1e15adb0-unit.xml` is
48 failed / 7,674 passed / 55 skipped. Its literal failed-name hash reproduces
`e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`
using sorted `classname::name`, joined by newline without a trailing newline.
The pre-slot-correction exploratory full run was interrupted and is NOT a
final receipt. After this frozen commit, run the exact-head full unit suite;
raw paths `/tmp/mirrorholdG-final-head-unit.{xml,log}` and
`/tmp/mirrorholdG-final-unit-comparison.json`. `verify_receipts.py` prints the
entire failed-name sets, added/absent names, raw hashes, and method protection.
No parity assertion is based on counts. Full-suite result is pending at this
freeze; final PR description and parent report must state the actual result,
including any timing swaps without rewriting the shared baseline.

Residual: queue/restore still uses the unchanged all-history gate. A later
AIXI deferred hold may remain blocked by its historical ambiguous orders.
This lane fixes direct per-intent dispatch only. The unrelated legacy Fill
fallback is an explicit proof policy described in STEP0, not fabricated
historical identity. DB-only evidence cannot establish unrecorded broker
orders; no new broker-book read or live acceptance/fill is claimed.

No production writes/trading/merge/pin/activation, no rebase, no other lane
changes. The shared `.venv` is invoked directly with normalized PATH and the
absolute isolated `src` path; it is not reinstalled or relinked.

# ORBPASTLEVEL1-20261021 Exact Original Input Request

All authoring UNRUN. No original packet located in the bounded ORBPAST-specific
inventory read on this resume. The PG copy is not evidence that quotes, original
consumer clocks, all historical gates or native events exist; no dump opened.
Worker performs no retrieval or database access. Request goes through main to
the existing authorized exporter; this document does not authorize collection.

## Required Originals

1. Complete eleven-plan census since Oct01, actual plan/intent/account IDs,
   session dates and all original place/reprice/refusal/cancel decisions. Expected
   reviewer list: MI, JAGX, FLYE, GIXI, NXL Oct01, XHG Oct06, BIYA Oct07,
   AIXI Oct08, OLOX Oct21, SXTC, AMOD Oct02. Dates of unspecified names remain
   UNKNOWN. Reconcile four reported fills/five preview refusals/one ineligible/
   one cancellation against actual records, never manufacture those outcomes.
2. Actual Massive quotes at every decision second, including quote identity,
   symbol, ask/bid, raw source record, raw SIP/provider timestamp and any actual
   local receipt/availability. Include preview and final prewire boundaries and
   quotes changing across awaits where retained. For OLOX include 13:28:02Z,
   13:29:02Z and 13:30:04Z; quoted 13:28:02.5-04.2Z data alone does not prove
   what was available at 13:28:02Z. Absent consumed quote stays UNKNOWN.
3. Bounded Massive trades through each real exit/cancel/session-close horizon,
   original prices/sizes/conditions/IDs/sequences/corrections/cancellations,
   eligibility rule and per-window complete/partial/missing coverage. No global
   tape download needed. Raw nanoseconds must be retained. This driver refuses
   submicrosecond boundary precision it cannot represent; a separate reviewed
   integer-clock adapter is required before treating such boundaries as known.
4. Full intent envelopes and effective settings; durable preview requests and
   responses; order/client/parent/replacement/OTOCO child IDs; fills/partials/
   late fills; local-zero or MAY_HAVE_SENT history where actually retained;
   owner/reservation/managed/account positions and all same-name/day attempts.
   Missing old receipts cannot be backfilled as locally unsent.
5. Original MACD/ATR/body bars and decisions, country/owner/control/session
   inputs, restart and 04ET state. Include source timestamps, known gaps and
   actual observation/dispatch timings. Market tape is insufficient for these.

## V2 Packet Binding

The original untracked v1 contract is preserved in this worktree. Its CSV column
contracts still apply, with these v2 additions for the strengthened driver:

- manifest schema must be `ORBPASTLEVEL1-input-v2`.
- `quote_binding` must be `actual-consumed` or
  `decision-second-counterfactual`. The latter studies actual raw quotes and
  cannot be relabeled as an OMS consumed quote or dispatch replay.
- Nonempty `exporter_identity`, `census_receipt` (source/query/coverage receipt),
  `missing_inputs` list, positive integer actual `quote_max_age_ms`, and explicit
  `declared_time_bases`. A true completeness flag is a producer declaration,
  never independently verified original acceptance.
- `plans` binds full plan_id/symbol/session_date/decision_ids. All eleven
  expected symbols are reconciled; undeclared decisions, duplicate identities,
  cross-symbol or cross-session records refuse rather than silently join.
- `files` binds decisions/quotes/broker_events/trades path and SHA256 inside
  the packet directory. No input file is fetched or modified by the driver.
- quotes additionally require provider exactly `Massive`, source_event_id,
  source_timestamp and raw_record_reference. These are provenance references,
  not verification of the producer's truth. Missing local availability refuses
  strict-arrival selection; SIP time is permitted only in the separately named
  event-time study, and all raw quote rows remain in the output.
- `candidate_source_sha256` must include all eight lane src paths plus the
  driver (enumerated as SOURCE_PATHS in the driver). Hash values must bind the
  final reviewed composition; the ORBPAST branch hashes cannot bind a different
  MOMBUFFER composition. Do not weaken the inventory for an easier replay.

Strict-arrival tape touches use retained local receipt ordering and reject
missing/inverted clocks. Event-time touches use the declared event clock.
Corrections/cancellations, wrong symbols, unknown eligibility and same-time
opposing touches remain explicit uncertainty. Both studies assume decision-time
entry for descriptive exit touches; neither models a native entry, queue,
partial allocation, bracket acceptance, body exit, PnL or target/stop fill.
Full original state replay needs separately reviewed offline fixture adaptation
of the real snapshots. That adapter remains BLOCKED by items 4-5 above; this
price comparator is not an implementation or acceptance of that gate.

## Main-Only Proposed Validation

After independent source/impact review and main's serial execution claim:

```sh
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q tests/unit/test_orb_pastlevel.py tests/unit/test_orb_pastlevel_replay_contract.py tests/unit/test_orb_schwab_bracket_plan.py tests/unit/test_orb_schwab_order_route.py tests/unit/test_orb_schwab_replace_adapter.py tests/unit/test_orbroute1_country_contract.py
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python tests/unit/test_orb_pastlevel_replay.py /ACTUAL/LANDED/PACKET --time-basis strict-arrival
PYTHONPATH=src /Users/velkris/Projects/project-mai-tai/.venv/bin/python tests/unit/test_orb_pastlevel_replay.py /ACTUAL/LANDED/PACKET --time-basis declared-event-time-study
```

These are proposals only, none executed. Run from this worktree or the reviewed
composition with its correct source hashes. No fake sample packet qualifies.
All zero-NEW-vs556, native, original eleven, full/ALL-ON, restart/04ET and Claude
source+impact approval gates remain owed. No installation authority inferred.

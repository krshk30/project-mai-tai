# PRICE1 Candidate Census V2 Source Readiness

Author source only, parent 5296267598b591ea465103fb58e4a4aee1a5dce4 preserved.
No tests/import/compile/AST/replay/provider/runtime executed. The strict P1 window
was respected; only source/prose was edited. Main released it at 15:19:50 ET.
No actual candidate inputs were parsed. No production/trading source changed in
this successor. Main's L13 strict PASS is not borrowed for PRICE1 qualification.

## Narrow Source Fixes

- V2 declares actual study bounds and retained validation-window/capacity config;
  no default end-of-day clock can silently replace 10-12's actual settings.
- Integer UTC minute arithmetic handles a minute's final ns without floating-point
  rounding. Every requested whole minute is present; no sample means null/UNKNOWN.
- Output includes candidate/desired-owner/active-path maxima, last candidate set,
  sample counts and per-minute cap refusals, not just an always-capped owner count.
- Strict duplicate-free bounded JSONL, explicit boolean completeness, predeclared
  input SHA, rehash of processed bytes, and imported engine/consumer root/hash
  binding. No Git subprocess, app constructor beyond the pure warning model,
  provider, database or network connection. CLI requires both source root and SHA.
- Compare requires identical input SHA/config/bounds/cap, preserves both actual
  source bindings, minute deltas and UNKNOWN missing minutes. Acceptance is never
  automatically PASS. V1 artifacts remain in 529; V2 schema intentionally changes.

40 intended new controls ALL UNRUN: precision boundaries, explicit missing minutes,
cap-pressure arithmetic, parser failures, bounds/config/hash/source drift, actual
current-price consumer with synthetic input, and old/new comparison arithmetic.
These are NOT real LNKS/MNDR capture results and do not prove native fills.
Original entry controls remain 61 intended; total 101 UNRUN, collection unverified.

## Existing Paths / Finite Missing Inputs

Bounded filename inventory of the external analysis directory found the PRICE1
source bundle and L2 handoff, but no named PRICE1/MOMNEG candidate-census packet.
This is not a claim that no differently named archive exists. No reviewer Python
or raw tape was imported/executed/read to derive any answer.

Please identify/provide the EXISTING 10-12 warning delivery prefix and config
described in PRICE1-SOURCE-HANDOFF-20261022.md, with producer SHA/provenance and
explicit study bounds. Need all competing candidate names, not only LNKS/MNDR.
Include original consumer observation clocks, A received/published clocks,
gateway sequence/drop/epochs, read checkpoints/invalidations/heartbeats,
legacy-active sets, prior closes, and delivered eligible T/coverage that affects
ownership/lifecycle. No new provider/DB/SSH collection authorized or requested.

Exact missing question: does an existing archive include T delivery for names
the new current-price rule would newly admit? If not, original delivery can still
support a DECLARED PARTIAL study, but cannot prove that counterfactual arrival
path. Historical SIP/event-time data may support a separately declared study;
it must not be relabelled native/local-arrival/as-known evidence. Actual old/new
whole-day candidate census remains UNKNOWN, not a source-authoring blocker.

An existing consumer_sample/transition log may independently supply baseline
observed counts. It cannot invent the new counterfactual or unsampled minutes.
No full-market 48GB export is required by this contract; request a finite retained
warning delivery/config packet with its exact coverage limitations.

## Main-Only Selection / Proposed Commands

Actual existing baseline metadata was read: aa16266823e7ef0ebb58a44e8efb64af417fc2bc
at /Users/velkris/.codex/worktrees/tonight-l7-l8-l9-no-l1/project-mai-tai.
Current author root: /private/tmp/mai-tai-price1-buy-floor-20261022.
Main must freeze/verify its exact successor head and both source hashes first;
never run this driver against a root being edited concurrently.

New module selection: tests/unit/test_price1_candidate_census.py. Main claims
serial execution after its current workload; no worker execution is authorized.
Original 3 PRICE1 modules and preservation/ALL-ON selections remain owed.

The following are proposals, NOT execution receipts. INPUT_JSONL/INPUT_SHA256
must be supplied from an actually delivered, verified packet; they are not known.
HEAD below must be set by main to its verified exact frozen candidate Git SHA.

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/Users/velkris/.codex/worktrees/tonight-l7-l8-l9-no-l1/project-mai-tai/src:/Users/velkris/.codex/worktrees/tonight-l7-l8-l9-no-l1/project-mai-tai /Users/velkris/Projects/project-mai-tai/.venv/bin/python /private/tmp/mai-tai-price1-buy-floor-20261022/tools/price1_candidate_counts.py "$INPUT_JSONL" --declared-head aa16266823e7ef0ebb58a44e8efb64af417fc2bc --expected-input-sha256 "$INPUT_SHA256" --expected-source-root /Users/velkris/.codex/worktrees/tonight-l7-l8-l9-no-l1/project-mai-tai
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/private/tmp/mai-tai-price1-buy-floor-20261022/src:/private/tmp/mai-tai-price1-buy-floor-20261022 /Users/velkris/Projects/project-mai-tai/.venv/bin/python /private/tmp/mai-tai-price1-buy-floor-20261022/tools/price1_candidate_counts.py "$INPUT_JSONL" --declared-head "$HEAD" --expected-input-sha256 "$INPUT_SHA256" --expected-source-root /private/tmp/mai-tai-price1-buy-floor-20261022
```

Main captures separate fresh stdout/raw exit receipts, then passes the two JSON
paths to tools/price1_candidate_compare.py. No data-dependent source tuning after
comparison. Expected cost: two bounded streaming passes over each input (hash,
then hash+process), <=256KiB normalized lines, existing configured model bounds,
<=1440 minute rows plus counters. Actual wall/CPU/memory UNMEASURED. Raw input
size/cost cannot be asserted before the packet lands. No whole-day app replay or
native latency result is inferred from these new source controls.

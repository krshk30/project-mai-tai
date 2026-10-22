# L8 PAPER aggregate last-tick guard: SOURCE ONLY

Task L8-PAPER-AGGREGATE-LASTTICK-20261022; Galileo sole L8 author.
Branch codex/l8-paper-aggregate-lasttick-20261022.
Worktree /private/tmp/mai-tai-l8-paper-aggregate-lasttick-20261022.
Direct base55204356807fd9f081d14a235abafce888a8838a, frozen L1 native tree untouched.
Only momentum_paper_app.py, this L8 test module and this handoff are authored.
No OMS/L7, actor/runtime/L1, queue/status/LATEST or other lane writes.
MOMMISS is deferred/NOTTONIGHT, not cancelled and not folded into this hotfix.

## Intake and exact deployed bytes: still BLOCKED

Full Claude09:38:29 card read after queue FIRST. External reviewer reports RUMBW
observed_aggregate UNANSWERABLE907186da/7d5a9cf1 without sip_ts_ms crashed PAPER
_build_bot_state09:31:42..09:32:18, systemd gave up. Operator-approved09:36 hotfix
was reportedly installed/restarted09:37:02, with stability/readiness afterward.
This is external reported evidence, NOT author/current production inspection.
Reported backup:
/var/backups/project-mai-tai/install-20261022-690f/momentum_paper_app.py.before-hotfix-0937.

Card gives exact predicate `if "sip_ts_ms" in dict(row["detect"])` but DOES NOT
provide deployed comment bytes/full diff/file SHA. Main has requested those from
Claude. Functional source can proceed; byte-identical-live certification CANNOT.
Authored comment is explicitly provisional, not represented as Claude's bytes:

    # Aggregate detections have no SIP trade timestamp.

Need actual deployed two-line diff including whitespace/comment, full deployed
momentum_paper_app.py SHA256, base/preimage or backup receipt and source identity.
Then compare actual bytes; if needed a separately reviewed narrow comment-only
successor, never guessed hash matching or rewritten qualification receipts.
Static base comparison690..5520 for this service file is EMPTY, so no other service
delta must be silently composed to match the reported hotfix. This does not prove
the deployed file has no other dirty changes. Preserve box's dirty hotfix: no clean
checkout/rollback, provider/SSH/restart/install or permissions action by author.

## Source assessment and impact

Read entire _build_bot_state, _decision_row/_position_row/_closed_row and publication
caller; read existing PAPER-service and real control-plane page/API fixture paths.
Completed UNANSWERABLE is included in today's terminal/recent rows; aggregate
detect carries bucket/decision clocks, NOT an actual SIP print. Unconditional
last_tick_at indexing raises before the isolated bot state can publish.

Exactly2source insertions in the comprehension: comment and supplied membership
predicate. Missing-SIP rows stay in recent_decisions, counts, grading and durable
records, but do not enter the last_tick_at dictionary. No fake epoch/decision/bar
timestamp is substituted there. Existing valid SIP int/string conversion and map
ordering remain unchanged, including a valid same-symbol row alongside an aggregate.
All other timestamp-display fallbacks remain unchanged/out of this narrow scope.
Present but invalid SIP values are not newly accepted by this membership guard.

Call graph: periodic _publish_state -> actual _build_bot_state -> real event JSON
-> isolated Redis publication -> control-plane isolated-event ingestion. Canonical
/bot/momentum uses its existing owned LIVE and separately labelled warning ledgers;
/api/bots and /api/overview keep MOMOLD1's exclusion of warning reference totals.
No UI/control-plane source change. Existing warning-history query excludes
UNANSWERABLE from FILLED/EXITED/FINAL history; this hotfix does NOT broaden it or
claim the incident row now appears in that independent Decision Tape query.

No detection, admission, native ownership, slot/cap/permit/order, broker/provider,
handoff sequencing, P&L, timestamp provenance, event retention or logging behavior
changed. No journal/MOMMISS performance code introduced. The guard affects only
construction of the display last_tick_at map, preventing its missing-key exception.

## New authored whole-module inventory, UNRUN

tests/unit/test_l8_paper_aggregate_lasttick.py:5test functions,16intended cases by
manual parameter inventory (NOT executed collection):

- Actual builder on completed observed_aggregate UNANSWERABLE without SIP:2warning
  strategies, no last_tick fabrication, row/status/reason/count preserved, no mutation.
- Valid SIP int/string unchanged:2warning+2legacy strategies x2encodings =8cases.
- Missing-SIP and valid-SIP same-symbol coexistence:2warning strategy cases.
- Actual _publish_state heartbeat+two isolated warning event JSONs:1case.
- Actual page/API pipeline:3routes /bot/momentum,/api/bots,/api/overview. Service
  first builds/publishes missing-SIP state (old source raises before HTTP), then
  existing TestClient/SQLite/Redis fixtures exercise real repository/renderers with
  emitted isolated JSON and retained synthetic UNANSWERABLE records. HTTP200,
  expected LIVE/reference page sections, empty LIVE positions/P&L and no warning
  rows relabelled as LIVE are asserted. Isolated-stream consumption is witnessed.

Fixtures are synthetic case-shape evidence, not copies of unavailable RUMBW event
payloads/prices, actual box health/native calls or endpoint-latency qualification.
Production methods/renderers are not stubbed. External Redis is a recording fake;
page fixture DB is local test SQLite only when MAIN eventually runs the new tests.
No worker DB, imports, AST/compile, tests, app, network, provider, SSH, benchmark,
SQL execution, install or restart occurred. All functional/runtime/render proof UNRUN.

Proposed MAIN-ONLY whole-module command AFTER exact source/head/hash review,
durable serial claim/fresh output paths and separate quiet process inventory:

```sh
env -u PYTEST_ADDOPTS -u PYTEST_PLUGINS PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:. /Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider tests/unit/test_l8_paper_aggregate_lasttick.py --junitxml=/Users/velkris/.codex/task-queues/analysis/L8-PAPER-AGGREGATE-LASTTICK-20261022.xml
```

No-k/maxfail/deselection/xdist/retries. Main owns timing relative to5520 native/full.
Estimated small functional suite, not measured. Additional preservation selections
remain main/reviewer-owned: PAPER service, warning pipeline and canonical live-first
page/API controls, then final composed ALLON/fullvsactual690. Existing AST-based
preservation tests were only read as source; author did not execute them.

Completion gates still pending: actual deployed diff/comment/SHA parity, independent
source review, actual16results/receipts, composed-head preservation/full/native gates
and reviewer final installation authority. Committed source alone is NOT identical
live certification, completed incident replay, production readiness or deploy GO.

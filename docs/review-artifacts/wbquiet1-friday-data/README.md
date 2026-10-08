# WBQUIET1 Friday Data Lane

Owner: codex-2, Lane E. Branch `codex/wbquiet1-friday-data-1008` starts at
`1e15adb03c3758e647d333828bbe3090e24e832e`. This is an offline evidence/reporting
change, not a cadence, cancel, trading, runner, or activation change. Agent D owns
the additional live reader hooks; this lane neither duplicates them nor edits the
shared handoff.

## Independent Receipt And Scope

The bounded SSH-stdin pull froze its upper bound at **2026-10-08 09:03:28.288912 ET**
(`2026-10-08T13:03:28.288912+00:00`), completed in 7.12 seconds, and reported no
collection errors. Its window begins October 2 at 00:00 ET. It captured eight OMS
files, 30,544 structured lines, 18 SDK cancel-417 exception events, 54 retained
Webull fills, and 20 terminal-detail proof records. No broker API was called;
SQL used `BEGIN READ ONLY`, a 10-second statement bound and 500-ms lock bound.
No production file, service, DB, Redis or order was changed.

Original decoded raw SHA256:
`20019a422d47e40b5fbfe82d2fa152800b21b1b8a13cafb1fceec54a5c904417`.
The gzip receipt preserves those original bytes. The extraction script subsequently
gained expected-rotation inventory metadata; the committed 09:03 receipt remains
unchanged, and the replay derives the expected names from its captured inventory.
All source inode/hash/first/last timestamp and per-line provenance are retained.

**Every count below is partial retained OMS evidence, not a complete ET-day,
app-key, account, or all-service wire count.** October 8 is additionally
right-censored at 09:03:28 ET; it must not be called today's final result. Endpoint
counters aggregate this OMS process's SDK calls without an account label.
DB account names (`live:orb`, `live:polygon_30s`, `live:webull_30s`) are inventory,
not runtime credential proof. Exception client IDs are nearest SDK-context values,
not adopted from a later order row. Successful day-list bodies and historical
per-account positions/generation histories are absent.

## Cancel Discrepancy Reconciled

| ET day | Selected final-per-minute calls / failures | Naive cumulative snapshots calls / failures | Actual SDK exception lines / distinct request IDs | Exit-child / other coid shapes |
| --- | --- | --- | --- | --- |
| October 6 | 65 / 5 | 75 / 12 | 5 / 5 | 4 / 1 |
| October 7 | 25 / 10 | 42 / 23 | 10 / 10 | 6 / 4 |

The reviewer's **12 and 23** exactly reproduce the naive repeated-snapshot sums
in this own pull. They are not 35 distinct cancel error events. Failure snapshots
are cumulative within a minute: October 6 at 12:03 UTC contributes 2 instead of
1; 14:29 contributes 5 instead of 2; 18:22 contributes 5 instead of 2. Total naive
12 versus selected 5. October 7 at 15:59, 17:21 and 19:22 UTC each contributes 5
instead of 2; 18:37, 18:51, 18:55 and 19:12 each contributes 2 instead of 1.
Total naive 23 versus selected 10. These are repeated `partial=1` plus `partial=0`
rows, not a repaired/capped failure population. Source paths, line numbers and
every selected/repeated snapshot are in `rule_B.endpoint_failure_reconciliation`.

The selected failure counts independently equal the extracted HTTP 417
`ORDER_CAN_NOT_BE_CANCEL` exception-event counts. There are no conflicting final
counter epochs in this receipt. October 6 detail **2,074 / 306** also reproduces
the agreed baseline. Rule B terminal membership remains **UNMEASURED for all
5 and 10 cancel events**: exception text, final DB status or terminal DETAIL
evidence cannot establish a fresh day-list row at the decision time.

## Coverage And Missing Tails

Expected closed UTC archives `oms.log-20261003` through `oms.log-20261008` are all
present, compressed or plain. `oms.log-20261002.gz` is captured for provenance
but contributes no in-window lines. UTC files straddle ET dates, so an ET-day row
is assembled across archives/current file, never assigned by filename alone.
There are **no absent expected rotated-file intervals** in this inventory.

Positions-counter minute gaps (half-open UTC intervals) are:

| ET day | Missing UTC interval | Minutes |
| --- | --- | --- |
| October 3 | 2026-10-03 20:59 to 22:03 | 64 |
| October 5 | 2026-10-06 00:52 to 00:53 | 1 |
| October 6 | 2026-10-06 23:48 to 23:49 | 1 |
| October 6 | 2026-10-07 01:26 to 01:53 | 27 |
| October 8, partial | 2026-10-08 13:03 to 13:04 | 1 current unfinished minute |

October 2, 4 and 7 have all 1,440 retained positions-counter minutes; that does
**not** prove complete request capture. Other endpoints emit counters only in
nonzero-call minutes. A stopped process may lose its current endpoint-minute
tail even when every archive exists. Exact lost request count and tail cause are
**UNMEASURED**, not zero; the minute gaps cannot distinguish downtime, no call,
or missing instrumentation. No shutdown tail has been invented or filled in.

## A / B / C Verdicts

- A: **UNMEASURED** actual savings, decision-changing minutes and hypothetical
  census `ok=0` windows. The retained search finds 85 decision lines / 47 UTC
  minutes on October 2, 121 / 90 on October 6, and 18 / 18 on October 7. Those
  are search denominators, not proof of zero changed decisions. Actual `live:orb`
  census `ok=0` windows are 0/287, 0/283 and 0/287 respectively. All retained
  shadow account-pass counts are zero before this pull; Friday's new observations
  and Agent D's partial reader hooks are future evidence, not historical repair.
- B: **UNMEASURED** terminal day-list membership for the retained cancel events,
  including the 4 and 6 exit-child-shaped cases on October 6 and 7. No HTTP 417
  is interpreted as positive terminal/zero-fill proof.
- C: **UNMEASURED**, BIYA `schwab_1m_v2-BIYA-open-0e40052d917c`, October 7 19:12
  UTC. Retained UNREADABLE/UNCONFIRMED lines do not preserve literal `body=None`
  or a fresh terminal day-list body. `dead_target_bound` is not substituted for
  that missing proof.

The JSON separately labels an always-flat, one-account 15/60/300-second
opportunity-capacity proxy, including a retained recent-fill variant. It is
**not measured calls saved**, and missing held/working/manual-position history
is never classified as flat. Manual holdings would nominally be first observed
within 60 seconds daytime / 300 overnight instead of 15, plus source lag; this
lane does not change ownership, cadence or any live decision.

## Repeat And Test

Use the shared environment with this worktree's `src` on `PYTHONPATH`:

```sh
python scripts/wbquiet_history_replay.py \
  --input docs/review-artifacts/wbquiet1-friday-data/RAW_PULL_2026-10-08_0903ET.json.gz \
  --output /tmp/new-wbquiet-report.json --markdown /tmp/new-wbquiet-report.md
python -m pytest -q tests/unit/test_wbquiet_history_replay.py
python docs/review-artifacts/wbquiet1-friday-data/mutation_controls.py \
  --output /tmp/new-wbquiet-mutations.json
```

Outputs are exclusive-create. The collector has explicit source, byte, line,
event, SQL and serialized-output bounds; the replay has an 80-MB decoded input
bound. Fixtures test guards, not recorded trading outcomes. Focused verification:
**51 passed**, Ruff clean, **7/7 isolated guard mutations RED** (held cadence,
terminal account, terminal status, freshness, reader window, cumulative-counter
selection, false complete-day claim). `MUTATIONS_2026-10-08.json` records each
control. No production/trading tests are weakened.

Follow-up parser verification: **78 passed**, Ruff clean, **13/13 mutations RED**
in `MUTATIONS_READER_CONTRACT_2026-10-08.json`. The frozen Lane D v1 schema is
`6d38735b80f651d366556c8f3b5a0310c6ebfaa3`, file
`docs/review-artifacts/wbquiet1-reader-hooks/READER_SCHEMA_2026-10-08.md`, SHA256
`1a422dd220a11fa07e2ff1ded30c6eaac8079b944c9e5cc1143e69ed6ba4e2d8`.
The offline parser joins `[WBQUIET-READER]` records to the published SHADOW
account's `committed_generation` using exact process/pass/account/local-read and
acquisition IDs. It retains instrumented adapter invocation counts, not HTTP
counts; renewal is not a positions pull. Multiple anchors, loss or missing
receipts, foreign accounts/processes, unknown/empty acquisitions, unreadable
reads and Schwab-scoped readers never establish a generation match. Even a
positive immutable-cache identity leaves decision equivalence and wire savings
UNMEASURED. The actual 09:03 corpus contains zero such receipts; compatibility
tests are isolated fixtures, not production measurements. Conflicting endpoint
counter epochs are explicitly counted in the daily table; selected maxima are
not actual per-process wire totals.

The exact retained parent baseline is **48 failed / 7,674 passed / 55 skipped**,
XML `/tmp/five-lane-main-1e15adb0-unit.xml`; failed-name SHA256
`e0b9fc7478ff484d88d9882194d3525632c0d2d43353d515b28f63a28464e9a9`
(sorted `classname::name`, newline-joined without terminal newline). This lane's
initial-head XML is `/tmp/five-lane-wbquietE-head-unit.xml`: **48 failed / 7,725
passed / 55 skipped**, 616.74 seconds, for `09c1aaa7`. Failed-name SHA256 is
`5e63ad6caec5865100ce4c8b64524e2fcdc12810adc7b807d53b9e074e61e20f`.
The actual names differ:

- Baseline only: `tests.unit.test_hotfix1_symbol_tick_cache::test_recorded_240_events_per_second_60_seconds_slow_db_flat_transactions[retained-off-existing-row]`.
- Initial head only: `tests.unit.test_nfq2_hotfix1_combined_proof::test_combined_240_events_per_second_60_seconds_active_hold`.

Both are timing benchmarks; no source/trading/benchmark file changes exist in
this lane. This is a reported residual, **not suite parity**. The final reader-
parser head's full suite uses `/tmp/five-lane-wbquietE-final-unit.xml`; its pair
against the retained baseline is pending. The exact baseline is never rerun.

Remaining measurement blockers: per-account decision-time position snapshots,
successfully acquired day-list response bodies, complete external-reader
generation coverage, unknown shutdown tails, and Thursday/Friday post-install
shadow observations. No A/B/C policy AGREE or behavior build follows from this
preparation receipt.

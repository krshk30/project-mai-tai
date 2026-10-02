# NFQ1: durable fresh-price Webull mirror

## Authority and Scope

BUGS v3 FINAL, 2026-10-02, NFQ1 / board row 38. The operator subsequently
explicitly authorized independent parallel starts; the old install-first and
RPG1-first sequencing in the attachment and assessment JSON is superseded.
This branch starts at `b8b0dafbdf583ca4af9eddc8dbf922cbf887a482` and contains
NFQ1 only. No RPG1 patch was adopted. Review only: not eligible to merge or
install. Tonight's b8 install is unchanged. No services, cron, production code,
Redis bulk reads, or broker writes were changed/performed by this lane.

Operator requirement: when OMS has a price from the last ten seconds, send
the Webull waiting order; otherwise hold and send on the next eligible price.
Never send without a price and never silently drop the order.

## Round 1 Review Response

Reviewed starting head: `8bed4ea193d500cc5b8bcf6b4d09543e361b3e30`.
Production source commit: `fc86fff550ebb74756719bd3e5b9045da5b735f4`.
No merge, deployment, runtime setting change or production write is authorized
by these results. Tonight's install and the main freeze are unchanged.

| Item | Change and proof |
| --- | --- |
| N3 stale serial token | Replays the recorded CYCU client no-wire expiry, queues, re-holds/restores, and queues again with the SAME durable hold, segment and attempt but a NEW token. Delivering the old copy while phase is again `queued` submits nothing. Current copy submits exactly once. Both live-process and OMS-restart cases pass; removing only `hold.token != token` makes both RED. |
| N5 filled sibling | Projects the recorded CYCU Schwab fill into the ledger WITHOUT the in-memory report callback. Tests fill-row evidence, filled status and partially-filled status, each before/after OMS restart. All six stop Webull submission; disabling only the filled-slot branch makes all six RED. Status-only/partial recovery states are controlled variants of the recorded full fill, not claimed historical partial fills. |
| F1 window | NFQ admission now calls the same `entry_gate.within_entry_window`/`resolve_entry_window` settings as v2, intersected with the existing OMS NORMAL-session check. Wire deadline is the configured end capped at RTH close. Tests cover custom 10:05 start, 14:30 cutoff, 15:50 allowed with a 16:00 cutoff, 09:29/09:30 and 16:00 session edges. The exchange RTH boundary agrees with v2's `_resting_session_is_eh`; it is not a configurable trading cutoff. Seven new assertions were RED on the old hardcoded implementation. |
| F2 measurement | Ordinary v2 cancel/replacement, segment/fill retirement and generic broker/risk terminals use `decision=retired` and `webull_mirror_nfq_retired:`. Explicit NFQ price-wait give-ups retain `decision=gave_up` and `webull_mirror_nfq_gave_up:`. The first authoritative reason still wins; dedup and exact-generation Webull release accept both namespaces. Actual Schwab resting state remains untouched. Tests pin both paths, ordinary cancels/expiry, and current-versus-stale generations. |
| F3 query cost | SQL statement-count tests measure **zero** NFQ DB statements with no holds, also zero for a different symbol. A waiting same-symbol hold with no matching order costs **two SELECTs per tick**; one unfilled Schwab sibling costs **four**. No broker reads occur. Details and limits below. |
| Composition | Four recorded-price cases drive actual v2 mirror open/cancel/replacement emissions, NFQ holds, stale serial copies and the explicit simulator. Exactly one replacement reaches submission and produces one fill. This pins the shared reprice/hold seam, not unbuilt immediate broker-replace behavior. |

The duplicate guards did not need loosening or replacement: their existing
conditions now have isolated assertion tests rather than tests masked by an
earlier phase check or the in-memory fill callback. The shared premarket
reactive-price gate remains 2,000 ms and retains its mutation test.

### Tick-Path Cost

These are synchronous local DB reads on the tick path, not claimed free or
constant work. For each matching held symbol, `_nfq_retirement_reason` reads
the latest segment row and all buy orders for its slot (two SELECTs), then
one fill-existence SELECT per matching-segment order visited and up to one
account SELECT per unfilled order. Thus the read-only waiting path is at
most **2 + 2N SELECTs per hold**, with early exit on fill/live-buy evidence;
the account identity map can reduce the count. Matching historical orders
are not LIMIT-bounded. Transition ticks additionally persist the hold and
read/append outcomes; the quoted two/four counts are not transition totals.
Multiple matching holds sum this work. No hold means immediate return and
**zero** NFQ DB statements, pinned by test. Live PostgreSQL latency and
tick-load cost remain UNMEASURED. No background or production benchmark was
run for this change.

### Writer Ownership

The RPG1 commits are Codex work, not reviewer edits. This revision's tracked
writer is confined to `codex/nfq1-mirror-fresh-price`; the composition sidecar
owns only its new test file and the validation sidecar changes no tracked
files. Neither writes `codex/rpg1-resting-reprice-gap`. RPG1's active branch
is preserved; combined-source testing uses an immutable snapshot, not a
second writer or a merge onto that branch. Prior wording that implicated
the reviewer was incorrect.

### Combined-Source Verification

An isolated overlay applied the immutable RPG1 source delta
`b8b0dafb..29410944b43488798c3f6fcd820e1fc459718d04` onto this NFQ source.
The patch applied cleanly; all 86 loaded project modules resolved inside
the overlay. **160 tests passed**, including the four new composition cases.
The overlay, exact applied patch, import-path verification and run log are
under `ops/local/nfq-review-round1/composition-overlay/`.

This is the existing cancel/reprice/held-generation composition. RPG1's
immediate broker-replace handoff is still unbuilt at that head; this result
does not claim its latency, partial-fill recovery, or first-quote callback
behavior is proved. No RPG1 application changes were added to this PR.

### Final Local Validation

Full `tests/unit`, no exclusions, skips or xfails:

| Tree | Passed | Failed | Added / removed failed names |
| --- | ---: | ---: | --- |
| Real-Git b8 baseline from the own RPG1 lane, raw JUnit independently parsed | 4,936 | 56 | Baseline |
| Revised NFQ real Git worktree, final tested source/tests | 5,049 | 56 | **0 / 0** |

The exact sorted failed-name sets are byte-identical. The 56 are the same
44 acceptance-installer tests, nine checkout-sync tests, two other shell
installer tests and one Momentum dead-consumer timeout as the baseline.
This is not a green-full-suite claim. The baseline recorded 311 warnings;
NFQ recorded 312, including the disclosed unawaited protection coroutine.

Focused reviewer selection: **511 passed**. NFQ-specific file: **109 passed**;
composition: **4 passed**. Combined-source overlay selection: **160 passed**.
All six scripted card mutations are killed: N3 gives **2** assertion failures,
N5 gives **6**, original age/hold/live-buy/premarket controls give **4/1/2/3**.
Additional isolated controls: ignoring the configured v2 window gives **2**
assertion failures; misclassifying normal cancellation/replacement as NFQ
give-up gives **2**. Ruff and whitespace checks pass.

Harness diagnostics are retained, not silently subtracted. Independent exact-b8
and old-NFQ archives each had 58 identical failed names: an absent archive-local
Git index and a 1,047-character cron line caused two additional harness failures.
The initial revision archive had 59: my new assertion put the production call
on the expected-value side, tripping the project's meta-test. The final test
puts the pinned literal-table expectation on the RHS; the rule/allowlist was
not weakened. A real-worktree run interrupted for that correction is excluded
from completion counts and retained. The subsequent complete run is the
5,049/56 result above. No production source changed after `fc86fff5`.

Raw logs, JUnit, input hashes and exact name comparisons:
`ops/local/nfq-review-round1/`. Baseline provenance and all 56 names are in
`nfq1-round1-validation-2026-10-02.json`. GitHub `validate` must separately be
green on the final pushed head; the builder does not supply an independent pin.

## Independent Step 0

**Issue: AGREE.** Independently collected read-only `db.ndjson`,
`prices.ndjson`, `logs.ndjson`, and `assessment-status.json` under the local
`rpg1-nfq1-20261002` evidence directory establish 2,590 mirror orders and 56
NO_FRESH_QUOTE refusals: 55 missing stamps and one stamp expired at the wire.
The old shared two-second stamp gate and absence of an NFQ retry owner are
confirmed in the b8 code. PA1 alone only re-arms PRICE_AGGRESSIVE.

The independently computed refusal-minute-modulo-five split is **30/26**, not
the reviewer's 27/29. Extraction population/clock differences are not resolved;
the different split is not presented as agreement. The reviewer also supplied
56/56 nearby market observations, 55 subsequent prices within 4.4 seconds and
seven lost Webull legs; these are reviewer claims, not independently reproduced
OMS cache/latency or causal missed-fill measurements by this lane.

| Recorded case | Gateway-published quote age projected at submission | Replay result |
| --- | ---: | --- |
| AIXI 2026-10-02 12:28:03 ET | 6.509800 s | STOP_LIMIT shape allowed |
| CYCU 2026-10-02 13:58:01 ET | 8.656509 s | Unchanged ask preferred; recorded trade fallback tested |
| TNON 2026-09-30 15:00:06 ET | 7.230982 s | STOP_LIMIT shape allowed despite */5 publication lag |
| IPDN 2026-09-23 11:47:02 ET | 3.968792 s | Ask 5.39 inside 5.38/5.41 wire band becomes LIMIT |

CYCU 2026-10-02 12:00:06 ET is the recorded wire-expiry case: stamped last
3.8968 at 16:00:03.734Z, reported wire age 2305 ms. Its actual client report is
replayed as evidence of the structured no-wire refusal. The new ten-second
boundary is separately exercised by the production adapter guard at 11 seconds.

**Exact historical OMS cache age remains UNKNOWN for the 55 missing stamps.**
Gateway `produced_at`, exchange trade time and capture receipt do not prove OMS
cache consumption. Tests project recorded prices into a controlled cache; they
do not retroactively prove the observations were available to OMS. No invented
broker responses are described as recorded evidence. Successful dispatch uses
the explicitly named simulator; fault schedules and ledger races are controlled
test scaffolding. Fixtures contain actual report payloads and redact account
numbers/tags. A bounded supplementary SQL read supplied a real CYCU Schwab fill,
an IMCC structured PRICE_AGGRESSIVE response and the CYCU trade print, using
read-only transactions, ten-second timeout, nice 19 and ionice 3.

RPG1 broker replace/cancel race measurements remain outside this lane. Parent
reports a separately reconciled 205-flip cohort and 130 traded definition
(129 within close+30 seconds plus YMAT reactive). NFQ1 does not claim its own
independent replication of that complete sweep or that it proves RPG1 safe.

## Requirement Card and Named States

| Requirement/scenario | Implementation and tested safe state |
| --- | --- |
| Mirror-only 10,000 ms, enabled with future NFQ deploy | New numeric setting and default-true flag classified in both expected catalogs; shared premarket/reactive setting stays 2000 ms |
| AIXI quiet name, TNON gateway lag | Real recorded quote/report fixtures reach the production wire-shape guard under ten-second setting |
| CYCU same-instant trade, unchanged nine-second ask | Eligible ask wins; absent ask falls back to last; quote or trade evaluation queues through serial intent stream only |
| IPDN inside band | Production shape guard produces LIMIT without changing price band or sizing |
| Missing price | Durable `held` snapshot and positive v2 feedback; no broker order |
| Stamp expires at wire | Explicit client no-wire refusal returns to `held`, retaining PA1 attempt budget |
| ASK_PAST_BAND while held | One authoritative NFQ terminal; Webull latch/claim released, actual Schwab rest untouched |
| v2 cancel/reprice | Slot, segment, attempt and generation retirement invalidates queued token; stale bound cancels cannot retire replacement |
| Schwab fills while Webull held | Recorded Schwab fill retires the held mirror; no second buy |
| Segment ends or evidence missing/malformed | No resume into an unproven generation; retirement with v2 feedback |
| No price ever, 15:45 / 16:00 | Periodic expiry without a tick; final wire deadline also prevents an in-flight late entry |
| OMS restart held/queued | Positive current-session segment proof required; old queue tokens invalidated, fresh tick may queue once |
| OMS restart with existing live buy | Durable duplicate-buy ledger wins; retry retired but actual live-leg management retained |
| Lost stream acknowledgement | Token invalidated, one give-up notification; possible stream copy cannot dispatch |
| Adapter accepted, acknowledgement/ledger fails | Pre-wire committed client-order id remains addressable; durable `uncertain`, never release/replay, including restart and window end |
| Successor attempt feedback | Same generation and exact predecessor authorize retry transition; arbitrary stale attempts rejected |
| First authoritative terminal ordering | NFQ owns mirror report refusals, preserving the audit row and PA1 deferral; pre-submit risk/routing/eligibility/collision/dedup and ordinary broker terminal families carry scoped NFQ provenance on the FIRST terminal, retaining the actual reason and emitting no duplicate cleanup terminal |
| Retry clock consistency | NFQ and PA1 internal events use the OMS clock explicitly, not envelope wall time; fixed October 2 fixtures pass with envelope clock on October 1, 2 and 3 |
| PRICE_AGGRESSIVE and 429 | Existing serial retry/budget path retained; numeric PA1 attempts survive NFQ handoff; no new broker polls or rate-budget bypass |
| Observability | Sent/held/gave-up logs include cache age, ask/last/none source, timestamp, wire age/source; producer clock and unknown OMS consumption explicitly labeled |

The hold snapshot uses existing `DashboardSnapshot`; no schema migration.
NFQ owns only waiting Webull mirror work, not the actual Schwab resting order.
Confirmed live orders/fills remain under the existing ledger and management
paths. Uncertain dispatch deliberately favors an addressable hold over a new
buy until existing broker reconciliation/operator evidence resolves it.

## Initial Validation at 8bed4ea

All application runs use the approved orb-date-test-base virtual environment
interpreter, with `PYTHONPATH` explicitly set to this branch's `src`. The b8
comparison alone points to the exact-base source snapshot's `src`.

- Before application edits: initial NFQ specification tests **7 failed, 1 passed**.
- NFQ scenario file: **85 passed**.
- Focused NFQ + outcome consumer/integration + PA1: **137 passed**.
- Parent-concern tests initially reproduced three feedback failures; after the
  ownership fix, all five feedback/uncertain-acceptance tests pass permanently.
- Follow-up risk and actual recorded terminal broker rejection tests reproduced
  two further ordering failures before the family-level fix. They now pass,
  alongside 18 terminal-family/current-versus-stale-generation cases and the
  recorded PRICE_AGGRESSIVE response below and at the three-attempt cap.
- Clock-bound RED/green: removing NFQ's explicit OMS timestamp in memory makes
  all six clock-consistency cases fail; independently removing PA1's timestamp
  gives three failed / three passed. Unmodified code passes all six cases with
  the envelope date set to October 1, October 2 or October 3. Fixture dates were
  not moved. RED outputs retained in `clock-red.log` and `pa-clock-red.log`.
- Broad OMS/Webull/fanout/v2/expected-settings run: **2187 passed, 44 failed**.
  Exact b8 installer test file: **74 passed, the identical 44 failed names**.
  Sorted FAILED-name comparison has no additions/removals, exit 0. The installer
  fake sha256sum invokes absent `/usr/bin/sha256sum` on this Mac. Full failed-name
  set and baseline identity are in `nfq1-test-evidence-2026-10-02.json`.
- Broad run excluding that entire installer test file: **2113 passed,
  118 deselected, 1 warning**. This exclusion is not a full-suite-green claim.
- The broad run emitted one unawaited `_attach_webull_protection` coroutine
  warning. No claim is made that the warning was baseline-compared or fixed.
- Required in-memory mutations: back-to-2s **KILLED (4 assertions)**;
  remove-hold **KILLED (1)**; remove-duplicate-check **KILLED (2, including
  restart)**; premarket-age-changed **KILLED (3)**. No mutant changed disk code.
- Ruff on all changed Python files and `git diff --check`: passed.

Reproduction from the NFQ worktree (set `PYTHONPATH=$PWD/src` first):

```sh
PY=/Users/velkris/.codex/worktrees/orb-date-test-base/project-mai-tai/.venv/bin/python
export PYTHONPATH="$PWD/src"
"$PY" -m pytest tests/unit/test_nfq1_mirror_fresh_price.py tests/unit/test_fanout_outcome_consumer.py tests/unit/test_fanout_outcome_integration.py tests/unit/test_oms_webull_mirror_deferred_resubmit.py -q
"$PY" scripts/check_nfq1_mutations.py
"$PY" -m pytest tests/unit/test_oms*.py tests/unit/test_webull*.py tests/unit/test_fanout*.py tests/unit/test_schwab_1m_v2*.py tests/unit/test_v2*.py tests/unit/test_nfq1_mirror_fresh_price.py tests/unit/test_expected_flags_check.py -q
```

Raw local run logs/XML and exact-b8 archive are retained under ignored
`ops/local/nfq-validation/` in this worktree. No other worktree was used or
modified for the baseline comparison.

## Limits and Unexercised Risks

- No live broker submission, replace, cancel or install was exercised. Synthetic
  acknowledgement loss and SQLite rollback/restart tests are not real venue
  latency or PostgreSQL crash-durability measurements.
- Historical missing-stamp cache ages and the first live week's sent/held/give-up
  age distribution remain unmeasured; the new logs provide future evidence.
- Held-symbol checks perform synchronous local DB work on tick/control paths;
  the exact query pattern and unbounded historical-order cardinality are
  disclosed above. Live load, DB-failure duration and dispatch latency have
  not been measured.
- Uncertain broker acceptance must not be auto-retried. The durable client id
  allows existing reconciliation/cancellation to address it; recovery timing
  and actual broker read outcomes remain unexercised here.
- The 44 platform-dependent installer failures remain failures, and the broader
  coroutine warning remains disclosed. No deployment approval is implied.

No band, sizing, exits, gateway or cron change. Main and production remain
untouched; merge/install require separate explicit authorization.

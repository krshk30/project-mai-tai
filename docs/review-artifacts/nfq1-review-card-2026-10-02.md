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

## Validation

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
- Held-symbol checks perform bounded local DB work on tick/control paths; live
  load, DB-failure duration and dispatch latency have not been measured.
- Uncertain broker acceptance must not be auto-retried. The durable client id
  allows existing reconciliation/cancellation to address it; recovery timing
  and actual broker read outcomes remain unexercised here.
- The 44 platform-dependent installer failures remain failures, and the broader
  coroutine warning remains disclosed. No deployment approval is implied.

No band, sizing, exits, gateway or cron change. Main and production remain
untouched; merge/install require separate explicit authorization.

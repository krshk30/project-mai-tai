# RPG1 piece 3: fourteen-scenario review

Historical follow-up at `6f3f000c`. Current rebased review fixes, configured-window
semantics, operator-final card and new admission switch are in
[`REBASE_REVIEW.md`](REBASE_REVIEW.md). Its verification supersedes the overlay
results and pre-reconfirmation status below.

## Step 0, before further changes

Starting RPG head: `d38a86a4ff68273af15f27533d1236d0e1dede93`.
New integration baseline: `250ab18458f4d806aa8bcdf98d787fff5bb57df4`.
The new attachment's `352da22a` skeleton description is historical: production
runtime wiring already exists at d38. Its Validate CI completed successfully;
the independent review pin is absent and this remains a draft, not a deploy pin.

- **AGREE:** the original stable-rest block cancels and returns. At d38 it now
  hands off through `atr_reprice` metadata, rather than allowing the next bar to
  place blindly. `_queue_resting_place` and the reactive entry path both defer
  to durable ownership during the gap.
- **AGREE:** exact parent identity plus terminal cancellation and explicit
  cumulative zero is sufficient to permit a new BUY through current gates.
  The cancel ACK is not proof. One-second read spacing, two-second timeout,
  thirty-read cap; unknown retains ownership rather than weakening this rule.
- **AGREE:** `fanout_slot_id`, segment, first/reclaim slot and ordinal must survive
  a reprice, while the new client/attempt and resting generation must differ.
  Production authorization uses existing trigger/limit/sizing helpers and current
  owner/RETRY-ONE/floor/window checks, not a saved quote or new pricing formula.
- **UNMEASURED:** recorded full cancel -> strict read -> new acceptance latency
  on either broker. Webull's historical exact GET samples are 148.514 ms (AIXI)
  and 125.064 ms (AMOD full fill); neither includes a new cancellation/placement.
  Schwab's retained body has second-resolution close time, not GET timing.
- **QUALIFICATION to scenario 9:** the normal deterministic AMOD replay can have
  its replacement before the 13:57 BUY flip. If the cancel remains unknown until
  that flip, no implementation can promise both immediate placement and no
  duplicate without more evidence. Existing current-first gates must expire
  rather than buy after the opportunity ends. This is not a newly permitted path.

No duplicate-safety stop is required for the selected conservative design.
Seconds-level live success is not established by a simulator. The operator's
partial-fill/no-remainder card reconfirmation remains required before pin.

## Recorded evidence boundary

Own retained `db.ndjson/amod_events` contains the exact Schwab AMOD old parent
`1008159036751`, closed `2026-10-02T17:56:03+0000`, explicit filled quantity zero.
The full nested raw body can be redacted and replayed without changing its order
identity, side, quantity, status, fill field or child shapes. The earlier fixture
`1008159036414` is the 13:50 cancellation and must not be called the 13:56 answer.

The same evidence contains Webull's 13:56:03.825140 cancel ACK and its later
13:56:16.286793 locally observed cancelled event: 12.461653 seconds apart. That
is a polling-observation interval, not a direct-confirmation latency. The raw
Webull AMOD 13:56 order-detail body is not retained in this capture. It must not
be reconstructed as a broker answer. The recorded exact AIXI zero-fill detail
and later AMOD full-fill detail remain the Webull strict-read fixtures.

Synthetic variants are limited to unavailable working/error/partial/second-order
races, replacement acceptances and controlled state/clock/quote delivery. Each
is labelled. No production evidence collection or broker write is needed here.

## Fourteen scenarios

All accepted replacement reports and live-book states below are explicit
simulator results, never invented historical broker answers. Real parent
identities/bodies anchor strict reads. `runtime` denotes
`tests/unit/test_rpg1_runtime.py`; `piece3` denotes `test_rpg1_piece3_14.py`.

| Scenario | Evidence and named ending |
| --- | --- |
| 1 Normal, both | `runtime::test_recorded_cancel_clear_uses_v2_callback_and_serial_lane_without_next_bar`: recorded zero, actual OMS/bot/serial lane, exactly one acceptance; `placed`. `piece3` simulated book asserts no overlapping BUY at each wire, not only at the end. |
| 2 Full fill | Recorded Webull AMOD full detail; original-OPEN accounting, managed position, v2 consumes slot; `filled`, no second BUY. Added explicitly controlled raw full-fill variants through both strict decoders and the real runtime. |
| 3 Partial | Controlled raw 31/197 Schwab and 17/98 Webull body variants, original accounting exactly once, actual-size position, no remainder; `filled`. Child scaling is not claimed; synthetic Schwab child answers are deliberately omitted. |
| 4 Working | Both brokers' controlled PENDING_CANCEL variants, five one-second reads then the unchanged recorded terminal-zero body; one old BUY until clearance, one replacement afterward; `waiting -> placed`. |
| 5 Unknown | Real dedicated adapters with explicitly controlled HTTP503/exception/wrong-parent/absent-fill responses for both brokers; thirty reads, one cancel, no BUY. Durable one-time notice survives duplicate delivery/OMS restart. Fresh v2 feedback owns the blocked slot; `held_unknown`. |
| 6 Refused | Retained Webull STOP_PRICE_MUST_BE_GREAT_THAN_MARKET and PRICE_AGGRESSIVE text projections, Schwab retained stop/ask excerpt; ordinary refusal/retry paths; `refused` or bounded `price_wait`. No claim these text projections are full raw venue answers. |
| 7 NFQ | `test_rpg1_runtime_nfq.py` plus migrated four-case composition: recorded cancel/read, NFQ price hold, old queued tokens inert, exactly one current-authorized replacement; `price_wait -> placed`. |
| 8 Independent | Runtime tests prove Webull moves with an unproven Schwab leg, and the inverse, while the healthy leg can reprice again. Separate real cached-ineligibility test refuses a Schwab serial replacement before wire. No invented cancellation proof for a missing Schwab parent. |
| 9 AMOD flip | Exact 13:56 Schwab body now replaces the older 13:50 fixture in the recorded 13:56 probe replay; normal simulated replacement precedes 13:57. Explicit delayed-read variant reaches current BUY flip and ends `expired`, not an unsafe late BUY. See Step-0 qualification. |
| 10 Cutoff | Current callback and final wire guard cover 15:45, 16:00 and processing delay across cutoff; `expired`/`refused`, no new BUY. Configured entry-gate checks remain. |
| 11 Floor | Current thin-bar check at replacement despite earlier eligibility; `expired`, no new BUY. |
| 12 Restart | Persisted old identity/reads/clear/claim; repeated cancel envelope never retargets or recancels. Empty v2 watch state waits. Committed accepted/fill/terminal accounting restores actual state, uncertain pending submission never blindly repeats; bounded safe `clear`/`placed`/`filled`/`submit_unknown`. |
| 13 Stale quote | Both brokers' actual bot quote callback resumes first-entry reprice from a >10s stale quote, without another bar; exactly one `placed`. Reclaim behavior remains unchanged under the approved first-entry-only scope. |
| 14 Two reprices | Both brokers, two moves inside two simulated seconds: either first replacement never wires and latest price wins, or it is strictly cancelled before the second. One live simulated BUY at every wire, same economic slot, distinct attempt IDs; latest `placed`. Successor read identity/quantity is explicitly synthetic because no recording of that future order exists. |

## Changes after d38

- Claim `blocked_notice_at` durably before the exhausted/unknown terminal log.
  Replays/restarts cannot repeat it. A process crash after claim but before log
  delivery can lose that log; durable ownership and v2 feedback still survive.
- Add per-account/symbol/slot/segment local timing fields to the existing RPG
  progress line: cancel-to-read, cancel-to-submit, cancel-to-report. Missing or
  backward-clock timing is `-1`, never falsely zero. These are local observations,
  not exchange timestamps or proof of native acceptance/child behavior.
- Add the exact redacted AMOD 13:56 fixture with source-file SHA-256, preserve
  Webull's ACK/poll envelopes without promoting them into strict-zero evidence,
  and replace misleading historical status headers with links to current evidence.
- No price, size, 0.5% threshold/band, exit, shared quote-age, gateway or timer
  setting change. Two-reprice behavior was already correct; its new test closes
  coverage, not a previously proven production defect.

## Work and evidence

All scratch logs are under `ops/local/rpg14-review/` in the RPG worktree.
Untouched current-main baseline: **5,050 passed, 56 failed**, 312 warnings,
202.38 seconds, `base250-unit.txt` / `base250-unit.xml`.

The isolated checkout is
`/Users/velkris/.codex/worktrees/rpg14-current-base/project-mai-tai`, detached at
250ab184. Only the previously reviewed combined source delta plus this follow-up
is applied; its 120-snapshot setting and NFQ source are retained. No branch
switch, rebase, main merge, production write or COLDSTART1 worktree access.

The initial combined full run omitted RPG's already-committed SLOT2 fixture
initializer and therefore had three extra fixture errors. That test delta is
now included; no production fallback/exemption was added. Final results below
supersede that incomplete overlay run.

Final verification, approved Python with `PYTHONPATH=src`, bytecode/cache writes
disabled:

- RPG branch full `tests/unit`: **5,160 passed, 56 failed, 2 skipped**, 312
  warnings, 212.32 seconds (`rpg-final-unit.txt/xml`). Both skips are NFQ-only.
- Current-main combined full `tests/unit`: **5,278 passed, 56 failed**, 312
  warnings, 223.83 seconds (`combined250-final-unit.txt/xml`). Exact JUnit
  failed-name sets for both runs equal the untouched 250 baseline: **zero added
  and zero removed failures**. This is not an all-green full suite.
- Final combined focused regression set: **619 passed**, 19.51 seconds
  (`combined250-focused.txt/xml`). It includes production RPG/NFQ composition,
  all RPG tests, inert-module enforcement, resting/orphan handling, dual fanout,
  sizing, deferred paths, backtest replay, refresh, reclaim, extended-hours,
  Webull mirror and SLOT2 fixture regressions.
- Added fourteen-scenario file: **20 passed**, 2.25 seconds. Earlier focused RPG
  pass was **226 passed**, before adding the four raw full/partial fill variants.
- Verified RED before the fix: **8 failed / 8 passed**, all eight unknown-cap
  variants reported repeated terminal notices. Fixture-only initial failures are
  not counted as production-defect proof (`red-scenarios-verified.txt`).
- **33 semantic mutations RED**: five new piece-3, nine runtime, eight strict
  readback, eight coordinator and three first-entry quote-wait mutations.
  Each child run failed test assertions, not an import or mutation-anchor check.
- Scoped Ruff and whitespace checks pass. Background protection-task warnings
  and pre-existing full-suite failures remain; no exemptions were introduced.

Reproduce the new focused proof with the approved Python and
`-B -m pytest -q -p no:cacheprovider tests/unit/test_rpg1_piece3_14.py`; execute
`docs/review-artifacts/rpg1/check_piece3_mutations.py` with the same interpreter
for its five independent in-memory mutations. Full comparisons use
`-B -m pytest -q -p no:cacheprovider tests/unit --junitxml=<scratch-output>` in
each indicated checkout. No broker endpoint is contacted by these tests.

Retained artifact SHA-256 values:

| Artifact under `ops/local/rpg14-review/` | SHA-256 |
| --- | --- |
| `rpg-final-unit.xml` | `ee4c2816097121b44901d5dff159237efb7d7f631afe88140cd52fc8489dad77` |
| `combined250-final-unit.xml` | `516985e95a492d0c98ebf3fffd70e14401734d34eddf551e4ca8126c49a8ed6e` |
| `combined-failed-name-comparison.json` | `c26c6e89467662d624bd1345df394a8ad94c7fb37e6528fe5f3d3b22ee604ac5` |
| `combined250-source.patch` | `8211e47d1798c14a104068b60f150da7fd700c7e01a44e33768ea3e590f9b7f1` |

The source patch is the tested current-base integration artifact, not permission
to apply it to an owned branch. `combined250-source-and-tests.patch` additionally
retains the exact isolated test overlay, including the existing SLOT2 fixture
initializer. GitHub conflicts and the missing independent review pin remain
separate integration/review blockers; no rebase or self-pin was performed.

Follow-up changed-file inventory (nine files):

- `src/project_mai_tai/oms/atr_reprice_runtime.py`
- `tests/unit/test_rpg1_buy_readback.py`
- `tests/unit/test_rpg1_runtime.py`
- `tests/unit/test_rpg1_piece3_14.py`
- `tests/fixtures/rpg1_amod_1356_recorded.json`
- `docs/review-artifacts/rpg1/check_piece3_mutations.py`
- `docs/review-artifacts/rpg1/PIECE3_14_SCENARIOS.md`
- `docs/review-artifacts/rpg1/STEP_0.md`
- `docs/review-artifacts/rpg1/RUNTIME_HANDOFF_REVIEW.md`

No new live evidence, native child scaling proof, unconditional seconds-level
guarantee, COLDSTART1 integration, merge or deployment is claimed.

# RPG1 immediate reprice: coordinator staged, runtime handoff incomplete

This is a draft implementation boundary, not a pin request. Dedicated BUY
readbacks and a durable, per-leg coordinator are built and tested. They are
**not yet connected to OMS cancellation dispatch or v2's immediate re-place callback**.
The existing minute-gap cancellation path therefore remains unchanged. No
broker write or live cancel/replace latency experiment was performed.

## Piece 3 progress after review of 29410944

`oms/atr_reprice_handoff.py` now implements the coordinator, with a
`DashboardSnapshot` journal rather than a new database table. The router forwards
the strict read to the account's adapter; an unsupported adapter returns UNKNOWN.

- Persist the exact old order, slot and segment before requesting cancellation.
  Cancel acknowledgements are never readback evidence.
- Read immediately in the same scheduled turn. Subsequent reads are spaced at
  least one second apart, with a two-second read timeout and thirty reads total.
  Exhaustion retains ownership as `held_unknown`; it does not authorize a BUY.
- Only terminal cancel plus explicit cumulative zero reaches `clear`. Current
  strategy authorization is a separate callback; stale price can wait and
  expiry can end the candidate without another BUY.
- Each leg has its own durable identity. A waiting Schwab leg does not stall a
  cleared Webull leg, and vice versa. No native replace or place-first.
- Positive fills persist `no_rebuy` before the accounting callback. The callback
  receives cumulative quantity on the original BUY identity, not on the cancel
  intent. Missing price or failed accounting retains the hold. A later zero
  observation cannot erase prior positive fill evidence.
- Claim `submitting` durably before calling the serial-lane submission callback.
  Process restart in that phase, or an uncertain submission response, never
  resubmits blindly. The journal records refusals separately from working orders.

The 36 coordinator tests include the recorded Schwab and Webull terminal-zero
responses, recorded Webull full fill, synthetic dollar-size partials, bounded
unknown/working reads, two-controller duplicate suppression, crash recovery,
scope checks and expiry. Two tests route the synthetic 31/197 Schwab and 17/98
Webull partials through the **real OMS `_record_order_reports` path** twice:
one fill row, one open managed position of the actual filled size, exit-management
membership, and no replacement BUY. This proves the callback contract in the
harness, not production callback wiring or broker child-order scaling.

Eight coordinator mutations are RED via `check_handoff_mutations.py`: unknown
authorizes replacement, retry spacing removed, read budget removed, ignored
partial fill, forgotten cumulative fill, ignored expiry, removed scope guard,
and restart resubmission.

**Still required before piece 3 is complete:** wire the OMS dispatch/retry and
v2 durable feedback/serial replacement path; prove current first/reclaim gates,
old-claim release and restart recovery through those actual entry points; replay
AMOD 13:56 and all V3 scenarios there. The coordinator's injected eligibility
decisions are NOT a substitute for those strategy tests. It remains deliberately
unconnected rather than enabling a partially verified order lifecycle. The
RPG1 x NFQ1 cancel/readback/re-place composition is likewise still unproven.

Final full unit run for this stage: **5,079 passed, 57 failed**, 312 warnings,
211.69 s. Compared with exact b8's 56 failed names, there is **one new failure**:
`test_no_inert_modules::test_no_new_module_is_imported_by_nothing`. It correctly
detects that the new coordinator is not yet connected to a production caller.
That check has not been removed, allowlisted, or bypassed with an unused import.
This head is NOT full-suite-equivalent to main and is not ready to pin. The
warning list also includes an unawaited Webull protection coroutine; its origin
has not been independently isolated, so it is not silently called baseline.
Raw run: `handoff-final-unit.txt` and `handoff-final-unit.xml` under the local
evidence root below. The 36 coordinator tests pass separately.

## Step 0(b) decision per broker

Native replacement is **not selected** for either ATR leg: retained evidence
does not prove the filled/part-filled race for these exact order shapes.
Place-first is rejected because it can leave two live buys. The working design
choice for both legs is cancel, immediately read the exact old order, and place
only after terminal cancellation **and explicitly zero cumulative fills**.
An acknowledgement, not-found result, absent fill field, or later status-poll
stamp alone does not prove zero fills. Full/partial fills must be reconciled and
must not result in a second full-sized buy. Unreadable state retains ownership
and blocks replacement, rather than freeing the slot.

| Broker | Current surface | Decision / blocker |
| --- | --- | --- |
| Schwab | `_cancel_order` already DELETEs then GETs the old parent. ORB has a separate, restricted `replace_bracket_order`. | Dedicated ATR cancel/readback path, not ORB replacement reuse. Current shared decoder incorrectly counts a real cancellation execution as filled quantity; it cannot supply the proposed zero-fill proof unchanged. |
| Webull | `_cancel_blocking` reports accepted/requested. Generic order polling reads cumulative fills; `_confirm_cancel_blocking` is an exit-release helper. | Dedicated BUY readback after cancel, not exit helper reuse. Exit confirmation maps a fill to `intent_type=close`, omits cumulative quantity on partial and terminal-cancel reports, and accepts absence. Those semantics are unsafe for authorizing a new BUY. |

The operator accepted this design after the earlier assessment. It is still
**not a measured seconds-level guarantee**. The existing 11-second Webull
stamps reflect our later polling. Recorded partial-fill/cancel race sequences
and cancel-to-new-acceptance timing are missing. Controlled variants are clearly
labelled synthetic; they are not replacements for those required recordings.

## New readback implementation

`atr_buy_readback.py` provides typed cumulative evidence, not a generic
`ExecutionReport`. `cumulative_filled=None` means UNKNOWN, never flat. Both
adapters expose `read_atr_resting_buy_after_cancel`: one fresh, exact-parent GET,
no replacement, no submit, no cancel, no cached today-orders fallback.

- Explicit zero plus terminal CANCELED/CANCELLED and matching parent identity,
  symbol, BUY side and original quantity is the only `can_replace=True` result.
- Missing body, absent fill field, invalid/nonfinite quantity, wrong identity,
  unknown account, acknowledgement, HTTP error and not-found all block.
- Schwab `filledQuantity=0.0` remains zero. CANCELED activities are ignored;
  contradictory positive FILL activity blocks. Only parent executions are read.
- Any positive cumulative fill, including terminal-cancel with a partial fill,
  returns `fills` and never permits re-buying either the full size or remainder.
  A missing execution price is retained as missing, never fabricated.
- This is cumulative evidence, **not a fill delta**. The future handoff must
  reconcile it through the OMS's existing deduplicated fill/position/exit path
  before deciding the lifecycle is complete. Partial fill protection through
  that new handoff is not yet proven.

The generic Schwab `_execution_report_from_order`, `_extract_filled_quantity`,
`_cancel_order`, `fetch_order_update`, and Webull `_confirm_cancel_blocking`,
`_cancel_order`, `_fetch_order_blocking` have identical ASTs to 4ed8dd58. No
existing cancelled-report consumer is rerouted by this change.

Fresh read-only database harm check, October 2 17:15:02 ET, submitted since
September 8, strategy `schwab_1m_v2`, side BUY, status cancelled:

| Account | Cancelled orders | Orders with booked fills |
| --- | ---: | ---: |
| live:schwab_1m_v2 | 630 | 0 |
| live:orb | 630 | 0 |

Both accounts returned zero persisted `partially_filled` events in that
separately queried event-time window. That is absence of recorded events, not
proof a venue never partially filled. Reproduce with
`collect_buy_readbacks.py --counts-only` on the box as root, read-only SQL.

## Real recordings and timing

`tests/fixtures/rpg1_schwab_cancelled_buy.json` is the full recorded AMOD parent
body below, with accountNumber/tag redacted. The Webull fixture contains two
fresh historical order-detail GETs, not new orders:

| Input | Read interval UTC | HTTP | Observation | GET wall time |
| --- | --- | ---: | --- | ---: |
| AIXI cancelled BUY | 21:03:27.609900-21:03:27.758419 | 200 | CANCELLED, filled_qty="0" | 148.514 ms |
| AMOD filled BUY | 21:03:29.859334-21:03:29.984399 | 200 | FILLED, filled_qty="1", filled_price="3.41" | 125.064 ms |

**Cancel -> readback -> replacement accepted latency: UNMEASURED for both
brokers.** These are GET-only observations of already terminal orders. There
was no cancel or new placement in this capture; test playback elapsed time
would not establish wire latency. No seconds-level promise is made from it.

| Fixture | SHA256 |
| --- | --- |
| rpg1_schwab_cancelled_buy.json | faa03a5fc8590b0858a700af9074a1bafc931dea2eed1da30defb86f0a626fda |
| rpg1_webull_buy_readbacks.json | 1400f84a897deb45f966bd0ae10e84df80277615d703424439c9edd66c4bff06 |
| Local buy-readbacks-20261002.json | f764e519705725fa0590113a8ad4801647e265b5caff0fa4705ca3e3df3bd791 |

## Tests and remaining blockers

73 dedicated readback cases pass. Real zero/full response bodies replay through
the strict decoder and fresh-GET adapter paths. Synthetic fault/partial variants
test 197-share Schwab with 31 filled and 98-share Webull with 17 filled, both
working and cancelled; every variant blocks replacement. These are dollar-size
policy tests, **not recorded partial-fill proof or an exit-attachment test**.
The RED-before-build run failed because the dedicated module did not exist.
Eight subsequent semantic mutations are all RED via
`check_readback_mutations.py` (zero fallback, CANCELED activity, missing fill
field per broker, ignored partial, ambiguous Webull body, wrong Schwab parent,
and contradictory executions).

The 410-case adapter/OMS/exit/quote regression selection passed before adding
the final SDK request-addressing test; that final test also passes. Final full
unit suite: **5,044 passed, 56 failed**, 311 warnings, 198.18 s. The exact
failed-name set equals the isolated Git base b8b0dafb comparison (4,936 passed,
56 failed); no added or resolved failed names. Evidence:
`/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/readback-final-unit.txt`
and `readback-final-unit.xml`; baseline is `first-quote-review-20261002/base-final.xml`.
The known host tooling failures and reproduced Momentum timeout are not hidden
or called a green full suite. Ruff and diff checks pass.

The old readback-only results above describe 29410944. The new coordinator adds
durable per-leg ownership and callback-contract tests, but the runtime integration
listed at the top remains incomplete. V3 scenarios 1-11, recorded partial race
responses and measured full-operation latency remain owed. Do not enable an
unproven replacement flow on the strength of standalone coordinator tests.

## New independently reproduced Schwab decoder finding

Input: retained AMOD cancellation, event `2026-10-02 17:50:05+00:00`, from
`db.ndjson` / `kind=amod_events` under the local evidence root below. The real
response has `status=CANCELED`, `filledQuantity=0.0`, and an
`orderActivityCollection` entry with `executionType=CANCELED`, quantity 2.
Passing it through the unchanged adapter yields:

```text
raw_filledQuantity=0.0
activity_executionTypes=['CANCELED']
decoded_event_type=cancelled
decoded_filled_quantity=2.0
decoded_fill_price=0.0
```

`_execution_report_from_order` uses `explicit_value or fallback`; Decimal zero
falls through to `_extract_filled_quantity`, which sums execution-leg quantities
without filtering cancellation activities. This does not prove that the OMS
booked a buy fill: the report is still `cancelled`, and the OMS consumers were
not changed here. It proves that this report's quantity cannot be reused as a
trustworthy reprice-clearance signal.

Raw evidence root:
`/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/`.
`db.ndjson` SHA256:
`e564c6c532e88200f2f11de973a993a50531a31ed46e3c735f09220064a489c1`.
Code read at fa194cbe: `broker_adapters/schwab.py` 1020, 1591, 1712, 1822;
`broker_adapters/webull.py` 893, 1060, 1200. No account identifiers or raw
credentials are copied into this report.

## Composition boundary

Initial combined verification used NFQ1 PR #1086 at
`8bed4ea193d500cc5b8bcf6b4d09543e361b3e30`. Its branch was not modified here.
Its 137 focused tests were independently rerun successfully. An isolated
temporary clone combines that exact diff with RPG1 4ed8dd58; no branch merge.
The new readback changes were then applied to that clone too: the combined
readback, RPG quote-wait, NFQ1 and composition selection is **197 passed in
4.62 s**. Reproduce by applying the exact NFQ1 diff from b8b0dafb onto a
temporary checkout of this RPG1 revision, then running these four paths:
`tests/unit/test_rpg1_buy_readback.py`, `tests/unit/test_rpg1_pending_first_quote.py`,
`tests/unit/test_nfq1_mirror_fresh_price.py`, `tests/composition/test_rpg1_nfq1.py`.
The composition module is now named for normal pytest discovery. On RPG1 alone,
it skips explicitly at collection if `oms.mirror_fresh_price` is absent. In the
combined tree, that module exists and all four cases must run, not skip:

1. Recorded AMOD stale-hold inputs -> fresh Schwab quote -> first-slot drafts
   -> no OMS price -> NFQ1 hold -> fresh OMS quote -> one serial submission.
   Repeated quote callbacks and repeated delivery do not duplicate the BUY.
2. The same composition with a simulated fill consumes the resting latch as
   designed; it is not reported as a still-working order.
3. NFQ1 window expiry informs v2 and releases only the Webull claim, preserving
   the Schwab resting state.
4. A generation-bound RPG reprice cancel invalidates an already queued NFQ1
   retry before it can submit.

The initial production quote-wait and OMS hold paths compose in these tests;
the adapter is explicitly simulated, and the primary leg is checked as an
emitted draft, not a broker acceptance. This does **not** claim end-to-end
cancel/readback/re-placement or partial-fill protection. That remaining
composition test must be added when the actual RPG handoff exists.

### Updated NFQ1 head and collected composition

During this turn, the independent NFQ1 lane published
`b20225fe67421bb125884b1216decdcb8d51f9ba`. Its PR body now answers F1 (configured
entry window), F2 (ordinary retirement versus NFQ give-up) and F3 (query costs:
zero without matching holds; two/four SELECTs in the named waiting examples;
up to 2 + 2N per hold, unbounded historical order count). This lane did not edit
that branch or contact its owner. GitHub showed both Validate checks and the
latest independent-review-pin check successful at that exact head.

Own read-only rerun of its in-memory mutations: N3 RED with two assertions, N5
RED with six; all six scripted NFQ mutations RED. The revised source was applied
only to the existing isolated composition clone. The collected test now pins
15:45 explicitly in the service fixture, rather than assuming NFQ's old hardcoded
cutoff. The combined selection is **261 passed, zero skipped**, including both
composition files, the coordinator, both readbacks, and the quote-wait tests.

Exact tested staged tree: `8bf0ec9158c63945f70bbb7da8a7f74e638ebe4a`, in
`/tmp/rpg1-nfq1-compose.tmyphc/repo`. It combines the RPG1 application/test changes
in this commit with NFQ1 b20225fe; the earlier report snapshot in that temporary
tree is not this final report. The standalone RPG1 run collects the new pytest
module and cleanly skips it because NFQ1's module is absent. This is the requested
explicit cross-branch skip, not a claim that the composition ran on RPG1 alone.

No main merge before the approved installation finishes. This review does not
infer installation completion or authorize a deployment.

# RPG1 immediate reprice: dedicated readbacks built, handoff incomplete

This is a draft implementation boundary, not a pin request. Dedicated BUY
readback methods and strict decoders are now built and tested. They are **not yet
connected to OMS cancellation dispatch or v2's immediate re-place callback**.
The existing minute-gap cancellation path therefore remains unchanged. No
broker write or live cancel/replace latency experiment was performed.

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

Still not complete: durable per-leg old-order ownership through cancellation;
the immediate serial-lane re-place and v2 feedback; partial-fill reconciliation
and protection through that handoff; V3 scenarios 1-11; recorded partial race
responses and measured full-operation latency. Do not enable an unproven
replacement flow on the strength of standalone readback tests.

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

NFQ1 is published in PR #1086 at
`8bed4ea193d500cc5b8bcf6b4d09543e361b3e30`. Its branch was not modified here.
Its 137 focused tests were independently rerun successfully. An isolated
temporary clone combines that exact diff with RPG1 4ed8dd58; no branch merge.
The new readback changes were then applied to that clone too: the combined
readback, RPG quote-wait, NFQ1 and composition selection is **197 passed in
4.62 s**. Reproduce by applying the exact NFQ1 diff from b8b0dafb onto a
temporary checkout of this RPG1 revision, then running these four paths:
`tests/unit/test_rpg1_buy_readback.py`, `tests/unit/test_rpg1_pending_first_quote.py`,
`tests/unit/test_nfq1_mirror_fresh_price.py`, `tests/composition/rpg1_nfq1.py`.
`tests/composition/rpg1_nfq1.py` is run explicitly in that combined tree (it is
not a silently skipped standalone test). Four cases pass:

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

No main merge before the approved installation finishes. This review does not
infer installation completion or authorize a deployment.

# RPG1 immediate reprice: broker integration assessment started

This is a code/evidence assessment, not a completed execution implementation or
a pin request. No broker write or live latency experiment was performed. The
first-entry quote wait is built separately; the minute-gap cancellation path
still needs its broker-confirmed handoff and V3 scenarios 1-11.

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

This choice is **conditional, not a measured seconds-level guarantee**. The
existing 11-second Webull stamps reflect our later polling. Recorded direct
readback responses and timing for the required partial-fill race are still
missing. Runtime integration is stopped at that safety/evidence boundary; do
not silently substitute hand-written broker answers for the required real ones.

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

NFQ1's last published branch head at this assessment was
`b8b0dafbdf583ca4af9eddc8dbf922cbf887a482`. Its local worktree contains uncommitted
changes owned by the other lane. They were not altered, committed, or merged
here. There is not yet a stable published NFQ1 implementation to bind the
end-to-end composition test to. That test remains owed; the existing quote-wait
test proves two emitted intents with a shared slot/segment, not two accepted
broker orders or NFQ1 give-up behavior.

No main merge before the approved installation finishes. This review does not
infer installation completion or authorize a deployment.

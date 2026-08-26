# §82 — one Webull fan-out lifecycle: identity, attempts, outcomes, and slot policy

**Status:** DESIGN ONLY. No runtime behavior changes are authorized by this document.

**Written:** 2026-08-26, after #790 made new fan-out legs attributable but before the strategy
could consume a Webull outcome.

## The decision in one sentence

A Webull fan-out trade is one durable **segment**, containing at most one **resting slot** and one
**reclaim slot**; each slot can create many broker **attempts**, but only an explicit OMS outcome may
release or consume that slot. Venue-side reconciliation remains an undecided dependency for outcomes
our process did not observe.

This is one lifecycle, not five independent proposals:

```text
segment identity
    -> economic slot (resting or reclaim)
        -> attempt 1 -> queued -> dropped
        -> attempt 2 -> submitted -> working -> cancelled
        -> attempt 3 -> submitted -> partially_filled -> filled
            -> slot consumed for the rest of the segment
```

## 1. The three identities

The current code has identities for different layers, but no durable key shared by all of them:

- `fanout_segment_id` groups newly emitted legs, but is only in strategy state until a draft is
  persisted and does not say which economic slot the leg represents.
- `TradeIntent.id` / event id identifies one handoff to the OMS.
- `client_order_id` identifies one submit or replacement attempt.
- `broker_order_id` identifies what Webull acknowledged.

The lifecycle needs all three levels, deliberately separate:

| identity | cardinality | meaning |
|---|---:|---|
| `fanout_segment_id` | one per symbol/ATR segment | durable grouping boundary, minted before an ARM is required |
| `fanout_slot` + `fanout_slot_id` | up to two per segment | the economic entitlement: `resting` or `reclaim` |
| `fanout_attempt_id` | many per slot | one place, reprice, retry, or replacement; maps to the existing intent/client-order identity |

`fanout_slot_id` is derived from `(strategy, symbol, fanout_segment_id, fanout_slot)`. It is stable
across every reprice, retry, cancel, and broker replacement for that slot. `fanout_attempt_id` must
change for every new broker action. `cw_entry_n` is retained as a label and is **not** an identity;
the resting-path under-count and historical zero segment ids already prove it cannot carry that job.

The existing source mapping is explicit:

| fan-out source | slot |
|---|---|
| `rth_resting`, `rth_resting_mirror`, `eh_resting` | `resting` |
| `reactive` | `reclaim` |

The mapping is the composition rule already recorded for v2: one resting entry and one reclaim.
A reactive entry does not silently substitute for a resting slot that never filled.

“Durable” has a strict restart meaning here. Once an id reaches the OMS, every local record keeps it
immutably. If the strategy restarts while the economic segment may still be live, it must either
rehydrate the same id from a durable local lifecycle record or hold the segment as `could_not_tell`;
it must not mint a fresh id and thereby manufacture a fresh entitlement. The current in-memory
`fanout_segment_id` does not yet prove that restart continuity. A deterministic pre-ARM anchor or
explicit local rehydration is required before outcome-driven suppression can ship.

### Known cause this addresses

- The eight unattributable 2026-08-25 legs and the earlier `cw_arm_bar_ts=0` resting population.
- Attempts in one economic slot currently look like independent orders, so order counts are mistaken
  for opportunity counts.
- A later outcome cannot be routed back to the exact state claim that produced it.

### It does not address

- Whether Webull accepted an order whose response was lost.
- Historical records that never carried a segment or slot identity.
- Whether a Webull exit child filled; this document covers entry-slot ownership, not exit attribution.

### What it cannot know

An identity proves that records belong together. It does not prove that the records are complete or
that their stated broker status is true.

### What would falsify it

- Two retries/reprices for one economic entitlement receive different slot ids.
- A real new ATR segment reuses the prior segment id.
- A resting and reclaim opportunity receive the same slot id.
- The same broker attempt is persisted under two attempt ids.

## 2. Why “about 81 orders” can produce “about 2 fills”

The number is not 81 independent trading decisions. The resting loop repeatedly places, cancels,
reprices, retries, and sometimes gets rejected while waiting for one price cross. Each operation is
counted at a different layer even though it belongs to the same economic slot.

A read-only production query on 2026-08-26 reproduced the shape for TNON on 2026-08-19:

- `trade_intents`: **82 open intents** = 79 rejected, 2 filled, 1 cancelled;
- `broker_orders`: **75 rows**, with 3 rows joined to fills.

Therefore “81 orders -> about 2 fills” is a useful description of churn but not a stable metric. The
answer changes depending on whether “order” means strategy intent, OMS broker row, cancel request, or
venue order, and whether “fill” means filled intent, filled broker order, or fill row. That denominator
instability is not rounding noise; it is exactly why the shared segment/slot/attempt identity is
required.

After identity coverage exists, the report reads in this order:

```text
segments -> slots -> attempts -> submitted attempts -> terminal attempts -> filled slots
```

The expected shape is many attempts per slot and zero or one consuming fill per slot. A high attempt
count can then be diagnosed as reprice/reject churn without being mislabeled as duplicate exposure.

### Known cause this addresses

- The current count conflates economic opportunities with implementation retries and cancels.
- A large broker-order number can make two fills look impossible or make churn look like duplicate
  trading.

### It does not address

- Why a particular attempt was rejected or repriced.
- Whether the current amount of churn is operationally acceptable.
- Missing venue orders that never reached our database.

### What it cannot know

The historical 81-ish population cannot be repartitioned exactly after the fact because most rows
have no slot identity. Symbol-day grouping would manufacture boundaries and is not a substitute.

### What would falsify it

Once new records have complete identity coverage, grouping shows that the attempts really do belong
to distinct slots rather than retries of the same slot. In that case churn is not the explanation and
the lifecycle boundary is wrong.

## 3. The Webull outcome loop

The OMS already sees more truth than the strategy: risk drops, collision drops, submit reports,
working/partial/fill/reject/cancel events, and broker ids. The strategy currently sees position polls
and a boolean latch, so it infers outcomes from “still flat.” That inference caused both false release
and silent suppression.

Every slot follows one monotonic outcome loop keyed by `fanout_slot_id` and `fanout_attempt_id`:

| phase | outcomes | slot effect |
|---|---|---|
| strategy handoff | `queued` | reserve the slot provisionally |
| before broker submit | `dropped_no_emitter`, `dropped_ineligible`, `dropped_routing`, `dropped_risk`, `dropped_collision`, `dropped_dedup` | release only after this explicit terminal no-submit outcome |
| broker submit | `submitted` | keep reserved |
| broker observation | `working`, `partially_filled` | keep reserved |
| terminal no-fill | `rejected`, `cancelled`, `expired` | release only when terminal is confirmed |
| terminal fill | `filled` | consume the slot for the rest of the segment |
| evidence failure | `could_not_tell` | keep reserved and make the uncertainty loud |

`still_working` is an observation, not a terminal result. A `filled` outcome wins over a later stale
cancel/reject observation. Duplicate events are idempotent by `(slot_id, attempt_id, outcome,
broker_event_id)`; ordering cannot turn a consumed slot back into a free one.

The OMS publishes the outcome only after the corresponding durable local record commits. The strategy
consumes the outcome keyed by slot id. The present grace timeout remains a backstop for observability,
but it must not turn `could_not_tell` into “free”; uncertainty is the state, not permission to trade.

### Known cause this addresses

- `fanout_webull_claimed` expires because the Schwab-scoped position query still reads flat after a
  Webull fill.
- A phantom union close clears the latch even when no shares were held.
- Bot/OMS drops are visible locally but never release the exact strategy claim deliberately.
- A non-2xx response or lost response can no longer be described as a success outcome.

### It does not address

- Reconstructing a venue order that neither produced a durable local record nor a trustworthy reply.
- Exit-child attribution such as the historical DAIC sell.
- Webull SDK pagination, history depth, or credential policy.

### What it cannot know

Without venue reconciliation, `submitted` followed by process loss is `could_not_tell`. The loop can
state the uncertainty precisely; it cannot manufacture the broker's answer.

### What would falsify it

- Any return/drop path emits no outcome.
- A terminal no-fill releases a different slot from the one that created the attempt.
- A stale or duplicate event changes `filled` back to free.
- A process restart loses a locally committed terminal outcome.

## 4. Suppression is intended per slot, not per attempt and not per segment

The intended composition is one resting entry plus one reclaim per ATR segment. Therefore:

- a second **attempt** for the same slot is allowed only as an explicit retry/reprice after the prior
  attempt is terminal or is being replaced;
- a second **fill** for the same slot is suppressed;
- consuming the resting slot does not consume the reclaim slot;
- consuming either slot remains true after the position exits; an exit does not refill an entry slot;
- a segment-wide boolean is too broad because it can block the legitimate second slot;
- an attempt-wide boolean is too narrow because a retry receives a new attempt identity.

This is the answer to the policy question: suppression-by-slot is intended. The existing
`fanout_webull_claimed` boolean mixes segment, slot, attempt, and outcome into one bit and cannot
implement that policy reliably.

The behavior change is deliberately **not** in the first increment. It becomes eligible only after
new records prove the identity mapping and the outcome loop has both a known-bad and known-good
control. Until then, current suppression remains unchanged and is graded as current behavior, not as
proof of the new policy.

### Known cause this addresses

- Same-slot duplicate fills such as the §82 chase population.
- Segment-wide suppression of a legitimate reclaim after a completed resting slot.
- Releasing a consumed slot merely because the position later returned flat.

### It does not address

- Whether Webull should receive both v2 slots as a product/risk choice; this document preserves the
  already-recorded resting-plus-reclaim composition and does not increase quantities.
- Broker-side exits or cross-account position reconciliation.

### What it cannot know

Historical rows without slot identity cannot prove whether a particular reactive-after-resting pair
was the same slot or a legitimate reclaim. The new policy is forward-verifiable only.

### What would falsify it

- The operator's intended composition is one Webull leg total per segment rather than one per v2
  slot.
- A valid strategy case requires two fills in one named slot.
- New identity evidence shows `reactive` is not consistently the reclaim slot.

Any of those is a policy contradiction, not an implementation bug; stop before changing suppression.

## 5. Venue reconciliation is an undecided dependency

The lifecycle has one boundary it cannot close internally: a request may leave our process while its
reply, later status, or combo children do not become a durable local event. Resolving that state may
require venue reconciliation.

This document does **not** choose or design that reconciliation. Before it can be a dependency, a
separate decision must establish whether the Webull capability can provide:

- history reaching the required date floor;
- every page with a provable terminal page;
- parent and combo-child visibility;
- partial-fill representation;
- order detail for every listing miss;
- a distinction between confirmed absence and an unavailable/incomplete response;
- a read-only credential or sandbox boundary acceptable for production investigation.

Until that decision is made, the outcome loop's honest terminal state is `could_not_tell`, and a slot
in that state stays reserved and loud. “Not found in one listing response” is never `cancelled` or
`absent`.

### Known cause this could address

- Lost submit responses, orphan broker orders, unrecorded combo children, and restart recovery.

### It does not address

- Internal drops before a broker request; those are already knowable from our own lifecycle.
- Whether the slot policy is correct.

### What it cannot know

Whether the SDK/account can meet the contract above. That is the undecided dependency, not an
assumption hidden inside this architecture.

### What would falsify the dependency

If every ambiguous state can be resolved from a complete durable local event before any broker call
can escape, venue reconciliation is unnecessary for this lifecycle. Conversely, if Webull cannot
provide complete/read-only evidence, the system must retain `could_not_tell` rather than design
around a capability it does not have.

## First increment — identity only, one attended v2 window

The first increment is intentionally smaller than the outcome loop:

1. Keep `fanout_segment_id` as the segment boundary already shipped by #790.
2. Add `fanout_slot=resting|reclaim` and deterministic `fanout_slot_id` to every Webull fan-out draft.
3. Carry the existing intent/client-order identity as `fanout_attempt_id` in persisted intent, broker
   order, event, and fill metadata; no schema or venue call is required if the existing metadata seam
   proves byte-for-byte propagation.
4. Add a read-only acceptance report in the order above: segments, slots, attempts, submitted,
   terminal, filled. It must print identity coverage and refuse a clean duplicate grade when coverage
   is incomplete.

**What it proves alone:** every new Webull fan-out record can be grouped into the economic slot that
created it, and the 81-ish-attempt / 2-ish-fill shape can be measured without treating attempts as
opportunities during one process lifetime. It proves neither restart continuity, outcome feedback,
nor correct suppression.

**What it changes:** metadata and observability only. It does not release a latch, suppress a leg,
query Webull, change quantity, or alter entry composition.

**One-window test:** deploy v2 once after hours; on the next fan-out opportunity require every queued
draft and every resulting local intent/order/fill to carry one non-zero segment id, one valid slot,
one matching slot id, and distinct attempt ids for replacements. A synthetic reprice must retain its
slot id; a synthetic new segment must change it. If that cannot be shipped and proved in one attended
v2 window, the increment is too broad.

Only after that increment is independently accepted should the OMS outcome publication and strategy
consumption be built. Suppression changes last, against a population whose identity and outcomes are
already readable.

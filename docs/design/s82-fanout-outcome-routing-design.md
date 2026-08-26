# §82 — one Webull fan-out lifecycle: identity, attempts, outcomes, and slot policy

**Status:** DESIGN ONLY. No runtime behavior changes are authorized by this document.

**Written:** 2026-08-26, after #790 made new fan-out legs attributable but before the strategy
could consume a Webull outcome.

## The lifecycle in one sentence -- policy recorded

A Webull fan-out trade needs one durable **segment**, two named candidate **slots** (`resting` and
`reclaim`), and a linked chain of broker **attempts**. Explicit OMS outcomes close the evidence loop,
and a Webull-only fill consumes the Webull fan-out claim only -- never v2's own resting/reclaim
composition. The paired broker legs are the intentional 2x measurement, not drift to reconcile.
Venue-side reconciliation remains an undecided dependency for outcomes our process did not observe.

This is one lifecycle, not five independent proposals:

```text
segment identity
    -> economic slot (resting or reclaim)
        -> attempt 1 -> queued -> dropped
        -> attempt 2 -> submitted -> working -> cancelled
        -> attempt 3 -> submitted -> partially_filled -> filled
            -> outcome recorded
            -> Webull fan-out claim consumed
            -> v2 resting/reclaim composition unchanged
```

## 1. Three identities and the missing replacement edge

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
| `fanout_predecessor_attempt_id` | zero for the root, one for every replacement | the directed edge from a replacement to the attempt it supersedes |

`fanout_slot_id` is derived from `(strategy, symbol, fanout_segment_id, fanout_slot)`. It is stable
across every reprice, retry, cancel, and broker replacement for that slot. `fanout_attempt_id` must
change for every new broker action, which the existing `client_order_id` already does. Distinctness
alone is not the missing property: every replacement must also carry
`fanout_predecessor_attempt_id`, producing an acyclic chain with one root per placement lifecycle.
`cw_entry_n` is retained as a label and is **not** an identity; the resting-path under-count and
historical zero segment ids already prove it cannot carry that job.

The current population proves that distinction. In a rolling 21-day production read at 13:00Z on
2026-08-26, `client_order_id` remained distinct for every row, while the predecessor field was
present on **0 of 12,535** `live:orb` orders and **4 of 1,999** `live:schwab_1m_v2` orders. The
denominators moved slightly during review; the missing-edge result did not.

The existing source mapping is explicit:

| fan-out source | slot |
|---|---|
| `rth_resting`, `rth_resting_mirror`, `eh_resting` | `resting` |
| `reactive` | `reclaim` |

The mapping names which v2 opportunity emitted the fan-out leg. It does not, by itself, decide
whether a Webull-only fill consumes that v2 composition slot. The #644 composition rule says one
resting entry and one reclaim for v2, and that a reactive entry does not silently substitute for a
resting slot that never filled. Section 4 names the cross-venue extension as an operator decision.

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
- Replacements have distinct ids but almost never name the attempt they supersede, so the chain
  cannot be reconstructed.
- A later outcome cannot be routed back to the exact state claim that produced it.

### It does not address

- Whether Webull accepted an order whose response was lost.
- Historical records that never carried a segment or slot identity.
- Whether a Webull exit child filled; this document covers entry-slot ownership, not exit attribution.

### What it cannot know

An identity proves that records belong together. It does not prove that the records are complete or
that their stated broker status is true. Before restart-durable rehydration exists, records on either
side of a process start also cannot prove whether they belong to one economic segment; the acceptance
report must expose the restart and retain `could_not_tell` for that spanning population.

### What would falsify it

- Two retries/reprices for one economic entitlement receive different slot ids.
- A real new ATR segment reuses the prior segment id.
- A resting and reclaim opportunity receive the same slot id.
- The same broker attempt is persisted under two attempt ids.
- A replacement has no predecessor, names an attempt in another slot, forms a cycle, or creates a
  second root in one placement lifecycle.
- A report whose window crosses a v2 restart still emits a clean coverage or duplicate verdict for
  the potentially spanning same-symbol population.

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

“Rejected” is also not one unit. A read-only seven-day `live:orb` census on 2026-08-26 found 208
rejected buy `STOP_LIMIT` rows:

- **179** were client-side aborts (`Webull combo MASTER must be LIMIT or MARKET`); no venue request
  was made;
- **28** carried explicit venue HTTP 417 reasons (16 stop below market, 12 illegal stop/limit
  relationship);
- **1** stored a fan-out source label where a rejection reason should have been, so its cause is
  `could_not_tell`.

The exit side is a different population again: **156** rejected sells (145 market, 11 limit) carried
Webull `NEW_NO_POSITION...CAN_NOT_SELL_SHORT` responses. Those are exit-loop evidence and must not be
counted as failed entry attempts.

The current `event_source` column cannot reliably perform the split. All 179 client abort events in
that seven-day population were labeled `unknown`; the explicit venue rejects were divided between
`unknown` and `broker`. Classification must be emitted at the source of the event, not reconstructed
later by parsing English error strings.

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
- Whether historical `event_source=unknown` rejections were client aborts or venue decisions when
  their stored reason is also unusable.

### What it cannot know

The historical 81-ish population cannot be repartitioned exactly after the fact because most rows
have no slot identity. Symbol-day grouping would manufacture boundaries and is not a substitute.
Likewise, a generic historical `rejected` row does not establish whether the request stopped inside
our client or reached Webull unless the emitting path recorded that provenance.

### What would falsify it

Once new records have complete identity coverage, grouping shows that the attempts really do belong
to distinct slots rather than retries of the same slot. In that case churn is not the explanation and
the lifecycle boundary is wrong.

The rejection classification is falsified if a known client-side abort is emitted as a venue reject,
or a forced venue 417 is emitted as a client abort. Both controls are required; a classifier that
puts everything in one bucket proves nothing.

## 3. The Webull outcome loop

The OMS already sees more truth than the strategy: risk drops, collision drops, submit reports,
working/partial/fill/reject/cancel events, and broker ids. The strategy currently sees position polls
and a boolean latch, so it infers outcomes from “still flat.” That inference caused both false release
and silent suppression.

Every fan-out claim follows one monotonic evidence loop keyed by `fanout_slot_id` and
`fanout_attempt_id`. Recording the facts is policy-neutral. The recorded consumer policy is reading
A: those facts update the Webull fan-out claim and never consume v2's own composition.

| phase | outcomes | fan-out claim effect |
|---|---|---|
| strategy handoff | `queued` | reserve the slot provisionally |
| before broker submit | `dropped_no_emitter`, `dropped_ineligible`, `dropped_routing`, `dropped_risk`, `dropped_collision`, `dropped_dedup` | release only after this explicit terminal no-submit outcome |
| client terminal before submit | `rejected_client_abort` | release only when the local abort is durably classified at its emitting path |
| broker submit | `submitted` | keep reserved |
| broker observation | `working`, `partially_filled` | keep reserved |
| venue terminal no-fill | `rejected_venue`, `cancelled`, `expired` | release only when terminal venue evidence is confirmed |
| unclassified rejection | `rejected_unclassified` | `could_not_tell`; keep reserved until provenance is established |
| terminal fill | `filled` | consume the Webull fan-out claim only; never consume the v2 slot |
| evidence failure | `could_not_tell` | keep reserved and make the uncertainty loud |

`still_working` is an observation, not a terminal result. A `filled` outcome wins over a later stale
cancel/reject observation. Duplicate events are idempotent by `(slot_id, attempt_id, outcome,
broker_event_id)`; ordering cannot turn a consumed slot back into a free one.

The OMS publishes the outcome only after the corresponding durable local record commits. The strategy
consumes the outcome keyed by slot id. The present grace timeout remains a backstop for observability,
but it must not turn `could_not_tell` into “free”; uncertainty is the state, not permission to trade.

The rejected split is a required input contract, not an inference inside the consumer. The local
abort path must emit `rejected_client_abort`; a broker response must emit `rejected_venue`; and an
old or ambiguous generic `rejected` remains `rejected_unclassified`. Until that provenance exists,
the outcome loop cannot treat the current generic bucket as terminal evidence.

The consumer keeps the Webull fan-out claim consumed to prevent another Webull attempt for the same
slot, but it does **not** deplete `cw_resting_taken`, `cw_reclaim_taken`, or any successor field that
governs v2's own entry composition. Section 4 records why the two venue legs must remain independent.

### Known cause this addresses

- `fanout_webull_claimed` expires because the Schwab-scoped position query still reads flat after a
  Webull fill.
- A phantom union close clears the latch even when no shares were held.
- Bot/OMS drops are visible locally but never release the exact strategy claim deliberately.
- A non-2xx response or lost response can no longer be described as a success outcome.
- Client aborts and venue rejects no longer share a terminal bucket whose provenance is mostly
  unreadable.

### It does not address

- Reconstructing a venue order that neither produced a durable local record nor a trustworthy reply.
- Exit-child attribution such as the historical DAIC sell.
- Webull SDK pagination, history depth, or credential policy.

### What it cannot know

Without venue reconciliation, `submitted` followed by process loss is `could_not_tell`. The loop can
state the uncertainty precisely; it cannot manufacture the broker's answer.
It also cannot determine from today's `event_source=unknown` alone whether a historical rejection was
local or venue-side.

### What would falsify it

- Any return/drop path emits no outcome.
- A forced local abort and forced venue 417 land in the same outcome class, or either known control
  is reported as `rejected_unclassified`.
- A terminal no-fill releases a different slot from the one that created the attempt.
- A stale or duplicate event changes `filled` back to free.
- A process restart loses a locally committed terminal outcome.

## 4. Webull and v2 composition are separate -- operator decision: reading A

**Recorded 2026-08-26:** a Webull-only fill consumes the Webull fan-out claim and does **not**
deplete v2's own resting/reclaim composition. Reading B is rejected.

The A/B table is retained because it shows why the same consumer has opposite meanings:

| reading | slots govern | effect of a Webull-only fill | verdict |
|---|---|---|---|
| **A -- selected: separate broker accounting** | v2's own exposure at its own broker; Webull's fan-out claim at Webull | consume only the Webull claim; leave v2 composition unchanged | preserves the intended paired broker sample |
| **B -- rejected: cross-venue composition** | one shared entitlement across both venues | consume the Webull claim and matching v2 slot | suppresses one leg of the experiment by construction |

Reading B failed for three general reasons:

1. **Alternation by survivorship.** If one venue's fill consumes a shared slot, whichever venue fills
   first suppresses the other. The surviving leg is selected by execution order rather than by the
   strategy's composition.
2. **Structural walkover, not a latency race.** The Webull resting-path leg is MARKET-at-cross while
   the Schwab leg remains a resting STOP_LIMIT. Webull is structurally expected to fill first. Under
   B, the system silently becomes Webull-primary with Schwab trading only leftovers, and no outcome
   error is required for that distortion.
3. **It destroys the broker bake-off deliverable.** `dual-broker-v2-design.md` requires both brokers
   to receive the same signal at the same instant, accepts intentional 2x exposure, and compares the
   two fills to decide which broker to retire. `per-broker-eligibility-webull-fallback-design.md`
   repeats “trade the same stock on BOTH brokers” and makes the legs independent. Under B, the
   surviving dataset is conditioned on the other broker being slower; a report would measure that
   selection rule and mislabel it execution quality.

The shared error behind B was treating one cross as one unit of total exposure. In the locked
dual-broker design, one cross intentionally creates **two broker legs**. Their difference is the
measurement. It is not drift for this lifecycle to reconcile.

The consumer contract is therefore explicit:

- every attempt belongs to one named `resting` or `reclaim` Webull fan-out claim;
- a retry/reprice keeps the same slot id and names its predecessor attempt;
- a confirmed Webull fill consumes that Webull claim and cannot be released merely because a later
  Schwab-scoped position read is flat;
- the consumer never mutates v2's own resting/reclaim taken state;
- the outcome is recorded before the Webull claim consumer acts.

**Duplicate-exposure alarms are venue-scoped.** The identity may pair Schwab and Webull legs for the
comparison report, but a duplicate alarm keys at least on `(broker_account, symbol, segment, slot)`.
One Schwab fill plus one Webull fill for the same cross is the expected 2x pair, not a duplicate. An
alarm that collapses the venue dimension would fire on the intended experiment and call it a defect.

### Known cause this addresses

- Repeated Webull fills for one Webull fan-out claim after the claim was released from a
  Schwab-scoped flat read.
- False duplicate alarms that mistake the intentional Schwab/Webull pair for same-venue repetition.
- Silent loss of the slower broker leg through cross-venue slot depletion.

### It does not address

- The deliberate 2x cross-venue exposure. That remains accepted experiment design, not an open gap.
- Broker-side exits, cross-account netting, or capital sizing.
- Which broker executes better; preserving the paired sample makes that measurable but does not
  answer it.

### What it cannot know

Identity and venue-scoped suppression cannot establish execution quality, protection quality, or the
venue truth missing from local records. Historical rows without slot identity also cannot prove
which Webull attempts were same-claim duplicates.

### What would falsify implementation compliance

- A Webull fill changes v2's resting/reclaim consumed state.
- The intended Schwab leg is suppressed only because its paired Webull leg filled first.
- A duplicate alarm fires solely because one expected fill exists at each venue.
- A second Webull fill in the same `(broker_account, symbol, segment, slot)` is permitted after the
  first confirmed fill.

Those are implementation defects under reading A; they do not reopen reading B.

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
4. Add `fanout_predecessor_attempt_id` to every replacement. The first attempt has no predecessor;
   every later attempt names the exact attempt it supersedes.
5. Add a read-only acceptance report in the order above: segments, slots, attempt roots, chain depth,
   submitted, terminal, filled. It must print identity and predecessor-link coverage and refuse a
   clean duplicate grade when either is incomplete.
6. Make process continuity part of the report's denominator. Print every v2 PID/process start that
   overlaps the window. If the window crosses a restart, any same-symbol lifecycle that may span the
   boundary is `could_not_tell`; it must not be graded from two process-local segment sequences as if
   they were one complete population.

**What it proves alone:** within a single process lifetime, every new Webull fan-out record can be
grouped into the economic slot that created it; replacements form a readable predecessor chain; and
the 81-ish-attempt / 2-ish-fill shape can be measured without treating attempts as opportunities.
Across a restart it proves only that the report detects the boundary and refuses a false clean. It
does not prove identity continuity across that boundary, outcome feedback, or correct suppression.

**What it changes:** metadata and observability only. It does not release a latch, suppress a leg,
query Webull, change quantity, or alter entry composition.

**One-window test:** deploy v2 once after hours; on the next fan-out opportunity require every queued
draft and every resulting local intent/order/fill to carry one non-zero segment id, one valid slot,
and one matching slot id. A synthetic reprice must retain its slot id, mint a new attempt id, name the
prior attempt, and increase chain depth by one; a synthetic new segment must change the segment and
slot ids. The report needs both polarity controls: a complete single-PID fixture must stay clean, and
a two-PID same-symbol fixture with no durable cross-restart link must return `could_not_tell`. Merely
showing distinct attempt ids is not an acceptance condition; production already has that property.
If this cannot be shipped and proved in one attended v2 window, the increment is too broad.

Only after that increment is independently accepted should the OMS outcome publication be built.
The later consumer is now specified by section 4: consume the Webull claim and never v2's own slot.
It still requires known-bad and known-good controls against a population whose identity and outcomes
are already readable.

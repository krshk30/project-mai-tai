# D20 fan-out duplicate acceptance grading

## Decision

Grade the duplicate fix over **live mirror-cross opportunities**, not over all fan-out fills,
all mirror orders, or the absence of duplicate orders.

The denominator is one row per observed up-cross of a resting-mirror level while that mirror is
live. Each row must carry the Webull `fanout_slot_id`. A session with zero such rows is
`UNEXERCISED`; it is never a pass.

The filled-claim suppression cost is a separate metric. It reports how many
`V2-FANOUT-RECLAIM-BLOCKED-BY-FILLED-CLAIM` events joined to a Webull position that was still open,
how many joined to a Webull position already flat, and how many could not be classified. It does
not widen the strategy position union across venues.

## The 2026-08-31 known-bad attempt census

The first control incorrectly stopped at 15:30 UTC. The complete 2026-08-31 ET session is
04:00 UTC on August 31 through 04:00 UTC on September 1, spanning the tail of
`schwab-1m-v2.log-20260831.gz` and all of `schwab-1m-v2.log-20260901`. That population contains
27 `V2-FANOUT-RTH-RESTING` emissions and 27 durable `rth_resting` intents: 20 rejected, five filled,
and two cancelled. These are measured attempts admitted after the old guard. They prove the defect
was exercised, but they are not the number of live mirror up-crosses that exercised it.

The previously published value of **16 crossing edges is withdrawn**. It combined 12 groups of
REST emissions sharing a slot and mirror level with four inferred re-arms. The 12 groups begin at a
placement or changed level, neither of which proves an up-cross. The four boundaries came from
intervening below-level `market_quote_ticks` LEVELONE events, but that observer-only streamer is not
the strategy's REST `on_quote` input and cannot prove the strategy observed the corresponding
re-cross. The composite therefore has no measured unit under the #863 event contract.

No retained 2026-08-31 artifact records the required pre-guard observation. The historical
`crossed_mirror_slots` denominator is unavailable and must not be replaced by a reduction of the 27
attempts.

## Why the old proxies are insufficient

Two tempting proxies answer different questions on the same session:

- Filled `rth_resting_mirror` broker orders: 18 orders and 18 distinct slot IDs.
- Live one-minute bars whose high reached a mirror stop during the broker-order interval: 19
  orders and 18 distinct slot IDs.

Neither is the required control. Broker execution and one-minute OHLC do not prove that the
strategy's live quote path observed the crossing while the mirror flag was live. Mirror placements,
distinct `(symbol, level)` pairs, and symbol counts measure still other populations and are also not
substitutes.

The old `V2-FANOUT-RTH-RESTING` line is not a usable post-fix denominator either. It is emitted only
after the duplicate leg is admitted. PR #858 returns on `webull_resting_active` before that line,
so a working fix and an unexercised path both print zero. Grading that zero would be circular.

## Required crossing evidence

PR #863 supplies an observation-only event at the live quote up-cross, before the
`webull_resting_active` veto:

```text
[V2-FANOUT-MIRROR-LIVE-CROSS] SYMBOL slot_id=... cross_seq=... px=... level=...
```

The event contract is:

- Require fan-out enabled, RTH resting mode, a fresh live bar, a positive live quote, and
  `px >= resting_level` while `webull_resting_active=1`.
- Do not gate the observation on Schwab position quantity or `fanout_webull_claimed`. Those are
  other protections and would cover for the mirror-live veto under test.
- Emit once per real below-to-at-or-above transition. Reset the observation edge only when price
  returns below the current level or a new mirror level is placed. Repeated quotes above one level
  must not inflate the denominator.
- Derive `slot_id` with the existing fan-out identity contract. `cross_seq` distinguishes multiple
  real up-crosses of one economic slot without changing slot identity.
- Mutate observation state only. Do not claim or release a slot, alter an order, or change either
  venue's position input.

PR #863 added this event, but it was not present on 2026-08-31 and is not yet deployed. Until the
marker is deployed and a completed session exercises it, D20 remains `COULD_NOT_TELL`, not PASS.

## Duplicate grade

For each `V2-FANOUT-MIRROR-LIVE-CROSS` row in the session, join `(symbol, slot_id)` to durable
`trade_intents.payload.metadata` on `live:orb` and count a duplicate leg when a later BUY-open
intent with `fanout_source=rth_resting` carries the same slot ID for that crossing opportunity.
Count intents regardless of terminal status. The 27 attempts from 2026-08-31 are a known-bad
attempt census only; do not use them to set or validate `crossed_mirror_slots`.

| Evidence | Verdict |
|---|---|
| `crossed_slots = 0` | `UNEXERCISED` |
| Any crossing lacks a valid slot ID, or the join is ambiguous | `COULD_NOT_TELL` |
| `duplicate_legs > 0` | `FAIL` |
| `crossed_slots > 0` and `duplicate_legs = 0` | `PASS` |

The report line must include both values:

```text
metric=duplicate_legs verdict=PASS duplicate_legs=0 crossed_mirror_slots=7
```

Do not use all Webull fills, all mirror fills, all armed segments, or cross-venue held quantity as
the denominator.

## Filled-claim cost grade

`V2-FANOUT-RECLAIM-BLOCKED-BY-FILLED-CLAIM` is the filled-claim subset of
`V2-FANOUT-REACTIVE-SUPPRESSED`. Pair each cost line to exactly one immediately preceding
suppression for the same symbol, and require its `slot_id` to join to exactly one Webull fan-out
BUY-open lineage.

At the cost-line timestamp, reconstruct the venue book from `live:orb` fills only:

- `open_at_webull`: Webull net quantity for the symbol is positive. The block prevented another
  leg while a Webull position was still open.
- `flat_at_webull`: Webull net quantity is zero. The block suppressed a possible legitimate
  re-entry; this is the measured cost of the operator's BLOCK ruling.
- `could_not_tell`: the marker is unpaired, the slot join is missing or non-unique, fill coverage
  is incomplete, or the Webull ledger is internally inconsistent.

The output must reconcile all three buckets to the filled-claim subset:

```text
metric=filled_claim_block_cost verdict=OBSERVED filled_subset=4 open_at_webull=3 flat_at_webull=1 could_not_tell=0 reactive_suppressions=9
```

Zero filled-claim suppressions is `UNEXERCISED`. Any `could_not_tell` row makes this metric
`COULD_NOT_TELL`. Otherwise the metric is `OBSERVED`, not a generic PASS: a nonzero
`flat_at_webull` count is the explicit cost the line was added to expose.

This venue-book reconstruction is for reporting only. It must not feed strategy state and must not
change the post-#843 Schwab-scoped position union.

## Controls and mutants

The implementation PR must include these controlled checks:

1. The retained 2026-08-31 artifacts reproduce 27 emissions and 27 durable intents, and report the
   historical crossed-mirror denominator as unavailable. Mutants that substitute raw emissions,
   mirror fills, bar crossings, placements, or distinct symbol-level pairs for the denominator fail.
2. Removing the mirror-live veto keeps the crossed denominator fixed and changes only
   `duplicate_legs` from zero to nonzero.
3. Killing the crossing event yields `UNEXERCISED`, never PASS.
4. A cost line without its suppression sibling or with an unknown/non-unique slot ID is
   `COULD_NOT_TELL`.
5. A Webull-open/Schwab-zero fixture classifies `open_at_webull`. Any implementation that widens
   the position union across venues fails this control.
6. A Webull-flat fixture classifies `flat_at_webull`, proving the operator-ruling cost remains
   visible rather than being folded into a clean duplicate count.

## Delivery sequence

1. Deploy the merged #863 observation event and duplicate fix together. No trading behavior or
   position-union change belongs in the observation increment.
2. D20 adds the read-only log-plus-database grader after the event schema and the unavailable
   retrospective-control ruling are pinned.
3. The reviewed artifacts run together for the measured session. The first completed
   session reports `PASS`, `FAIL`, `UNEXERCISED`, or `COULD_NOT_TELL`; no bare zero is published.

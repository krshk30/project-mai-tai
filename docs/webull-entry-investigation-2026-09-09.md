# Webull Entry Investigation - 2026-09-09

Status captured at approximately 16:30 ET on 2026-09-09. This note preserves the
baseline for comparison with the next live session. All times below are ET.

## Scope and accounting

This investigation covers V2 entry activity for FTFT, SUNE, and YMAT on Webull.
The V2 Webull fan-out account is stored under the legacy database name
`live:orb`; it is identified here by its Webull provider and V2 strategy
metadata, not by the account label. The broker-disconnected ORB paper service is
not part of this population.

Repeated resting-order reprices and retries are not counted as separate trade
decisions. The durable record contained 67 Webull open intents grouped into 22
stable fan-out slot identities. Two additional reclaim decisions reused an
already-consumed slot and were suppressed before a new Webull intent existed.
The resulting denominator is 24 logical producer episodes.

| Symbol | Logical episodes | Webull fills | Locally blocked while held | Consumed-slot suppressions | Rested, never filled |
| --- | ---: | ---: | ---: | ---: | ---: |
| FTFT | 7 | 5 | 0 | 1 | 1 |
| SUNE | 5 | 4 | 0 | 1 | 0 |
| YMAT | 12 | 4 | 7 | 0 | 1 |
| Total | 24 | 13 | 7 | 2 | 2 |

There were zero Webull venue rejections among 54 recorded Webull buy orders and
zero ineligibility rows for the three symbols. The 13 raw rejected intents all
occurred before broker submission and collapse to seven logical YMAT episodes;
their refusal was `fanout_webull_collision_managed`.

For the two symbols traded by both brokers, Schwab filled 11 entry episodes and
Webull filled the matching leg on 9 of 11. The missing Webull legs were FTFT at
12:10 and SUNE at 13:07.

## Findings

### FTFT 12:10 - consumed fan-out slot

The first entry filled both brokers at approximately 11:58. After that position
closed, the strategy placed a rested reclaim at 12:01 and Schwab filled it at
12:10. No Webull order was submitted.

The first entry and rested reclaim both used the same durable identity tuple:
strategy, symbol, ATR segment, and `slot=resting`. The first Webull fill had
already consumed that identity. The later reclaim therefore produced
`[V2-FANOUT-SLOT-CONSUMED] ... suppressed=1` while the Schwab resting order
remained eligible to fill. This was a one-sided producer defect, not a Webull
rejection.

### SUNE 13:07 - consumed slot plus a stranded Webull position

The first entry filled both brokers at approximately 12:50. A confirmation exit
fired for both accounts at 12:52. Schwab closed, but Webull did not.

For the same Webull protection base, the log first reported two of two children
released at 12:52:13.094, then a second evaluation reported
`pair_cancel_unconfirmed` at 12:52:13.355. The fan-out result refused the
Webull close. The Webull share remained held until the 13:25 ATR exit.

The deployed OMS releases `_confirmation_exit_inflight` immediately after the
protection-cancel call, before the close reaches a terminal result. A second
quote evaluation can therefore re-enter during the released-but-not-yet-closed
interval. This is a separate confirmation-exit race.

The later rested reclaim also reused SUNE's consumed `resting` slot. Schwab
filled it at 13:07 while Webull emitted no new order. Even with a distinct slot,
the existing Webull position should have caused the managed-position collision
guard to refuse another buy.

### YMAT - local held-position refusals

YMAT produced four Webull buy fills. Seven distinct logical entry episodes were
blocked locally at approximately 09:35, 09:59, 12:38, 13:00, 13:18, 13:58, and
15:55. Every block occurred while Webull already held YMAT. These are overlap
protections, not broker eligibility or venue failures, and the collision guard
must not be weakened to increase the fill count.

One additional YMAT resting episode from approximately 14:09 through 14:27 was
submitted and repriced but never filled. It was not rejected.

## Recommended disposition

1. Use RECLAIM1 PR #933 for entry symmetry. When reviewed, merged, deployed, and
   explicitly enabled, it admits only the first ATR-trail resting entry in each
   confirmed ATR BUY segment and disables both reclaim producers. FTFT 12:10 and
   SUNE 13:07 would then be refused on both brokers instead of filling Schwab
   alone. This intentionally reduces trades; it does not make Webull chase the
   missing reclaim.
2. Keep the Webull managed-position collision guard. It correctly prevented the
   seven YMAT overlap episodes and would correctly refuse SUNE while the earlier
   Webull share remained held.
3. Fix the confirmation-exit race separately. Per-account in-flight ownership
   must remain held from protection release through terminal close submission,
   confirmed flat, or completed re-protection. A control should replay SUNE's
   two overlapping evaluations and prove one protection cancellation and one
   Webull close attempt.
4. Do not treat merged code as tomorrow's behavior until the exact production
   state is verified. At capture time PR #933 was open, unpinned, default-off,
   unmerged, and undeployed. PR #930 was merged but production remained on box
   SHA `72b13393078f07543162c13ca823bbcacc742c81`.

## Next-session comparison checklist

Run this comparison after the next full session, split by broker and symbol:

1. Count logical entry episodes, not raw intents or reprice retries. Preserve the
   raw counts alongside the logical denominator.
2. For each episode, report decision time, ATR segment, producer (`first`, rested
   reclaim, or reactive reclaim), submitted account legs, accepted legs, fills,
   cancellations, local refusal reason, and venue rejection reason.
3. Report one-sided entry episodes explicitly: one broker filled while the other
   had no order, was locally refused, was venue-rejected, or merely did not fill.
4. Confirm that strict RECLAIM1 mode emits no rested-reclaim or reactive-reclaim
   entry on either broker. A zero requires the producer-evaluation denominator.
5. Count `[V2-FANOUT-SLOT-CONSUMED]` by symbol and reason. Under strict mode, no
   later reclaim should reach this guard.
6. For each `fanout_webull_collision_managed`, prove whether a Webull position or
   managed row was actually open at that moment.
7. Compare exit timing between Schwab and Webull. Flag any position remaining on
   one broker after the other closes, including the release result and duration.
8. Search for the contradictory Webull confirmation sequence: a release followed
   by `pair_cancel_unconfirmed` for the same account, symbol, position, and
   protection base.

## Evidence sources

- `dashboard_snapshots` rows with `snapshot_type='v2_fanout_outcome'`
- `trade_intents`, `broker_orders`, `broker_order_events`, and `fills`
- `webull_ineligible_today`
- `/var/log/project-mai-tai/schwab-1m-v2.log`
- `/var/log/project-mai-tai/oms.log`
- Deployed source at box SHA `72b13393078f07543162c13ca823bbcacc742c81`


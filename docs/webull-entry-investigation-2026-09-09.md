# Webull Entry Investigation - 2026-09-09

Initial three-symbol status captured at approximately 16:30 ET on 2026-09-09.
Expanded later that day with the seven-day census below. This note preserves
both the incident detail and the canonical baseline for comparison with the
next live session. All times below are ET.

## Seven-day categorized census

This is the canonical wider-window comparison for 2026-09-03 through
2026-09-09 ET: seven calendar days containing four trading sessions. Reprices
and retries are collapsed into logical producer episodes. The denominator is
72/72 episodes. Local refusals and safety suppressions are separated from
actual Webull venue rejections. This table supersedes the narrower three-symbol
counts only for the wider-window category question; the incident-level evidence
below remains the source for FTFT, SUNE, and YMAT.

| Outcome | Category | Logical episodes | Raw evidence | Symbols | Meaning and next step |
| --- | --- | ---: | ---: | --- | --- |
| Filled | Webull entry filled | 34/72 | 34 fills | AEHL, BNC, CDTG, CHPT, FTFT, GELS, IMRN, MOBX, NUR, SUNE, YMAT | Control population; no rejection occurred. |
| Blocked/rejected | Consumed entry slot | 21/72 | 25 suppression markers | AEHL, BNC, CDTG, CHPT, FTFT, GELS, IMRN, NUR, SUNE, YMAT | Our code suppressed these before creating another intent. RECLAIM1 should prevent most attempts earlier; retain the slot guard and measure the next session. |
| Blocked/rejected | Webull position already held | 7/72 | 13 local refusal intents | YMAT | The OMS refused these before broker submission. RECLAIM1's account-neutral position gate should now refuse both legs earlier; retain the OMS collision guard. |
| Blocked/rejected | Webull minimum order size | 3/72 | 13 venue rejections | MIMI | The only actual Webull venue-rejection category: Webull required 100 shares below $1 while the configured leg was one share. Do not auto-size; consider a pre-submit capability refusal only after the next-session comparison. |
| Blocked/rejected | Ask past cross cap | 1/72 | 1 local refusal | AEHL | Local chase protection, not a Webull rejection. Keep the cap; no change is authorized from this census. |
| **Blocked/rejected subtotal** | **All four causes** | **32/72** | **52 refusal/suppression records** | **11 symbols** | **Observe the first full session with RECLAIM1 enabled before deciding whether another fix is needed.** |
| Accepted, unfilled | Submitted, then cancelled or expired | 6/72 | 6 final unfilled outcomes | BNC, FTFT, MOBX, NUR, SST, YMAT | Webull accepted these; they were repriced, cancelled, or remained unfilled. They are not broker rejections. |
| **Total** | **All categorized Webull producer episodes** | **72/72** |  |  | **34 filled + 32 blocked/rejected + 6 accepted but unfilled = 72.** |

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
   unmerged, and undeployed. It has since merged as `dfde881a` but remains dark
   and undeployed. PRs #927, #928, #929, and #930 were also merged but
   undeployed; production remained on box SHA
   `72b13393078f07543162c13ca823bbcacc742c81`.

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

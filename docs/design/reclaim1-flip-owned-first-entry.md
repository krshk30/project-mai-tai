# RECLAIM1: flip-owned first resting entry

## Status and scope

This change is default-off behind one switch:
`strategy_schwab_1m_v2_flip_owned_first_entry_enabled`.

When the switch is on, one ATR-trail resting entry opportunity belongs to one
confirmed BUY flip. The resting order may fill before the BUY flip is confirmed.
If any sibling broker leg remains open when that flip closes, the opportunity is
bound to that flip and remains consumed until the next SELL flip. If every sibling
position closes before a BUY flip is confirmed, the opportunity is retired and a
new first rest receives a new durable identity.

The strict path admits only `cw_entry_slot=first`. Both reclaim producers are off:
the rested segment-high producer and the reactive intrabar segment-high producer.
A missed first rest skips that flip; neither reclaim producer substitutes for it.

This changes entry admission only. It does not change quantities, dual-broker
routing, exit rules, `SymbolState.cw_arm_bar_ts`, or broker protection.

## Fail-safe and rollback

Switching the flag off and restarting `schwab-1m-v2` restores the pre-RECLAIM1
producer and slot behavior. The off path does not read or write the ownership
store and does not add an admission check. The known 2026-09-09 compatibility
controls retain the FTFT 12:01 rested reclaim and the SUNE 13:06 fresh first rest.

Ownership rows are append-only `DashboardSnapshot` records, so rollback requires
no migration and no cleanup. An in-flight position remains under the existing OMS
exit and broker-protection paths. On restart with the flag off, the new ownership
rows are ignored; the existing fan-out identity and legacy boot/position guards
continue to apply. Re-enabling the flag later restores only a current-session row
whose opportunity identity exactly matches the active fan-out identity. Missing,
malformed, mismatched, stale, or replaced evidence refuses entry.

## Durable identity

The managed-position row UUID is the position-episode identity for each broker
leg. One fresh query reads open `schwab_1m_v2` rows for both configured live
accounts. A dual-broker fill is one logical opportunity: either fill consumes it,
and all observed sibling rows must close before a pre-flip opportunity can be
released.

`fanout_segment_id` remains the cross-emitter order identity. It is not redefined
as the ATR flip ID. A pre-flip close retires that identity through the existing
fan-out persistence store before minting a later first-rest identity. Strict-mode
drafts carry `fanout_identity_schema=entry_opportunity_v2`; legacy drafts carry no
new field. D20/#862 results using the older ATR-segment grouping are therefore a
v1 population and must not be pooled with v2 entry-opportunity counts.

`SymbolState.cw_arm_bar_ts` retains its confirmed-flip-bar meaning. A draft built
before confirmation records `cw_arm_bar_ts=0`, not the unrelated fan-out identity.
No money path reads that payload field.

## Assumption register

| Assumption | Live marker | Denominator and false direction |
| --- | --- | --- |
| A provisional first-rest fill still held when the BUY flip closes belongs to that flip. | `[V2-FLIP-OWNER-BIND-EVALUATED]` | `bind_evaluated`; outcomes are `bound`, `pending`, and `unknown`. Missing, stale, or mismatched evidence becomes `unknown` and permits no entry. |
| All sibling positions closing before any BUY flip confirms releases the provisional opportunity. | `[V2-FLIP-OWNER-PREFLIP-CLOSE-EVALUATED]` | `preflip_close_evaluated`; outcomes are `released` and `unknown`. A persistence failure remains closed and does not mint a replacement. |
| Explicit Webull fill evidence plus the fresh account-neutral managed-position book is sufficient to establish whether every sibling position remains open. | `[V2-FLIP-OWNER-CROSS-ACCOUNT-EVIDENCE]` | `cross_account_evaluated`; outcomes are `known`, `pending`, and `unknown`. An unreadable book, an unannounced account row, duplicate rows, a replaced UUID, or a missing row beyond the settle bound becomes `unknown`. |
| Every attempted first-rest admission had current evidence and a readable restart state. | `[V2-FLIP-OWNER-ADMISSION]` | `admission_evaluated`; `refused_unknown` is the fail-closed subset. The marker also names the slot and reason. |

The same cumulative counters are exported in the v2 heartbeat as `flip_entry_*`.
No zero is a pass without its matching `*_evaluated` denominator.

## Replay limit

This rule is not backtested. The current replay runner sets `filled = False` once
per symbol and breaks after the first completed exit. It cannot model close,
subsequent ATR flip, and fresh first-rest re-entry. Live markers and denominators
are the accepted substitute for this release; adding a multi-trade replay
lifecycle is separate work.

## Deployment gate

This PR does not enable the switch. Enabling requires an after-close deployment,
both live accounts freshly flat, an exact-head independent pin, and a rollback
rehearsal that flips the one setting off and restarts v2 without data changes.

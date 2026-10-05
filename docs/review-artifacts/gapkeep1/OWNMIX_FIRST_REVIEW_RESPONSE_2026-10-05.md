# OWNMIX1 #1094 - first-review response, no source change

Read-only at b94bd0ff0c2c7da681da45d748db61db82250b7c. Complete review
pending: branch has not been edited, rebased, committed or pushed.

## F1 - Confirmed Flat Ends The Episode

AGREE. `_v2_close_reconcile_flat` (service.py:8203) returns on unbound entry
or mismatched child after a positive broker-flat read. A pending-fill record
can also return before the flat read. The flat backstop at8851 delegates to
child-required resolution. Ownership attribution and position lifecycle
must be separated; removing only the two reported returns is insufficient.

Fix plan after the complete review: capture the exact managed-row UUID before
broker awaits; retain existing fresh-fill/positive-flat/replacement-race
protections. A confirmed-flat read closes that same episode regardless of
missing binding, foreign child, mismatched quantity or attribution-read failure.
HELD/UNKNOWN remains managed. Never close a new replacement row.

Only a proven owned child with matching full-close evidence may write an exit
fill/P&L. Unproven detail writes none, logs owned/candidate IDs and the flat
closure reason, and retains a deduplicated unresolved `oco_exit_fill_unrecorded`
incident/page. Do not let management cleanup erase pending attribution proof
or resolve that incident merely because the broker is flat. Existing mismatch
text `row_stays_open=1` and ownership-mismatch paging exclusion need correction.
An incident source existing in code is not proof that its installed pager runs.

Required tests on both account paths: unbound+confirmedflat;
bound+foreignchild+confirmedflat; bound+ownchilddifferentqty+confirmedflat;
pending/unreadablechild+confirmedflat; HELD/UNKNOWN; replacement duringawait.
Assert exact row closed, no invented fill, IDs and unresolved page/incident,
episode cleanup, and positive owned-fill attribution preserved. Current tests
`test_no_bid_broker_flat_without_child_fill_holds_and_pages` and
`test_reject_flat_backstop_cannot_close_a_known_unrecorded_child_fill` encode
the old lifecycle coupling and require explicit revised expectations.

## Q1 - Attach/Persist Window Is Not Covered Safely

**Confirmed gap, not covered by existing tests.** Attachment remembers
`_webull_protect_base[(account,symbol)]` at3382, then awaits persistence with
3.75s retry backoff plus DB latency. `_oco_exit_base_for_entry` at7351 reads
only the bound entry payload, so it returns empty before commit or after
exhausted persistence. Shared exit release logs BARE_POSITION_PROCEEDING at
5406; `no_pair` at5307 allows the sell path. A real pair can therefore remain
unreleased. Do not restore account+symbol latest-buy/handle fallback.

Proposed remedy for complete review: attach state bound to exact managed
episode + entry IDs, published immediately upon successful attach; its
pending/persist-failed/unknown state must never mean bare/no-pair. Exact
episode-owned handle can address cancel/readback during the persist window;
after memory loss, missing durable proof refuses release pending proven
recovery. Stale attachment state cannot release a replacement episode.

Add real cancel-then-sell tests with successful attach paused before commit,
failed persist after retries, and replacement episode. Existing
test_webull_attach_protection.py:188 proves memory storage only;
test_webull_protect_handle_persistence.py:77 proves retry/incident only.
Neither proves safe concurrent release.

## Q2 - Parent Read Budget

Own loaded-process read16:01:39Z/12:01:39ET: OMS23705,
MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS=15; active-stop-guard cadence1.0s.
Source `_refresh_native_oco_armed_state` at9590 does one serial parent GET
per eligible bound Schwab managed row per sync, with no separate throttle.

| Cadence | 1 position | 3 positions | 6 positions |
|---|---:|---:|---:|
| Loaded normal15s, nominal GET/min |4|12|24|
| Loaded active-stop1s, nominal GET/min |60|180|360|

These are new parent-read budget calculations, not measured total HTTP
traffic. Network work can slow a cycle; post-intent syncs can add cycles.
Positions/order-list reads, 30s exit polls, resolution and auth retries are
additional. Refresh happens after positions sync and eligible intents;
the count is not globally capped at24/min. Call-count/throttle tests are
missing. Any proposed cache/throttle changes require review separately.

## Q3 - Rollback

No OWNMIX switch. Reviewed rollback must be a code revert/old exact-SHA
checkout plus approved service sequence, **schema retained**, not migration
downgrade. Migration0022 is additive nullable columns with no backfill or
new constraints; dropping it would discard bindings. Old code ignores them
but restores the old attribution vulnerability and creates unbound rows.
Old-code-against-retained-schema compatibility still needs a test. A later
redeploy must handle legacyNULL rows safely, not infer ownership by quantity.
No rollback execution is authorized or performed here.

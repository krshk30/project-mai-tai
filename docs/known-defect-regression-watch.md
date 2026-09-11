# Known-defect regression watch

This watch answers one narrow question: did a defect we already paid for return? It does not
alert on a marker merely because the marker exists. Every row records the evidence, the benign
`GUARD_WORKING` polarity, and the distinct `RECURRENCE` polarity. A row that lacks that last
discriminator is `UNARMED`; it cannot report a clean zero.

The watch is read-only and imports the pager, delivery-result handling, state loader, and atomic
state writer from `ops/health/unexercised_watch.py`. It creates no second notification route.

| Row | State | Evidence | Guard working | Recurrence |
|---|---|---|---|---|
| `BOOT1` | ARMED | ordered boot restore/hold lines | complete before release | early release or no release within 5m |
| `ROLL1` | ARMED | current-session roll line | boundary line exists, including `rolled=0` | no line by 04:10 ET |
| `DISARM1` | UNARMED | arm/disarm markers | cause-specific disarm | a live arm and silent clear are not distinguishable yet |
| `ORPHAN1` | DELEGATED | broker order ownership | no stale unowned order | `orphan_order_check.py` red shape |
| `CAP1` | DELEGATED | fill-time segment composition | legal slot composition | `v2_entry_fix_watch.py` breach |
| `PHANTOM1` | ARMED | stable managed/broker double-read | open row has fresh broker quantity | open managed row with fresh broker zero |
| `REDIS1` | UNARMED | producer/consumer stream evidence | snapshots self-heal | one-shot loss lacks a durable acknowledgement |
| `BARGAP1` | DELEGATED | persisted bar continuity | no restart-spanning gap | bar-gap watch reports a non-halt gap |
| `SAW1` | UNARMED | broker-origin rejects plus episode identity | episode stops at ceiling | time buckets can merge episodes, so recurrence is not yet provable |
| `CHURN1` | DELEGATED | managed limit/bid/cancel chain | marketable exit rests | P0a watch reports churn |
| `RESERVE1` | ARMED | managed Webull sell outcomes | exit pair positively released/resolved | broker reverse/short rejection |
| `W4291` | ARMED | Webull reads and 429 timestamps | isolated 429 backs off | 10 position-sync 429s within 60s |
| `CEILING1` | UNARMED | ceiling plus episode identity | episode stands down | DB has no durable boundary for post-ceiling rejects |
| `FALSEFLAT1` | UNARMED | broker and ownership ledgers | inferred fresh flat keeps protection | deletion removes the ownership discriminator |
| `VPZERO1` | ARMED | open managed + fresh broker + virtual row | virtual quantity remains positive | broker and managed positive, virtual absent/zero |
| `SEED1` | ARMED | seed census/refusal/fail-open lines | stale seed is dropped | calendar failure proceeds as contiguous |

## Weekend activation

1. Merge the watch without installing it during the live session.
2. Over the weekend, run `sudo ops/health/install_known_defect_regression_watch.sh`. It installs
   one dedicated `/etc/cron.d` file at five-minute cadence and never rewrites a shared crontab.
3. Run `known_defect_regression_watch_cron.sh --selftest`; the phone message must explicitly say
   no live defect was observed.
4. Run once with `--no-page` and inspect `STATUS.txt`. It must contain all 16 rows. `UNARMED` and
   `DELEGATED` are inventory states, not passes.
5. On Monday, a recurrence pages once per episode; failed delivery retries next run, and a cleared
   episode re-arms. Evidence loss returns non-zero and never becomes a measured zero.

The cron runs every five minutes, but the wrapper evaluates only 03:50-20:15 ET on weekdays. The
host ignores cron timezone directives, so this ET guard is part of the installed behaviour rather
than documentation. `--selftest` deliberately bypasses the window so weekend delivery proof works.

The four delegated rows remain on their existing specialist checks to avoid duplicate pages. C6
owns restart evidence and the weekend deploy checklist. C1 and C2 are separate after-close
measurement harnesses, not inputs to this watcher.

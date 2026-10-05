# LINE=CHART - independent re-add/restart Step 0

## Verdict

**AGREE on the RETO cause and the widened card; OLD defect class.** A recent
bar releases warmup today even when the intervening history is incomplete.
Late REST history is persisted but skipped by the strategy once a newer
streamed bar has advanced its cursor. A restart uses the same seed/warmup
machinery. There is no complete-line admission gate today.

The previously reported mathematics and exit-scope differences are now
explicitly accepted by the operator/reviewer. The historical assessment at
e90a7073 remains a record of what needed that approval, not a current parking
instruction. No missing/unknown-data carry, shortened wait, late flip entry,
or independent exit line is proposed.

**UNMEASURED:** exact historical chart-feed coverage, historical live
dual-input coverage, fills/profit caused by these differences, and an exact
total of watchlist add/re-add episodes where the log truncates the names.
The retrospective census is not a live silence classifier PASS.

No production changes. No #1094 edit or push. This commit is assessment and
first-review answers only, not a trading fix or install approval.

## Own Instruments And Bounds

Own SSH read began 2026-10-05 15:58:30Z (11:58:30 ET), population frozen at
15:50Z (11:50 ET). Thirty-one retained v2 files, 26 calendar dates with
retained markers, 29,084 selected event lines, 47,424 fresh-age probe lines,
60,479 stored one-minute bars. SQL read-only, five-second statement timeout
per query, <=50,000 bar rows/day and <=40 RETO intents. Nice19 on the box;
zero Redis/snapshot-batch reads, broker calls or application writes.

The first pull's final intent query used denormalized columns that do not
exist; it failed read-only and produced no retained result. The corrected
pull joined strategies/accounts; all final queries succeeded. No failed
query is concealed as coverage.

Raw directory: `/Users/velkris/.codex/gapkeep1-evidence-20261005/`.
- `line-chart-own-readd.json`: original log/bar/intent pull, 42,602,415 bytes.
- `line-chart-own-replay.json`: reproducible comparison and full exceptions.
- `line-chart-own-holes.json`: fresh bounded counts from both tape tables.
- `ownmix-sync-loaded.txt`: separate loaded-OMS cadence read for Q2.

SHA256 respectively: cfd6f99c7411ce67c4f2c93fcd7c2771ba883fffc4792b8f1acc2f5b49809dfb;
477d7d1fabbd284885ea31c496be0236f23c63b7e9ff8792be58aee9bc354ceb;
1e670ab99a47e6ca25f51b85762250269ae3c076c0d1be70ce3d3473513ebc27;
c342d12d7f54dff6eff086272013d3c676e6aaf9671dbb81960f263e3725d16b.

Committed helpers: pull_readd_evidence.py, replay_readd_evidence.py and
pull_readd_holes.py. The oracle is the unmodified session-sliced
backtest.atr_oracle.compute_atr_trail, period5/factor3.5. It is continuous
over the stored bars, not proof that those bars equal an external chart.
Stored bars can be revised; their exact historical revision availability
is not reconstructed for this broad census. RETO's late-created bars are
explicitly decision-time unavailable, not silently inserted into live input.

Fresh-age means log time minus bar-start time is 0..180 seconds. A final
warmup bar can satisfy this filter; these are logged observations, not all
new live signals. Matching the same bar does not prove a fill or an entry
that would survive every other gate.

## R1-R5

| Claim | Verdict | Own evidence / correction |
|---|---|---|
| R1 re-add hole and line mismatch | AGREE | 11:06:04.557 hydrated216 bars, last seed10:48; 11:06:06.050 processes11:05 with replay gap17min. First reading bot2.000258 vs oracle1.9629. The cited2.0792/2.0639 pair is the11:09 bar, read11:10:03.191, not the first11:05 reading. |
| R2 delayed history ignored | AGREE | All16 bars10:49..11:04 source=rest, created11:10:19.672210. That is253.622s after the first11:05 reading. Later probes retain the unrepaired line; REST skip/out-of-order guards explain why. |
| R3 bot-only SELL / first rest | AGREE | 11:18:02.820 logs SELL on11:17 bar: botshort2.399348, oraclelong2.0639. 11:21:03.180 first-slot rest trigger2.3632, limit2.3750. Stored Schwab intent rejected by policy; Webull intent skipped-before-submit/deferred. Not two accepted buys. |
| R4 old repeated class | AGREE | Own gap-marker counts09-22=713,10-01=1006,10-05=822 at the frozen cut. Deliberate gap clamp predates10-03; logs already exhibit it in September. |
| R5 MI unseen BUY | AGREE | Continuous11:31 bar flipsBUY, prior trail4.9249 -> offset price4.9495245. Actual11:32:01.890 probe for that bar is long/flipnone/trail4.028659. The later state converges; the missed transition is still real. |

## Retained Sweep

47,376/47,424 fresh-age probes have a defined same-bar oracle; 48 are
UNMEASURED. There are 990 state-mismatch observations and 166 bot-only
flip observations among1,191 measured flip observations (165 distinct
symbol/day/bar/direction mismatches). This is an observation census, not
166 distinct lost trades or a causal attribution to re-add alone.

1,123 primary `[V2-RESTING-PLACE]` attempts have a recent matching probe.
Twenty-three occur with the continuous stored-bar line long:
**15/15 reclaim attempts have the bot long too**, and are NOT counted as
false-short first entries. **8/1,108 first-slot attempts have botshort /
continuouslong**: MYSZ09-15; FTFT,JZXN,NAMI and three DLXY attempts09-16;
RETO10-05. These are attempts, not fills.
Exact first-slot cases: MYSZ09-15 11:18:02; FTFT09-16 11:01:02;
JZXN09-16 12:48:02; DLXY09-16 13:53:02,14:14:02,14:20:03;
NAMI09-16 14:14:17; RETO10-05 11:21:03, all ET.

Daily denominators below: adds/re-adds are **lower bounds from consecutive
fully visible watchlists**; names are truncated to five on273 of1,400
updates. The initial visible set and transitions through a truncated set
are not assumed. Re-add means seen earlier in the retained visible census,
not proven lifecycle identity across a process restart. Successful DB seed
exposures total524 and are a separate population, not the missing add count.
An exact all-episode rate is therefore UNMEASURED.

| Date | Proven adds/re-adds lower bound | Truncated updates | Initial/recovery holds | Gap lines | Bot-only/measured flip observations | Long-line places/all places |
|---|---:|---:|---:|---:|---:|---:|
|09-04 partial|0/0|0|0/0|7|0/0|0/0|
|09-05|0/0|0|0/0|0|0/0|0/0|
|09-08|31/21|0|0/0|1337|14/58|4/51|
|09-09|5/2|0|0/0|397|0/46|11/66|
|09-10|14/8|0|0/0|535|0/24|0/12|
|09-11|25/18|0|0/0|75|6/56|0/69|
|09-12|0/0|0|0/0|0|0/0|0/0|
|09-14|19/13|0|0/0|235|13/43|0/16|
|09-15|42/27|1|0/0|695|13/57|1/55|
|09-16|23/19|49|0/0|1199|13/86|6/114|
|09-17|28/14|41|0/0|826|30/111|0/86|
|09-18|36/26|1|0/0|1493|2/57|0/37|
|09-19|0/0|0|0/0|0|0/0|0/0|
|09-21|18/8|10|0/0|66|0/68|0/111|
|09-22|35/23|5|0/0|713|5/55|0/86|
|09-23|25/21|76|0/0|342|24/115|0/85|
|09-24 age-unit defect|33/27|59|40/0|146|5/95|0/90|
|09-25|47/38|28|0/0|626|10/77|0/55|
|09-26|0/0|0|0/0|0|0/0|0/0|
|09-28|47/35|0|8/6|2039|2/20|0/21|
|09-29|23/20|0|3/0|157|2/38|0/40|
|09-30|38/28|0|5/2|805|7/65|0/51|
|10-01|29/22|0|3/8|1006|9/59|0/22|
|10-02|27/23|1|4/0|340|9/46|0/51|
|10-03|0/0|0|0/0|0|0/0|0/0|
|10-05 through11:50|15/11|2|7/9|822|2/15|1/5|

Fresh hold census has70 initial holds,40 on the excluded09-24 defect day.
The30 since09-28 split **12 offline zero-tape /18 prints-in-hole**. The
earlier frozen27 remains9/18; addedMI10:36,AGMH10:47,APUS11:05 each has0/0
on the reviewer's last-bar-close..detect-minus10s interval. Positive counts
remain18, including EGG0Schwab/10capture and0/18. Offline absence does NOT
certify full live coverage or the final ten seconds. The prior27-row state
table and13-entry controls remain in ASSESSMENT_2026-10-05.md; the added
three safe live classifications/state changes are UNMEASURED until coverage
is instrumented. No retrospective count is relabelled as decision-time proof.

## Cause, Blocking And Restart Shape

Code references are against maina80b5181 and the unchanged deployed path.
`_apply_strategy_state_event_async` drops departed state, prunes `_db_seeded`
and warmup, and replays DB state for a re-add. `_seed_symbol_from_db` reads
at most250 bars and sets seeded on successful replay/empty result. This
is a seed-read success, not a proof of no same-session holes. The session-skip
truncation is valuable and must remain: no stale prior-market arms.

`_mark_warmed_from_fresh_bar` accepts a bar within300 seconds of wall clock
from REST **or streamer**. `_handle_bar_from_streamer` drains and caps replay
once that freshness condition holds. Thus re-add admission waits for recent
data plus existing gates, **not for the missing interior bars**.

`_should_skip_rest_strategy_feed` skips REST timestamps <= streamer cursor;
`on_bar` also rejects timestamps older than the deque tail. The REST client
can save missing history while neither path inserts it into the indicator.
The90s true-range clamp suppresses cross-hole prior-close movement; it does
not reconstruct Wilder history. RETO exhibits both mechanisms independently
of the later gap-hold reset class.

A process restart re-enters the same250-bar DB seed, current-source warmup,
boot-cap and skip machinery. Shape AGREE; an exhaustive historical restart
incidence denominator is UNMEASURED, not invented from subscription SUBS.
Additional finding: the250-bar cap itself cannot promise bit-exact04:00
session mathematics for a dense session longer than250 bars. The new
rebuild must explicitly address its session anchor/history coverage, not
just prepend the16 RETO bars into already-mutated live state.

## Safe Build Split And Exit Scope

Pause lane: bounded dual-input evidence with subscription/connection epochs,
loss/backlog/overflow invalidation and saved first-return boundary; carry
prior-close TR only for the exact proven-silent pair. Current v2 retains
latest Schwab REST/stream print clocks, not the independent capture history.
Both existing clocks are insufficient for the two EGG controls. A reliable
live independent-input coverage contract must be implemented and pinned;
mere healthy heartbeat/zero offline rows is insufficient. UNKNOWN resets.

Restoration lane: versioned, ordered session rebuild off the trading callback,
no buys while incomplete; publish the corrected mathematical snapshot only
when its source/coverage and current bar are proven, without resurrecting
cancelled orders or unconsuming an entry. Restart, re-add and late backfill
need the same admission invariant. No historical flip is emitted late.
These concerns split cleanly but must compose; both flags defaultOFF.

Both confirmation and ATR SELL read the same corrected line. This is an
accepted intentional exit-evidence change, **not exit-neutral**. For original
MI K-T7, missing09:35 confirmation target remains unanswerable under either
line; no synthetic negative or confirmation decision is invented. The prior
raw exit read and continuous09:39 long state remain the evidence. Later
continuous11:31 BUY does not recreate an already ended position.

Preserve BENF sparse-bar reset, missing/unknown reset/clamp, clean2xperiod
wait, cancellation of both legs, unresolved broker ownership, consumed flips,
boot hold and DB session skip. Flag-off must be inert. No new buy admission
or changed confirmation scheduling is authorized by this assessment.

Verification: final helper Ruff PASS; all final SQL queries succeeded;
unchanged gap_hold, gap_hold_prints and atr_bar_gap suites15PASS, including
the BENF sparse-bar control. These tests certify unchanged baseline controls,
not a new implementation or historical decision-coverage proof.

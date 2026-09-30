# Row 38: independent Webull resting-mirror reassessment (2026-09-30)

Assessment only. No trading, gateway, cron, OMS, flag, or production change was made.
The production checkout was `3389090a7d30bdc88736a53211d968f4c82f0288`; the
gateway process was PID 2202865 and market-capture PID 2202817 when read.

## Calls against the proposed two-cause explanation

| Claim | Call | Evidence and limit |
| --- | --- | --- |
| Five-minute trade delay pattern | CONFIRMED | Exchange-to-Redis lag >2 seconds occurred only in five-minute boundary minutes in two separately extracted, overlapping stream samples. This is a timing observation, not attribution to cron. |
| Cron is the cause of that delay | UNKNOWN | Cron starts cluster at `:01`; a live boundary produced CPU run-queue contention. That same boundary had no trades delayed over two seconds. There is no independent vendor-receipt timestamp. |
| The gateway loop can prefer quotes over queued trades | CONFIRMED as a synthetic mechanism, NOT as the production root cause | The current production loop publishes one trade, then drains the entire quote queue. A test-only virtual-clock run at 300 quotes/s and 50 trades/s, with 3.2 ms per publish, gives trade lag above three seconds while quote lag remains below one second. |
| Stable NBBO / sparse prints explains a subset | CONSISTENT for 10/49; causal attribution UNKNOWN | Ten abandons had no captured exchange-timed print or published quote in the prior two seconds and only one published NBBO state in the prior ten. A missing captured print is not proof the vendor was quiet; capture consumes the gateway stream. |
| C1 versus C2 accounts for all abandons | REFUTED as the specified exhaustive mechanism; root cause UNKNOWN | Six decisions had a positive-ask gateway quote published within two seconds before intent creation. Yet none of the rejected broker events carried an OMS market stamp. Gateway trade-queue delay and a quiet NBBO alone do not explain why those published asks were unavailable to OMS. |
| Harm to hard-stop or PA1 exits | No affected exit established; missed-evaluation harm UNKNOWN | Code invokes hard-stop and PA1 evaluation on **both quote and trade** events. Three held rows overlapped proven 09-30 stall minutes, but no hard-stop/PA1 trigger is logged in those windows. Unlogged evaluations cannot be counted as safe. |

## Direct reproduction

The Claude-1 stream dump at
`/private/tmp/claude-502/-Users-velkris/9b59bc17-8b7a-4dde-be74-ff74d9edd1d0/scratchpad/row38_rootcause/md_full.txt`
covers 2026-09-30 14:38:53-15:18:06 ET, 69,410 trade and 30,592 quote records.
`analysis/row38_trade_lag.py` independently computed exchange timestamp to Redis
stream ID lag on its 69,410/69,410 timestamped trades: p50 0.111 s, p99 2.015 s,
max 7.152 s. Of 12,377 trades published in minute mod 5 = 0, 702 (5.67%)
exceeded 2 s; of 57,033 trades in the other four minute classes, **0** did.

A separately captured read-only dump at
`/tmp/row38-codex-market-data-20260930-1930Z.txt.gz` covers
14:53:29-15:30:19 ET, 70,222 trade and 29,786 quote records. All 70,222
trades carried a source timestamp. Its p50 was 0.111 s, p99 2.332 s, max
7.152 s; 855/13,268 (6.44%) mod-0 trades exceeded 2 s, versus 0/56,954
in the other minute classes. The changed p99/share reflects the overlapping,
not identical, 100,000-event retention window. `timestamp_ns` is a misleading
field name here: Massive live values are milliseconds, as documented by
`src/project_mai_tai/market_data/tick_time.py`.

This metric includes exchange/vendor/network time as well as gateway queue and
publish time. `produced_at` is assigned when the gateway publishes; OMS stores
that value in `_latest_quotes_by_symbol` / `_latest_trades_by_symbol`. The
exchange-to-stream lag is **not** itself the OMS two-second age.

For TNON at 15:00:06.455 ET, the Webull mirror logged
`shape=abandoned_no_fresh_quote` and the broker refusal. The last TNON quote
published before it was at 14:59:59.231 ET (bid 4.03, ask 4.04); the next was
15:00:09.399. TNON trades timestamped 15:00:04.334 onward reached the Redis
stream starting 15:00:09.962. Thus no fresh *published* TNON event existed at
the decision, despite exchange-timed prints. Whether the late arrival was at
Massive or inside our gateway is unproven.

## Cron / CPU cross-check

The `/var/log` cron journal starts 15-20 commands at `:01` on each measured
five-minute boundary (versus four on other minutes). In the independent live
15:35 ET sample, 16 commands started at `:01`; the run queue peaked at 23 at
`:02` on a host reporting **four online CPUs** (`nproc=4`, `pidstat` header),
and gateway `%wait` reached 55.45% at `:02`. Load1 rose from 1.35 to 2.17 by
`:06`. Yet the 15:35:00-15:35:30 Redis slice had 928 trade records, p99
1.787 s, maximum 1.864 s, and **0** over 2 s. This falsifies the stronger
claim that a cron start alone necessarily produces a >2 s trade stall.

Raw: `/tmp/row38-cron-window.log`, `/tmp/row38-pidstat-1935Z.txt`,
`/tmp/row38-sar-1935Z.txt`, `/tmp/row38-gateway-1935Z.txt`, and
`/tmp/row38-gateway-1935Z-summary.json`. At 15:35 the Redis SLOWLOG contained
no operation over its configured 10 ms threshold since 15:30:20; at 15:00:04
it did show a 23 ms XREAD and at 15:00:09 a 16 ms snapshot XADD, not a
multi-second Redis command. Raw: `/tmp/row38-redis-slowlog-1935Z.txt`.

## Independent-source and code checks

`market_capture_trades.received_at` is **not** independent Massive receipt
time. The running `market-capture` unit executes `mai-tai-market-capture`;
production `market_capture_app.py` explicitly consumes the shared
`mai_tai:market-data` stream and batch-flushes rows to Postgres. In the TNON
case the 13 captured prints were written at 15:00:10.592-10.593, after the
gateway's 15:00:09.962-10.151 publication. This table can establish print
presence, not vendor-vs-gateway attribution.

Production `gateway.py:_stream_publish_loop` publishes one trade per cycle,
then drains all live bars and quotes. Production `oms/service.py` prefers a
fresh ask, then a fresh last trade, with a 2,000 ms age cap for the Webull
resting mirror. `broker_adapters/webull.py` repeats the age check at the wire
boundary. A snapshot stamped fresh by OMS can therefore expire before wire
submission in principle, but **none of the 49 rejected broker events carried
an OMS market stamp**. By contrast, 464/464 unchanged and 3/3 converted
orders carried the stamp and wire-age fields. The observed rejection population
does not support in-flight expiry as its cause. Six rejected cases had a
positive-ask quote published within two seconds of intent creation (6/6 quotes
checked); whether the OMS consumer had processed those quotes at that moment
is UNKNOWN. Raw ask check: `/tmp/row38-valid-asks.txt`. PA1 resubmits and
hard-stop evaluation run after **both** quote and trade updates, not trades
only. Only PRICE_AGGRESSIVE creates PA1 deferred
state; `NO_FRESH_QUOTE` is not retried by that mechanism until a later slot
event.

## Population and harm census

The retained OMS log extraction, including the uncompressed
`oms.log-20260930` rotation, contained 516 mirror-lag outcomes from 09-17
through 09-30 15:43 ET (the last such line at capture):
464 unchanged, 3 converted, and **49 abandoned-no-fresh-quote** (49/516,
9.50%). The 49 span 24 symbols and split 25 at minute mod 5 = 0, 24 in the
other four minute classes. All 49 log cases joined to a rejected Webull
mirror intent by symbol, exact `fanout_slot_id`, and creation time; no case
was inferred from a nearby order. Classification at *intent creation* rather
than later refusal time is:

| Prior 2 s at intent creation | Cases / 49 | Five-minute boundary / class |
| --- | ---: | ---: |
| Exchange-timed prints, no published quote | 21 | 13/21 |
| Neither a captured print nor published quote | 22 | 6/22 |
| Prints and published quote | 5 | 5/5 |
| Published quote, no captured print | 1 | 1/1 |

Of the 22 neither-event cases, 10 had exactly one published NBBO state in the
prior ten seconds (2 at five-minute boundaries): strict *C2-consistent*
candidates, **not** verified vendor quietness. Thirteen boundary-minute cases
had captured exchange-timed prints and no published quote: *C1-timing*
candidates, not proof the gateway rather than vendor delayed them. The TNON
15:00 case has independent raw stream proof that exchange-timed prints were
published **after** the refusal; it still does not locate the upstream delay.
The remaining 26 cannot be assigned to either cause. In particular, the five
print-and-quote and one quote-only cases require OMS-consumption/wire evidence
not present in these rows. A captured trade's `event_ts` is exchange time and
may predate its actual gateway publication; a quote's `event_ts` is gateway
`produced_at`. Do not equate either with independent vendor receipt.

Read-only raw and joins: `/tmp/row38-oms-all-through-close.log`,
`/tmp/row38-abandons-all-query.sql`, `/tmp/row38-abandons-all-capture.csv`,
and `/tmp/row38-db-abandon-orders.txt`. `analysis/row38_abandon_population.py`
and `analysis/row38_classify_abandons.py` specify the exact query and
classification.

There were 61 distinct logged PA1 `decision=queued` events in the retained
09-21..09-30 feature window: 11 at five-minute boundaries, 5 in seconds
`:02-:11` of those minutes. Their historical raw stream segments have aged
out, so those five are **not** verified feed-stall exposures. The 09-30 raw
stall window (14:45-15:15 ET) overlapped three managed position rows (TGE
Schwab and GOW on both brokers), while TNON entered after 15:07 and closed
before 15:10. The filtered OMS log for those names shows no hard-stop or PA1
trigger inside a proven stall; TGE and GOW later have exact broker-child exit
fills recorded. This is **zero observed hard-stop/PA1 decisions inside the
proven 09-30 stalls**, not evidence that missed decisions were impossible.
No `[HARD-STOP TRIGGERED]` line was found in the retained PA1/hard-stop
extraction, so the number of unlogged missed evaluations remains UNKNOWN.
Raw: `/tmp/row38-exit-pa1-unique.log`, `/tmp/row38-held-stall-oms.log`.

## Decision boundary

Do not build gateway fairness, stagger cron, or replace the 2-second abandon
with a hold/retry on this report. The operator should first rule on whether
the residual UNKNOWN attribution and the measured population justify a
specific card. Nothing in this report authorizes a live change.

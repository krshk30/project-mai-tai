# RPG1 independent assessment, 2026-10-02

Later build status: the operator selected dedicated cancel/strict-readback for
both brokers, not native replace. The first-entry quote wait and standalone
BUY readback methods are built; the immediate re-place handoff remains
incomplete. See REPRICE_HANDOFF_REVIEW.md for current tests, recordings, NFQ1
composition and explicit remaining limits. The original assessment below is
retained as the evidence/design history, not the latest implementation status.

## Call and boundary

Issue: **AGREE, real defect against the approved reprice card**. The strategy
cancels for reprice and returns; placing again requires another completed-bar
pass. This is not a claim that the broker took a minute to cancel.

Implementation safety/seconds-level broker outcome: **UNMEASURED**. Native
replacement is the candidate, not yet an approved implementation choice. The
required recorded fill/partial-fill-during-replace responses are missing. Do not
invent them, assume an HTTP acknowledgement proves a working replacement, or
place first while an old buy may still fill. Direct-cancel-confirm latency is
also unmeasured. Runtime RPG1 changes stop at this specific Step-0 evidence
boundary; NFQ1 runs independently on `codex/nfq1-mirror-fresh-price`.

Both branches started from `b8b0dafbdf583ca4af9eddc8dbf922cbf887a482`. Neither may
merge before the separate October 2 installation is journaled. No production
orders, cancellations, replacement probes, Redis reads/writes or service changes
were performed for this assessment.

## Broker answer (Step 0b)

| Broker | Independently established | Not established |
| --- | --- | --- |
| Schwab | The adapter already implements an ORB-only native PUT replacement of a STOP_LIMIT TRIGGER/OCO parent. It fetches before/after, retains an uncertain replacement handle, and does not regard a receipt as a proven working shape. The ATR path does not use it. Cancellation already follows DELETE with a direct GET. | No retained replacement-receipt events found by the bounded query below; no recorded ATR replace response during a fill/partial fill, nor a measured direct-confirm latency distribution. ORB's fixed two-share assumptions cannot simply be copied into dollar-sized ATR orders. |
| Webull | The installed SDK has a stock replace endpoint and V3 `replace_order`; the official US stock documentation supports modify while open. Our current cancel returns `accepted` pending order-detail confirmation, not `cancelled`. The production OMS sync interval is 15 seconds. | No recorded US bare BUY STOP_LIMIT modify response during a fill/partial fill. The explicit STOP_LOSS_LIMIT replacement example in the current official SDK is **HK**, not US proof. No direct-confirm latency measurement. |

AMOD October 2: accepted cancel request to locally observed cancelled report was
11.167764 s at 15:14 and 11.624764 s at 15:17 ET. Webull's unfilled status adapter
uses local observation time when no fill time exists. These intervals therefore
do not establish an 11-second venue cancellation time or a two-second direct
confirmation guarantee. The initial accepted answer must remain pending.

Code at the assessment base: `broker_adapters/schwab.py:1591` native replacement,
`:1088` cancellation confirmation; `broker_adapters/webull.py:1109` cancel receipt,
`:893` order polling; `oms/service.py:13318` ORB replacement reconciliation;
`strategy_core/schwab_1m_v2.py:5363` cancel-and-return and `:5275` later placement.

Primary references (read October 2):
- [Webull US stock order lifecycle](https://developer.webull.com/apis/docs/trade-api/stock/).
- [Webull replacement reference](https://developer.webull.com/apis/docs/reference/common-order-replace/): its explicit stop-limit mutable-fields list is under **futures**, not evidence of US equity race semantics.
- [Official SDK V3 example](https://github.com/webull-inc/webull-openapi-python-sdk/blob/main/samples/trade/trade_client_v3.py): STOP_LOSS_LIMIT example is marked HK and uses market=HK.

Needed to finish this decision: recorded responses for the exact US order shapes
under normal replacement, fill race, partial-fill race, rejection and unreadable
confirmation, or an operator-authorized evidence exercise/ruling. No live broker
write is authorized by this assessment. A safe fallback must retain the old
order identity and block a new buy while its execution state is unknown.

## Reprice denominator (Step 0a)

Fresh own pull: all retained `schwab-1m-v2.log*`, parsed timestamp prefixes,
one strategy `V2-RESTING-CANCEL reason=reprice` per event, paired to the next
`V2-RESTING-PLACE` for the symbol. Webull's mirrored log is not counted twice.
Cutoff fixed at October 2 15:25 ET. Cross-day and unpaired events remain visible.

| Population | Pairs | <90 s | 90-150 s | >=150 s | <10 s |
| --- | ---: | ---: | ---: | ---: | ---: |
| Stated Sep 18-Oct 2, same day | 477 | 432 | 22 | 23 | 0 |
| All retained Sep 2-Oct 2, same day | 866 | 786 | 39 | 41 | 0 |
| All retained, additionally gap <600 s | **848** | **786** | **39** | **23** | **0** |

Thus the reviewer histogram is exactly reproducible with the third definition,
not the stated date range. This is a reconstruction of its filtering, not proof
of which script the reviewer ran. The stated date range has 489 cancellations:
477 same-day pairs, 2 cross-day pairs and 10 with no later place. Median of its
477 pairs is 60.112 s; minimum 57.397 s. All retained has 884 cancellations,
866 same-day pairs, 4 cross-day pairs and 14 unpaired.

Of the 62 longer pairs in the matching <600 s population, 28 include explicit
stale-quote holds and 29 include short bars with volume <=10,000; five overlap.
Ten have neither observation. These are evidence tags, not exclusive causal
claims: historical floor settings and silent early-return state still require
per-case reconstruction. BENF/QCLS/DLXY cases contain five-minute bar gaps, so
waiting for the next bar can itself exceed a minute. A current 10,000 floor is
verified from v2's running environment; it is not silently projected onto every
historical run. The analysis retains all 62 case paths/line numbers locally.

Verified October 2 examples:
- AMOD 13:37:02 -> 13:39:03: at 13:38:02 both account checks explicitly refuse a
  12,325 ms quote against 10,000 ms. This extra minute must not be 'fixed' by
  bypassing freshness.
- AIXI 12:16:02 -> 12:20:02: two thin bars (8,514 / 8,684), then a stale-quote
  hold; AIXI 15:06 -> 15:08 has volume 9,048; 15:09 -> 15:13 has
  2,434 / 1,888 / 7,970. Fresh-placement gates explain the added waits.
- AMOD 13:56:02 -> 15:04:03 is **not** a continuous 68-minute replacement:
  BUY at 13:57, SELL at 15:01, and a new established-short segment precede the
  next rest. This missed flip is excluded by the reviewer's reconstructed
  <600 s filter and must remain a dedicated regression case.

The replacement design must carry the exact slot/segment and intended price,
keep it tracked through confirmation, and recheck session, liquidity, price,
fills and composition before any fresh placement. It cannot promise an order
when an existing safety gate legitimately disallows entry.

## Flip sweep (Step 0d)

Own bar-cohort reconstruction matches **205/205** reviewer RTH flips exactly,
with zero extra/missing keys, across 19 sessions Sep 8-Oct 2. The key is the
bar's `ts_ms` minute, not the log's observation minute. First observation must
be within 120 s of bar start; duplicates/replays are collapsed. Also reproduced
30 pre-market flips, not classified here.

Own bounded fills query returns 297 buy fill rows for the two live accounts.
Using last SELL through BUY-bar close +30 s gives 129 traded cycles:
83 both / 37 Webull-only / 9 Schwab-only. The remaining reviewer traded case is
YMAT Sep 9 14:29: a **reactive** Webull fill at 14:32:09.601, not 'at flip'.
Counting that later confirmation gives the reported 130, but its timing must be
stated. Extending every cycle all the way to the next SELL yields 133 and four
account-attribution differences; that broader window is not an exact-flip test.

The reviewer's complete 75-case non-fill causal classification is **not yet
independently verified**. Cohort agreement is not causal-class agreement. In
particular, the eight 'untraced' cases remain untraced, not silently assigned to
RPG1/NFQ1. Local scripts preserve narrow and broad attribution separately.

## Evidence and reproducibility

Local evidence root: `/Users/velkris/.codex/study-evidence/rpg1-nfq1-20261002/`.
Remote originals: `/var/log/project-mai-tai/schwab-1m-v2.log*`, PostgreSQL
`fills`, `broker_orders`, `broker_order_events`, `broker_accounts`, `strategies`;
reviewer comparison only: `/tmp/sweep4.json`. Pulls used nice/idle I/O and one
read-only database connection, 10-second statement / 1-second lock timeout.
No snapshot-batches reads.

| Local input | SHA256 |
| --- | --- |
| v3-v2-logs.ndjson | 20a0c752d35fba59f44456ad4a5a049986f54a252d595ac24b04a8a356f1006e |
| v3-all-retained-v2.ndjson | 69dadda069c8a81df3e77d5e636be8412d3e15338a31dcae403b343e0b6db45a |
| sweep-fills.json | 5c3d9dbc807353b4593120397125b895c8215af7cbf095b5756414aaeb21e44d |
| reviewer-sweep4.json | 9590227a97ec5e8f9651219ff95549b8a64d1861532d29ebe529c98d467dea5f |

Run `analyze_reprice_logs.py` with default Sep 18 bounds, then `--start
2026-09-01T00:00:00-04:00` for all retained. Run `analyze_flip_cohort.py` with the
log, fill, and reviewer JSON paths. Outputs: `v3-gap-assessment.json`,
`v3-all-retained-gap-assessment.json`, `v3-flip-sweep.json` in the evidence root.

Replacement receipt check: read-only `broker_order_events` since Aug 14 where
payload contains `replaced_broker_order_id` or `replacement_receipt_only`, limit
100: zero rows. This only establishes absence in our persisted receipt surface,
not that Schwab lacks replacement support. Webull recorded cancel reports are
in `db.ndjson` / `amod_events`; poll observation time is not broker cancel time.

Build state: Step-0 tooling/report only; no RPG1 runtime patch or fabricated
broker fixtures. No claim of tested replacement safety or latency is made.

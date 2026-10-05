# PMPRINT1 independent incident assessment

The SAIQ causes match the reviewer: one high last-trade print crossed the trigger while the ask remained below it; Webull lost its ask before dollar sizing. No fill or managed position resulted. **BUILD STOPPED pending the operator's strict-lower-check ruling.** The reviewer clarified that the five-second path must not acquire a new upper cap: the lower-only alternative leaves 14/14 first-leg price proxies eligible and blocks 5/5 trigger-reaching outliers; decision-cache timing remains UNMEASURED. CLRO's raw second-print ask differs from the existing test's supplied ask and would be blocked. This is an assessment, not a fix or deployment approval.

## Independence limitation and build gate

Codex-2 read the entire supplied attachment before splitting its sections, and had already reproduced the stream-path Webull ask-zero defect during PMREST1. These answers therefore **cannot be described as blind**. They are independently replicated from fresh production reads and the deployed source. The operator accepted this disclosure: "Accept disclosed independent replication." That acceptance does not waive the measured preservation gate. No runtime change has been made.

The reviewer claims and proposed ask-confirmation rule will be compared below after these answers. Any disagreement or inability to meet the 14-real-cross / 5-triggered-stray replay gate blocks a build. Missing instrumentation is UNMEASURED, not a successful replay.

## Independently measured answers

Assessment source: clean production HEAD `e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89`; v2 PID 26811, OMS PID 23705. Fresh SSH database/log read completed approximately 08:35 ET 2026-10-05. Times below are ET, with millisecond precision; database sub-millisecond values remain in the raw evidence.

### Q1 Why Schwab received a buy without a chart flip

**Cause:** a software rest can fire on a single last-trade print reaching its trigger; neither the streamed-price path nor its shared cross check requires the ask to have reached that trigger.

| Recorded event | Time ET | Measured content |
| --- | --- | --- |
| Software rest armed | 08:08:02.896 | line 6.7590, trigger 6.7928, first slot |
| Previous Schwab print | 08:10:34.569 | 6.14 |
| Triggering Schwab print | 08:10:35.281 | price 6.83, size 1, raw bid 6.14 / ask 6.24; trade id 6790350 |
| v2 cross decision | 08:10:35.817 | px 6.83, trigger 6.7928, cap 6.8267, logged print age 535 ms |
| Original intent published | 08:10:35.824 | 97 shares at cached REST ask 6.19; Redis id 1791202235824-0 |
| OMS repriced | 08:10:35.931 | own ask 6.24, trigger 6.7928, cap 6.8268, limit 6.24 |
| Next Schwab print | 08:10:36.569 | 6.07 |
| Broker order submitted | 08:10:36.267 | 96 shares; row 0e0003e7-d405-42d7-9e4e-cc6714c34669 |
| Broker rejection stored | 08:10:37.303 | Opening transactions for this security must be placed with a broker |
| 08:10 completed bot bar | 08:10 minute | high 6.25, below trigger |

The print is 1/404 Schwab trade rows at or above 6.75 in 08:05-08:13 ET. No SAIQ fill or managed position was found after 08:00 ET (0/1 submitted order filled). A resting-trigger buy is not itself proof of a completed ATR-bar flip.

**Code path at deployed HEAD:** `strategy_core/schwab_1m_v2.py:3178` receives the streamed print; `:3210-3239` checks print freshness and the upper ask cap, not an ask lower bound; `:3240-3251` calls `_eh_resting_cross_check`; `:5993-6000` accepts last price >= trigger; `:6011` takes the settle latch. `services/schwab_1m_v2_bot.py:4864-4875` supplies the valid cached stream ask. The REST-quote cross uses the same `_eh_resting_cross_check`, so this is not limited to streaming.

**Why 97 became 96:** the original retained intent, not the mutated database payload, records price 6.19 and quantity 97. Extended-hours routing reads the separate REST quote cache (`strategy_core/entry_gate.py:106-120`), then `services/schwab_1m_v2_bot.py:5321-5342` recomputes quantity from its routed limit. Nearest whole shares: 600/6.19 = 96.9305 -> 97. OMS uses its own 6.24 ask and finalizes again (`oms/service.py:14259-14274`, sizing `:12539`): 600/6.24 = 96.1538 -> 96. The initial strategy cap-based draft would be 88 shares; it is not the emitted quantity.

**Earlier occurrence:** TOPS 09-22 09:28:44.274 ET also crossed with the first OMS ask below its trigger (1.30 versus 1.3490, -3.632%). The cross log used last price 1.39. One-share print outliers appear at 09:28:46.120 and 09:29:16.922 with surrounding prices 1.28/1.29. Those later prints are not the exact quote sample that caused the earlier REST decision. The retained OMS-priced cohort has 2/16 below-trigger first asks, TOPS and SAIQ; 14/16 are above the trigger. These are attempts, not 16 fills.

### Condition 3: Alternative Explanations for SAIQ 6.83

A genuine trade at another venue or a late/out-of-sequence report remains possible; neither is ruled out by the surrounding low NBBO or the completed bar. A fresh bounded own query found Massive-derived capture row 185338442: price 6.83, size 1, event timestamp 2026-10-05T12:10:35.281Z, exactly matching Schwab row 6790350. Schwab raw fields 3/9/35 contain that last price, size and millisecond timestamp, and the decoder reads those fields directly: our bid/ask-as-last decoding and local timestamp conversion do not explain this print. Cross-provider agreement does not rule out an upstream timestamp/reporting error or prove a current, bar-eligible execution. The 42/45 same-priced outlier-window matches strengthen a feed-level origin, not an execution-identity claim: market_capture_trades is Massive-derived gateway capture, not another independently connected recorder, and lacks raw execution IDs and participant-versus-SIP timestamps. SAIQ's captured exchange 17 and conditions 12,37 are retained but not interpreted here; venue validity and late/out-of-sequence eligibility remain UNMEASURED.

### Q2 Why Webull received no order

**Cause:** the streamed cross builds a synthetic Quote with `ask_price=0`, so Webull's dollar-sizing helper refuses the draft even though a valid ask was supplied to the stream gate.

At 08:10:35.818 ET the log records `[V2-ENTRY-SIZE-REFUSED] symbol=SAIQ leg=webull price_basis=trigger_quote_ask price=0.0`. Code: `strategy_core/schwab_1m_v2.py:3240-3247` zeros bid/ask; `:6040-6057` passes that Quote to Webull; `:6214-6221` sizes from its ask and returns no draft. Schwab still emitted at 08:10:35.824. The fresh database pull found 1 SAIQ intent and 1 broker-order row, both on live:schwab_1m_v2; zero live:orb rows. The Schwab policy rejection happens later, so it did not cause the missing Webull draft.

**Newness:** the ask-dropping shape predates dollar sizing. A positive dollar amount makes the zero ask a sizing refusal; with amount zero the legacy fixed-share helper does not require that price. Four retained stream-cross markers were found: MEDS 10-01 07:57:53.875, NXL 10-01 09:16:49.828, AMOD 10-02 08:32:34.930, SAIQ 10-05 08:10:35.818 ET. Only SAIQ is after sizing deployment: 1/1 observed post-sizing stream crosses lost its Webull draft to zero-ask sizing. This small exercised denominator is separate from the code-level structural defect.

## Claim Comparison

| Claim | Verdict | Own evidence and qualification |
| --- | --- | --- |
| C1 | AGREE | Arm 08:08:02.896, line 6.7590 / trigger 6.7928; SAIQ ATR-PROBE records through the incident are short / flip=none; 08:10 bar high 6.25. A software-rest trigger and a completed-bar flip are different events. |
| C2 | AGREE | Own 404-row window has exactly one print >=6.75: 6.83 size 1, 08:10:35.281, raw bid/ask 6.14/6.24, neighbours 6.14/6.07. |
| C3 | AGREE | Original retained intent is 97 at routed REST ask 6.19; OMS changes it to 96 at its 6.24 ask. One rejected broker row, zero fills/managed positions. |
| C4 | AGREE | Neither shared cross path requires ask >= trigger. Stream checks only freshness/upper cap before discarding bid/ask. Local unmodified-strategy reproduction emits a Schwab draft on the recorded print. |
| C5 | AGREE | Reproduction emits zero Webull drafts and the exact zero-ask sizing refusal. Observed post-sizing denominator 1/1, not an independently measured large population. |
| C6 | AGREE, scoped | 16 distinct primary OMS-priced attempts, 19 pricing lines including secondary legs; 14 first OMS asks above trigger, two below. Own search has 30 rotated files plus current file per service. There are 27 v2 cross markers, so the 16 priced attempts are a subset, not all attempts. LGHL's secondary Webull ask 7.00 is below 7.0149, despite its primary 7.04 ask being above; do not extend first-leg counts to both legs. |
| C7 | UNMEASURED as a causal claim | Counts replicate: 25 upward outliers / 9 names, 6 with an armed rest, 5 reaching trigger. The statement that QNME/WHLR were missed ONLY because of five-second polling is not proven: retained rows do not contain every decision-cache sample or every guard state. A missed stream event cannot be attributed exclusively to polling from these tables. |
| C8 | DISAGREE with literal state; operational silence confirmed | The cross sets resting_flip_ms, not resting_active=False. Local reproduction leaves resting_active=True while the settle latch blocks another entry. Actual DISARM is 08:12:02.425; re-arm 08:13:01.534. Entry silence from the cross to re-arm is 145.717 s. Saying the rest was literally DOWN for that whole interval confuses a latched armed rest with a disarmed one. |

Under the supplied gate, a C-claim disagreement also blocks a build. The C8 correction does not dispute the operator's loss-of-opportunity concern.

## Replay Method and Gate

Population frozen at 10-05 08:25 ET. Premarket is 07:00 <= time <09:30 ET. The outlier query partitions Schwab trades by symbol/service, ordered by event_ts/id; an upward row is >=1.05 times BOTH adjacent prices. This mechanical definition does not prove a vendor bad print: TURB's 2.9245 outlier had raw ask 2.92 and could be a genuine brief move.

Own census: 19 weekdays 09-09..10-05, 279,734 captured Schwab trade rows through the cutoff, 25 upward and 20 downward outliers. Missing capture outside this population is not zero. Day queries completed in 0.291-1.293 s with an 8 s timeout.

Three distinct prices must not be interchanged:

- The stream gate's ask cache uses positive raw field 2, including the triggering trade message, before dispatch. Its freshness clock is callback time, not the database flush time.
- The REST path / extended-hours routing uses the separate REST quote cache. Pre-sizing trade_intents retain that original routed limit; broker_orders show later OMS repricing. These original routed asks are useful evidence, but are not separately timestamped snapshots of the cross-call cache.
- The OMS logs its own later ask. Replaying solely that price would hide bot-side cap problems and would incorrectly certify 14/14.

Candidate predicate: print >= trigger AND fresh ask >= trigger AND ask <= trigger*1.005. No new tolerance was introduced. For stream events below, the event-time quote is a replay proxy; raw asks and callback ordering are examined separately. For REST events, the original routed ask is shown, not the future OMS ask.

| Decision ET | Symbol | Trigger | First OMS ask | Candidate ask/source | Upper cap | Price predicate only |
| --- | --- | --- | --- | --- | --- | --- |
| 09-09 07:45:41.519 | YMAT | 1.9975 | 2.00 | 2.01, original REST route | 2.0075 | BLOCK: ask above cap |
| 09-10 07:48:06.506 | TNON | 3.5454 | 3.56 | 3.56, original REST route | 3.5631 | eligible |
| 09-14 08:48:01.570 | SCNI | 2.3302 | 2.34 | 2.34, original REST route | 2.3419 | eligible |
| 09-15 08:53:09.771 | MYSZ | 2.4399 | 2.44 | 2.44, original REST route | 2.4521 | eligible |
| 09-17 08:50:46.048 | DAIC | 3.9849 | 3.99 | 3.99, original REST route | 4.0048 | eligible |
| 09-18 08:56:23.884 | IMCC | 3.1195 | 3.13 | 3.15, original REST route | 3.1351 | BLOCK: ask above cap |
| 09-21 09:22:49.623 | GLND | 3.0417 | 3.05 | 3.05, original REST route | 3.0569 | eligible |
| 09-22 08:05:39.730 | TOPS | 1.3453 | 1.35 | 1.35, original REST route | 1.3520 | eligible; stream quote absent in bounded window |
| 09-22 09:28:44.274 | TOPS | 1.3490 | 1.30 | 1.30, original REST route | 1.3557 | BLOCK: ask below trigger |
| 09-23 08:23:15.261 | DCOY | 4.3148 | 4.32 | 4.32, original REST route | 4.3364 | eligible |
| 09-23 09:22:44.740 | WHLR | 4.9325 | 4.94 | 4.94, original REST route | 4.9572 | eligible |
| 09-30 08:23:59.250 | LGHL | 7.0149 | 7.04 | 7.04, original REST route | 7.0500 | eligible |
| 10-01 07:57:53.874 | MEDS | 4.6438 | 4.66 | 4.66, stream quote | 4.6670 | eligible; original REST routing used 4.61 |
| 10-01 09:16:49.828 | NXL | 7.1412 | 7.16 | 7.16, stream quote | 7.1769 | eligible; original REST routing used 7.11 |
| 10-02 08:32:34.930 | AMOD | 2.5693 | 2.58 | 2.58, stream quote | 2.5821 | eligible; original REST routing used 2.57 |
| 10-05 08:10:35.817 | SAIQ | 6.7928 | 6.24 | 6.24, raw triggering stream message | 6.8268 | BLOCK: ask below trigger |

**Gate: NOT PASS.** The routed/event-time price replay has 12/14 real attempts eligible, two upper-cap refusals. It is not a proof that the full patched strategy would emit 12 entries: exact REST decision-cache timestamps and callback receipt order are unmeasured. Those missing records cannot turn the two counterexamples into a claimed 14/14 PASS. Same-moment, both-leg preservation remains UNMEASURED and the price-proxy test fails it.

Measured alternative, NOT approval: adding only an ask lower bound at the REST cross while retaining the existing downstream OMS cap leaves 14/14 first-leg real attempts price-eligible in these proxies. It avoids adding a new REST-side upper-cap refusal, but does not settle CLRO's low-ask brief cross or prove freshness/both-leg delivery. No price tolerance or new threshold is proposed without evidence.

**Reviewer clarification, 10-05 approximately 09:00 ET:** this lower-only alternative is the intended rule, not the originally replayed lower-plus-upper predicate. Preserve the stream path's existing upper-cap skip and the OMS's cap; add no upper cap to the five-second path. Price-proxy result: 14/14 real first-leg eligible, 5/5 trigger-reaching outliers blocked, CLRO second print blocked. Exact decision-cache timing and same-moment two-leg delivery remain UNMEASURED. The operator's answer on that strict rule, including the CLRO behavior change, is still pending; no build has begun.

### All 25 Upward Outliers

Size is Schwab last size when supplied; '-' is absent, not zero. The rest state is reconstructed from prior same-day ARM/DISARM records, not a persisted every-tick state snapshot.

| Time ET | Symbol | Print | Size | Adjacent prices | Armed trigger | Reaches trigger |
| --- | --- | --- | --- | --- | --- | --- |
| 09-17 08:33:07.819 | TURB | 2.9245 | - | 2.04 / 2.51 | not armed | no |
| 09-17 08:37:13.167 | TURB | 2.395 | 100 | 2.2785 / 2.23 | not armed | no |
| 09-22 08:54:46.223 | QNME | 1.55 | 1 | 1.4 / 1.4001 | 1.4363 | yes |
| 09-22 09:28:46.120 | TOPS | 1.37 | 1 | 1.28 / 1.29 | 1.3490 | yes |
| 09-22 09:29:16.922 | TOPS | 1.38 | 1 | 1.28 / 1.29 | 1.3490 | yes |
| 09-23 08:05:33.754 | BENF | 2.5699 | 765 | 2.36 / 2.38 | not armed | no |
| 09-23 09:14:35.011 | WHLR | 6.98 | 0 | 4.78 / 4.7801 | 4.9325 | yes |
| 09-25 08:59:36.552 | AIFF | 1.83 | 1 | 1.7386 / 1.7386 | not armed | no |
| 09-25 08:59:38.597 | AIFF | 1.83 | - | 1.7386 / 1.7386 | not armed | no |
| 09-25 09:00:39.855 | AIFF | 1.81 | 1 | 1.6986 / 1.6993 | not armed | no |
| 09-25 09:12:51.967 | AIFF | 1.93 | 1 | 1.6599 / 1.65 | not armed | no |
| 09-25 09:22:48.205 | AIFF | 1.77 | 1 | 1.6789 / 1.67 | not armed | no |
| 09-25 09:22:54.237 | AIFF | 1.76 | 1 | 1.675 / 1.6687 | not armed | no |
| 09-29 09:04:29.135 | BKYI | 2.8883 | 8 | 2.73 / 2.74 | not armed | no |
| 09-29 09:04:31.105 | BKYI | 2.88 | 234 | 2.74 / 2.66 | not armed | no |
| 09-29 09:05:35.409 | BKYI | 3.13 | 1 | 2.9183 / 2.9183 | not armed | no |
| 09-29 09:06:00.317 | BKYI | 3.17 | 2 | 2.9097 / 2.902 | not armed | no |
| 09-29 09:19:27.575 | BKYI | 3.23 | 1 | 3.0 / 3.002 | not armed | no |
| 09-29 09:19:29.458 | BKYI | 3.23 | 1 | 3.002 / 3.01 | not armed | no |
| 10-01 08:36:54.966 | VEEA | 3.21 | 2 | 3.01005 / 3.0185 | not armed | no |
| 10-01 08:36:59.921 | VEEA | 3.18 | 1 | 3.015 / 3.0002 | not armed | no |
| 10-01 08:37:57.029 | VEEA | 3.49 | - | 3.01 / 3.008 | not armed | no |
| 10-01 08:38:00.038 | VEEA | 3.48 | 1 | 3.01 / 3.0 | not armed | no |
| 10-05 08:10:29.531 | SAIQ | 6.54 | - | 6.2 / 6.16 | 6.7928 | no |
| 10-05 08:10:35.281 | SAIQ | 6.83 | 1 | 6.14 / 6.07 | 6.7928 | yes |

| Trigger-reaching outlier | Available ask evidence at/before print | Candidate result |
| --- | --- | --- |
| QNME 09-22 08:54:46.223 | quote 1.41 at 08:54:45.500, trigger 1.4363 | block: ask below |
| TOPS 09-22 09:28:46.120 | raw trade-message ask 1.29, trigger 1.3490 | block: ask below |
| TOPS 09-22 09:29:16.922 | quote 1.29 at 09:29:16.399, trigger 1.3490 | block: ask below |
| WHLR 09-23 09:14:35.011 | no positive Schwab quote captured in preceding 12 s; neighbours 4.78/4.7801, trigger 4.9325 | block: no proven fresh ask, not a fabricated 4.79 quote |
| SAIQ 10-05 08:10:35.281 | raw ask 6.24, trigger 6.7928 | block: ask below |

Five of five are blocked in this captured-stream price replay. WHLR's exact REST cache is missing; the full live delivery/freshness claim remains UNMEASURED. No print-size filter was used.

### Brief Crosses: CLRO and LGHL

CLRO 09-28 trigger 5.5590, cap 5.5868. At 07:24:58.121, print and raw ask are both 5.59: existing stream upper-cap guard already refuses it. At 07:24:59.992, print is 5.5689 but raw ask is **5.55**, below trigger. The existing test `test_clro_out_of_band_print_does_not_burn_the_in_band_second_print` supplies **5.57** for that second print. The service reads raw field 2 before offering the trade, so a full-message replay with 5.55 would update the cache and the proposed lower-bound rule would block. This is an additional preservation conflict, not permission to use a different quote silently. #1054 was not live for this 09-28 incident; the old test was a counterfactual replay, not proof of a fill.

LGHL 09-30 08:23:58.103: print/raw ask 7.07 exceeds the 7.0500 cap. At 08:23:59.137, raw ask 7.04 is in band, but print 7.00 is BELOW trigger 7.0149. The 08:23:59.250 cross used the REST path's different last price (logged 7.02), not that 7.00 stream print. Original routed ask is 7.04. A later 08:24:01.176 stream print is 7.04. The two feed/cache paths must not be collapsed into one invented confirming message.

## Session Assessments: No Exit or Reactive Build

### A1 Broker Resting Stops, Both Accounts

Own fill-linked resting STOP_LIMIT census 08-14..10-05: 501 orders, all first fills in 09:30-16:00 ET; 250 Schwab / 251 Webull. Each has a bot 1-minute high. Minimum fill price is compared with the actual wire stop where available. "Well below" is explicitly defined here as >0.5% below, not a trading-rule change.

| Population | Any fill below stop | >0.5% below stop | Fill-minute high below wire stop | Limitation |
| --- | --- | --- | --- | --- |
| Schwab, 250 wire-stop orders | 59/250 | 5/250 | 0/250 | Price improvement is not proof of a stray-trigger event |
| Webull, 119 recorded STOP_LOSS_LIMIT wires | 22/119 | 1/119 | 0/119 | Actual accepted-event wire metadata used |
| Webull, 129 missing wire metadata | UNMEASURED actual wire | UNMEASURED actual wire | UNMEASURED actual wire | Adapter rounding reconstruction gives 26 / 1 / 0, but is not a broker-wire observation |
| Webull, 3 converted LIMIT wires | N/A broker stop | N/A broker stop | N/A broker stop | Not stop-trigger orders after wire-shape conversion |

The five Schwab >0.5% cases: XOS 08-18 10:27:07 (stop 4.23 / fill 4.19 / bar high 4.23); TNON 08-19 13:04:00 (14.28 / 14.17 / 14.30); TNON 08-19 15:15:37 (11.98 / 11.85 / 12.07); LIDR 09-01 09:34:44 (1.64 / 1.63 / 1.64); NUR 09-08 12:05:44 (3.75 / 3.72 / 3.78). Webull: ZTG 09-16 15:59:00.418 (wire stop 1.19 / fill 1.1789 / high 1.23).

Naively comparing Webull's unrounded source stop with bar highs produced apparent misses; rounded wire stops / LIMIT conversion explain that comparison. It is not evidence of 34 broker bad-print triggers. The fill-linked denominator excludes orders without a joined fill; it must not be presented as every broker stop ever submitted. Whether an individual stray caused a broker trigger is UNMEASURED: no broker trigger-tick identity is retained.

### A2 Reactive Entry

`strategy_core/schwab_1m_v2.py:4530` returns early when flip-owned-first-entry is enabled. Own running v2 `/proc` confirms both reactive=true AND flip_owned_first_entry=true, so this reactive path is dormant today despite its own flag being on. If reachable in the legacy configuration, it uses quote.last_price (fallback bid/ask midpoint) and can act on one sampled last print after its arm/flat/reclaim/high guards; it does not require size or ask >= trigger. No change proposed here.

### A3 Software Exits

Among 25 upward outliers, 0/25 overlap a managed-position interval. Among 20 downward outliers, 2/20 overlap a position: the same AMOD Schwab trade. Buy fill 2 @2.58 at 08:32:35 on 10-02; sell fill 2 @2.7113 at 08:46:54. Outliers 08:44:37.544 @2.42 and 08:44:40.325 @2.43 (size 1 each). Both are above the -8% stop 2.3736: 0/2 qualifying hard-stop crosses, 0/2 SELL-order submissions in the bounded -12..+5 s window. The later exit must not be attributed to those earlier prints.

The gateway feed is distinct from the captured Schwab feed. Own bounded aggregate queries of market_capture_trades/quotes show those AMOD low prices on the Massive capture too (7 rows at 2.42 and 2 at 2.43 in -2..+5 s windows), with bids staying >=2.54 / >=2.55. Thus there is cross-provider evidence, not proof that the OMS processed each exact capture tick. Across all 45 outlier windows, 42 have a same-priced Massive capture trade; this does not establish identical conditions/tick identity.

| Exit path | Price consulted | One last-price print sufficient? | Measured result |
| --- | --- | --- | --- |
| CW managed target / hard stop / floor | fresh positive bid from quote handling; floor OFF live | No: last-only trade does not run this ladder | AMOD bid stayed above stop; 0/2 observed immediate exits |
| Confirmation / ATR bar exit | completed bar decision plus fresh bid in OMS | Not a lone last-price check | No exit caused by the two observed AMOD low outliers |
| ArmedHardStop fallback | fresh bid <= stop OR fresh last <= stop (`oms/service.py:10535` onward) | Yes, structurally; a low last can trigger even with bid above | Actual historical stray-triggered sell UNMEASURED: no exercised below-stop held outlier in this cohort |

Premarket observations do not certify after-hours exits: the fixed outlier population is 07:00-09:30. After-hours stray frequency / actual trigger attribution is UNMEASURED. No exit-path build is authorized by this assessment.

## Verification and Ending

Local unmodified-source SAIQ reproduction: recorded prices/bar and explicitly initialized flat armed state; Schwab draft yes (initial cap-sized 88 shares), Webull drafts 0, sizing refusal logged, resting_active=True / settle latch set. Extended-hours routing / OMS later resizing are proven separately by the original intent and order, not simulated by this direct strategy call.

Existing stream-cross unit file: **43 passed**. This is a regression check on current code, not PMPRINT1 acceptance. No T1-T11 implementation tests, mutation suite, or full-unit fix comparison are claimed; no source change exists. T1-T3/T10 causes are assessed, T4/T5 preservation is blocked, T8 distinguishes REST from stream, and T6/T7/T9/T11 await an approved build with a settled card.

Assessment branch: `codex/pmprint1-independent-assessment`, docs only, not merged. No PMPRINT1 runtime PR or install plan has been opened because the build gate is not met. PMREST1 #1091 remains separate and unchanged. Required next decision: reconcile the REST upper-cap counterexamples and CLRO raw-ask case before asserting same-moment preservation; do not loosen the rule or use a future OMS price without the operator's reviewed ruling.

## Raw evidence and read safety

- Durable local evidence directory: `/Users/velkris/.codex/pmprint1-evidence-20261005/`; raw fresh pulls, read scripts, analysis and local reproduction retained there. SHA256SUMS covers the saved files. Initial copies also remain under `/tmp/pmprint1-*`.
- Raw incident pull `pmprint1-own-read.json` (62 file paths: 31 OMS, 31 v2); original intent `pmprint1-own-intent.json`; historical ticks/state `pmprint1-own-history.json`; original routed intents `pmprint1-own-originals.json`; wire-aware RTH census `pmprint1-own-rth-wire.json`; no-payload gateway aggregates `pmprint1-own-gateway-aggregates.json`; SAIQ probe lines `pmprint1-own-saiq-probes.txt`; exact price/size/timestamp origin cross-check `pmprint1-own-origin-check.json` and its `pmprint1-origin-check.py` read script (READ ONLY, five-second timeout, LIMIT 10, one row returned).
- Remote sources: `/var/log/project-mai-tai/oms.log*`, `/var/log/project-mai-tai/schwab-1m-v2.log*`, `market_trade_ticks`, `market_quote_ticks`, `strategy_bar_history`, `trade_intents`, `broker_orders`, `fills`, `oms_managed_positions`.
- Original intent from `mai_tai:strategy-intents`, event bda3ad57-da92-4d69-b62c-2d5df0efc6ce; bounded XRANGE COUNT 25, found after three envelopes. No snapshot-batches read.
- Database queries were READ ONLY, statement timeout 8 s, per-symbol/time bounded. Tick `received_at` is database flush time, not callback time (`market_data/schwab_v2_tick_writer.py:99-137`); it cannot prove when a quote reached the bot. This distinction will be maintained in the replay.
- Initial market-capture windows were LIMIT 100 and often saturated. No absence was inferred from those truncated reads; subsequent COUNT/MIN/MAX/FILTER aggregate queries cover the full small windows without returning bulk ticks.
- No broker write, trading restart, environment/flag change, Redis write, or production file edit.

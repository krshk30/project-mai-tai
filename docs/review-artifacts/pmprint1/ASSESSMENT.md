# PMPRINT1 independent incident assessment

The bot attempted SAIQ because one high last-trade print crossed its software trigger while the ask remained well below it. Webull was not sent because the streamed path discarded its valid ask before dollar sizing. No fill or managed position resulted. This is an assessment, not an implemented fix or deployment approval.

## Independence limitation and build gate

Codex-2 read the entire supplied attachment before splitting its sections, and had already reproduced the stream-path Webull ask-zero defect during PMREST1. These answers therefore **cannot be described as blind**. They are independently replicated from fresh production reads and the deployed source. Acceptance of this disclosed limitation is pending. No runtime change has been made.

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

**Earlier occurrences:** retained-log census and per-event historical replay are in progress; do not yet call this the first occurrence.

### Q2 Why Webull received no order

**Cause:** the streamed cross builds a synthetic Quote with `ask_price=0`, so Webull's dollar-sizing helper refuses the draft even though a valid ask was supplied to the stream gate.

At 08:10:35.818 ET the log records `[V2-ENTRY-SIZE-REFUSED] symbol=SAIQ leg=webull price_basis=trigger_quote_ask price=0.0`. Code: `strategy_core/schwab_1m_v2.py:3240-3247` zeros bid/ask; `:6040-6057` passes that Quote to Webull; `:6214-6221` sizes from its ask and returns no draft. Schwab still emitted at 08:10:35.824. The fresh database pull found 1 SAIQ intent and 1 broker-order row, both on live:schwab_1m_v2; zero live:orb rows. The Schwab policy rejection happens later, so it did not cause the missing Webull draft.

**Newness:** the ask-dropping shape predates dollar sizing. A positive dollar amount makes the zero ask a sizing refusal; with amount zero the legacy fixed-share helper does not require that price. The current retained census contains MEDS, NXL and AMOD stream crosses before sizing deployment, and SAIQ afterward. Exact denominator and historical results will be recorded with the replay.

## Raw evidence and read safety

- Local fresh raw pull: `/tmp/pmprint1-own-read.json` (62 file paths: 31 OMS, 31 v2); original intent `/tmp/pmprint1-own-intent.json`.
- Remote sources: `/var/log/project-mai-tai/oms.log*`, `/var/log/project-mai-tai/schwab-1m-v2.log*`, `market_trade_ticks`, `market_quote_ticks`, `strategy_bar_history`, `trade_intents`, `broker_orders`, `fills`, `oms_managed_positions`.
- Original intent from `mai_tai:strategy-intents`, event bda3ad57-da92-4d69-b62c-2d5df0efc6ce; bounded XRANGE COUNT 25, found after three envelopes. No snapshot-batches read.
- Database queries were READ ONLY, statement timeout 8 s, per-symbol/time bounded. Tick `received_at` is database flush time, not callback time (`market_data/schwab_v2_tick_writer.py:99-137`); it cannot prove when a quote reached the bot. This distinction will be maintained in the replay.
- No broker write, trading restart, environment/flag change, Redis write, or production file edit.

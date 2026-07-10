# v2 exit-variant research — results & decisions (durable record)

Research-only (nothing live changed by this doc). The v2/CW exit variants, their measured results, the
broker-aware (latency-honest) verdict, the data sources, and the operator decisions. Script:
`/home/trader/wt-atr-ab/smart_exit_reentry.py` (VPS, off the live engine). See also
`docs/dual-broker-v2-design.md`, and the memory notes `project_mai_tai_smart_exit_research` /
`project_mai_tai_scanner_confirmed_capture`.

## Variant definitions
- **BASE** — today's LIVE confirmed-window (CW) exit: sell **100% at +2% target**, **−5% hard stop**,
  **bar-close ATR flip**. This is what the OMS runs today and what the dual-broker qty-1 harness tests on.
- **V1** — partial-scale: sell **50% at +2%**, **floor** the remaining 50% at +2%, **2% trailing** stop on the
  runner; −5% hard stop kept. (Operator-specified.)
- **V2** — V1 + **one MACD re-entry per flip** on a confirmed bullish MACD/signal cross (MACD line > 0).
- **V3** — V1 + **anticipatory** MACD re-entry: histogram shrinking toward the cross, within a threshold `T` of
  zero; `T` swept across 4 normalizations (abs / %-price / ATR / self-range). **ATR-normalized ranks best**
  (operator's "it should be a volatility-relative number" — confirmed).

## Sample
07-09 CONFIRMED set (from `scanner_confirmed_events`), 4 RTH-tradeable names (VRAX, JLHL, TDTH, RPGL —
**all Schwab-ineligible / Webull-routed**), ~10–16 trades. **n is tiny — directional, not proof.** The
accruing daily confirmed-set forward test is the adjudicator; 07-10 adds via the 17:00 ET cron.

## ⭐ INTRABAR vs BAR-CLOSE — the entry-timing gap (IMPORTANT)
This is central to reading every number below, and to the exit-path decision.
- **The backtest enters INTRABAR** — `wait3` enters at the **first tick (print)** that crosses the 3-bar-high
  trigger, mid-bar. That is the *earliest, best* price the breakout ever prints.
- **The LIVE CW bot enters at BAR-CLOSE + ~6s** — it only sees a bar's high crossed the level once the 1-min bar
  *completes*, then emits ~6s later (GMM 07-10: 08:19 bar closed 08:20:00 → `[V2-CW] ENTER` at 08:20:02). So the
  live entry lands **anywhere from a few seconds to ~60s+ later** than the intrabar cross, at a worse price.
- **Therefore the idealized (0-latency) table uses an intrabar entry the live bot NEVER achieves.** The honest
  tables charge the ~6.5s **system floor** on top of the intrabar-cross time — which is *between* intrabar and
  true bar-close, so **even the honest numbers are still slightly optimistic on entry** (a fully faithful model
  enters at **bar-close + 6.5s**, not intrabar-cross + 6.5s).
- **TODO (fidelity):** add a **bar-close-entry** mode to `smart_exit_reentry.py` (enter at the bar-close-then-+6s
  ask, matching the live path) and re-run — that is the true-to-live entry and will read lower than table ②.
  Intrabar stays as the "best-case entry" bound; bar-close is the "as-live" bound; the truth is bracketed.
- Exit legs already model latency (market exits fill at bid `delay` later; the +2% target is a resting limit).
  The **entry** is where the intrabar-vs-bar-close gap bites hardest.

## ① IDEALIZED — 0 latency (the fantasy; reference only)
| Variant | n | Win% | Median | Mean | Net$ |
|---|---|---|---|---|---|
| BASE | 10 | 80% | +2.00% | +1.01% | +$6.05 |
| V1 | 10 | 80% | +1.95% | +1.06% | +$6.17 |
| V2 | 16 | 81% | +1.89% | +1.02% | +$10.59 |
| **V3** (atr≤0.05) | 14 | **86%** | +2.00% | **+1.34%** | **+$10.96** |

## ② HONEST — mean% return; 6.5s MEASURED system floor on BOTH brokers + fill latency on top
Measured v2 latency (122 fills, DB): bar-close→emit ~6s + emit→submit ~0.56s = **~6.5s system floor** before
either broker acts. **Schwab fill latency was NEVER measured** (fills stamp `reference_price`, not a broker fill
time) — the old "Schwab ~0s" is an artifact. Columns = broker FILL latency ADDED to the 6.5s floor.
| Variant | +0s (floor only) | +0.5s | +1s | +2s | +3s | +14s |
|---|---|---|---|---|---|---|
| BASE | **−1.39%** | −0.63% | −0.96% | −0.96% | −0.65% | −1.21% |
| V1 | −1.49% | −0.72% | −1.06% | −1.13% | −0.80% | −1.40% |
| V2 | −1.03% | −0.46% | −0.66% | −0.78% | −0.69% | −0.98% |
| V3 | −0.90% | −0.35% | −0.61% | −0.64% | −0.51% | −0.82% |

## ③ HONEST — realistic broker-split (Webull-routed names at floor+3s; Schwab fill still unmeasured)
| Variant | Win% | Median | Mean | Net$ |
|---|---|---|---|---|
| BASE | 50% | +0.79% | **−0.65%** | −$2.57 |
| V1 | 50% | +0.22% | −0.80% | −$3.84 |
| V2 | 50% | +0.43% | −0.69% | −$5.41 |
| V3 | 57% | +0.93% | −0.51% | −$4.14 |

## ④ FORWARD RUN — 2026-07-09 + 2026-07-10 (real confirm→drop windows, 8 name-days with an ATR entry)
`smart_exit_universe_20260710T210001Z.log` (21:00 UTC cron). 9 (07-09) + 10 (07-10) confirmed names → **8
name-days produced ≥1 ATR entry**. First run with 07-10's real FADE drops. **Routing: 7/8 name-days
Schwab-INELIGIBLE → Webull** (GMM, HAO, JLHL, RPGL, TDTH, VRAX, YMAT; only SUNE → Schwab) — the confirmed
movers pay the most latency. V3 normalization here = `atr≤0.2` (the run's best sweep point).

**① Idealized (0-lat fantasy):**
| Variant | n | Win% | Median | Mean | Net$ |
|---|---|---|---|---|---|
| BASE | 16 | 75% | +2.00% | +0.50% | +$4.68 |
| V1 | 16 | 75% | +1.94% | +0.53% | +$4.79 |
| V2 | 26 | 73% | +1.90% | +0.30% | +$6.11 |
| **V3** (atr≤0.2) | 25 | **76%** | +1.98% | +0.50% | **+$10.19** |

**② Honest (6.5s floor to BOTH brokers + fill latency added):**
| Variant | +0s (floor only) | +0.5s | +1s | +2s | +3s | +14s |
|---|---|---|---|---|---|---|
| BASE | **−0.85%** | −0.36% | −0.54% | −0.49% | −0.25% | −0.01% |
| V1 | −0.96% | −0.51% | −0.63% | −0.75% | −0.44% | −0.26% |
| V2 | −1.24% | −0.83% | −0.83% | −0.77% | −0.58% | −0.74% |
| V3 | −0.41% | +0.15% | +0.10% | −0.19% | +0.01% | +0.26% |

**③ Realistic broker-split (Webull-routed at floor+3s; Schwab floor-only, still unmeasured → upper bound):**
| Variant | n | Win% | Median | Mean | Net$ |
|---|---|---|---|---|---|
| BASE | 17 | 59% | +2.00% | **−0.34%** | −$2.48 |
| V1 | 17 | 59% | +0.87% | −0.52% | −$4.49 |
| V2 | 28 | 57% | +1.06% | −0.64% | −$8.75 |
| V3 | 27 | 70% | +1.21% | −0.05% | +$0.06 |

**Read:** at the FLOOR ALONE every variant is mean-negative again (BASE −0.85%). V1/V2 are **out** — re-entry
adds latency-exposed losers (V2 −$8.75). V3 is the only not-clearly-negative one (≈breakeven, +$0.06) but
"least-bad on 8 name-days" = noise, not edge. **BASE is a fine incumbent — nothing beats it after latency.**
Consistent with run ①–③ (07-09 4-name): the sign holds on more data. Median +2.00% / mean ~0 → trust the mean.

## Verdict
Every variant is **positive at 0 latency (+$5–11) but negative once the ~6.5s the system already runs at is
charged** (BASE −1.39%/−0.85% at the floor alone across both runs; −0.65%/−0.34% realistic split). V3 is
consistently the *least* negative but not a real edge. **The median stays pinned near +2% while the mean craters**
— the tell that winners become losers (operator stopping-rule criterion 4: trust the MEAN; if mean/median diverge
in sign, trust the mean).
→ **The confirmed-window rule is characterized-but-unaffordable at today's latency.** You can't out-exit a bad
entry — on the raw gapper universe (209 trades, 5 days) every variant lost even harder. The weak link is the
ENTRY edge surviving latency, not the exit shape.

## DECISIONS (operator, 2026-07-10)
1. **Dual-broker build + qty-1 harness run on BASE** (the live CW exit) — this is plumbing/broker-evaluation, not
   an exit change. Flag-off. See `docs/dual-broker-v2-design.md`.
2. **Exit-rule choice (BASE vs V1 / V2 / V3) is DEFERRED to end of next week**, decided from the accruing
   confirmed-set forward data (real-latency), not from this tiny idealized sample.
3. **Enable of anything is gated on forward MEAN POSITIVE** over the stopping-rule window
   (`docs/atr-confirmed-window-forward-test.md`) — currently net-negative, so live enables wait.
4. Live canary at **qty 4** (2→4, 07-10) — accepted 2× loss exposure for a stronger $ signal.

## Data sources / how to reproduce
- Confirmed-set backtest: `python smart_exit_reentry.py confirmed-db 2026-07-09 2026-07-10` (VPS,
  `/home/trader/wt-atr-ab/`, niced, off-hours). Reads real [confirm→drop] windows from `scanner_confirmed_events`
  (multiple dates accumulate the forward series; run ④ logs to `smart_exit_universe_<stamp>.log`).
- Broker-aware latency: same script; `SYSTEM_FLOOR_S=6.5`, `WEBULL_LAT_S=3.0`, `SCHWAB_INELIGIBLE` set from the
  live `broker_orders` reject history. Nightly universe/confirmed run: cron 21:00 UTC (17:00 ET) → ntfy.
- Latency measurement: `broker_order_events`/`fills`/`trade_intents` timestamp spans across the 122 v2 fills.

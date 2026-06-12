# Design — schwab_1m_v2 replay study (expectancy / MFE / MAE / slippage haircut)

**Status:** design for review. No code yet. Read-only analysis — like the overnight audit, it
produces scripts + a report via PR; it changes no strategy/OMS behavior.

## Purpose & what it is NOT

Quantify whether v2's fired signals are *worth trading* — expectancy and its drivers (MFE/MAE,
win/loss, profit factor) under explicit, honest exit and slippage assumptions. It is **not** a live
track record: today's signals were OMS-rejected (no real fills), sim fills are idealized, and the
entry reference is the signal bar close (optimistic). The study models **opportunity with stated
haircuts**, and is explicit about every idealization.

## Foundation it builds on (already verified)

- **Bars** — Phase A proved v2's stored 1m bars are byte-exact vs Schwab vendor data (24/24).
- **Signals** — Phase B reproduced all fired signals (35/35, 0 false positives, 0 unexplained
  misses) from the bot's own inputs. Entry price = signal bar close (== the new `reference_price`),
  so replay entries and any future sim fills agree **by construction**.
- So the study can start **now** on bars+signals; it does not wait on fills or tick capture.

## The core difficulty (and why it's phased)

A 1-minute candle only gives O/H/L/C. For a trade entered at the close, the **forward** candles'
highs/lows bound MFE/MAE — but when a single forward candle's range spans **both** the target and
the stop, the bar cannot say which hit first. That ambiguity is exactly what tick capture (PR #282)
resolves. So:

- **Phase 1 — bar-only (available now).** Computes MFE/MAE and expectancy under exit policies that
  don't depend on intra-candle ordering, and **inventories the ambiguous candles** (both-hit) rather
  than guessing them.
- **Phase 2 — tick-resolved (after #282 deploys + captures a day).** Re-resolves the ambiguous
  candles via `replay_exit_from_ticks.py` (first-tick walk; `UNRESOLVED_NO_TICKS` when a window has
  no ticks) and adds realistic-fill haircuts from quote ticks.

## Inputs

- `trade_intents` for `schwab_1m_v2` over a date range (entry ts, entry price = `reference_price`/
  signal close, path, side=buy).
- `strategy_bar_history` (v2) + Schwab `pricehistory` (the Phase-A-validated vendor source) for the
  forward window — reuse the audit's fetch so the study inherits the parity guarantee.
- (Phase 2) `market_trade_ticks` / `market_quote_ticks`.
- Horizons: 5 / 15 / 30 / 60 min (configurable).

## Metrics (per signal, then aggregated)

Per signal, from forward bars over each horizon H:
- **MFE** = max(high) − entry over [t, t+H]; **MAE** = entry − min(low). Report in $ and %.
- **time-to-MFE / time-to-MAE** (minutes) — separates "pops then fades" from "grinds up".
- **Ambiguity flag** — for a given (target%, stop%) pair, the first forward candle whose range
  covers BOTH levels (bar-unresolvable → Phase 2).

Expectancy under **exit policies** (each stated, none claimed realistic):
1. **Fixed target/stop** (e.g. +2% / −1.5%): bar-only resolves all non-ambiguous candles; ambiguous
   ones are reported separately as a **bounded range** (best case = target-first, worst = stop-first)
   — never a point estimate that hides the ambiguity.
2. **Time-stop** (exit at close of t+H): unambiguous from bars; a clean lower-bar on "do signals
   carry".
3. **MFE-capture fractions** (e.g. could you have captured 50% of MFE before MAE): characterizes the
   signal independent of a specific rule.
4. *(Documented as out-of-scope for v1)* the strategy's **actual** OMS exits (MACD-cross-down /
   stochastic / quick-stop / scaled / hard-stop) — modelling those faithfully is a separate, larger
   piece; v1 uses the transparent policies above and says so.

Aggregates: win rate, avg win / avg loss, profit factor, expectancy per trade ($ and %), per path
(MACD Cross vs VWAP Breakout), and by session bucket (premarket vs RTH).

## Slippage haircut (the honesty layer)

The bar-close entry is the **optimistic upper bound** (recorded sim-fill scope). The study reports
expectancy at three explicit fill assumptions so the operator sees the gradient, not one number:
- **Idealized** — fill at signal close (what sim does today).
- **Spread haircut** — entry at the ask, exit at the bid, from quote ticks (Phase 2) or a fixed
  assumed spread (Phase 1 placeholder, flagged as assumed).
- **Slippage + partials** — a parameterized haircut (bps + fill-probability) for v2's illiquid
  pennies, where real slippage is severe. Phase 2 can ground the spread from captured quotes; full
  realism still needs the future tick-based slippage sink or real fills.

The report leads with: *these are opportunity estimates under stated assumptions; illiquid-penny
slippage means realized results will be materially worse than idealized.*

## Deliverables

- `analysis/replay_study.py` — re-runnable on any date range; emits per-signal rows + aggregates
  (JSON) and a markdown report. Reuses the audit's vendor-fetch + the Phase-A parity gate (quarantine
  any non-assembly-exact symbol's signals, as the audit does).
- `analysis/reports/replay-study-<date>.md` — expectancy tables (by policy / path / session / fill
  assumption), the MFE/MAE distributions, the **ambiguous-candle inventory**, and an explicit
  assumptions/limitations section.
- Phase 2 add-on: tick resolution of the ambiguous inventory + quote-grounded spread haircut.

## Sequencing

- **Phase 1 can be implemented now** (on the verified bars+signals) once this design is approved —
  in parallel with / after the #284 review. It does not block on the after-close deploy.
- **Phase 2** follows once #282 (tick capture) is deployed and has captured ≥1 RTH day.
- Same gates as the audit: read-only, findings not trades, scripts + report via PR.

## Open questions for review

1. Default target%/stop% for the fixed policy (the requirement cited +2% / −1.5% — use those?).
2. Horizon set (5/15/30/60 min ok, or include EOD)?
3. Is the strategy's actual OMS exit-rule modelling wanted in v1, or correctly deferred to v2 of the
   study (my rec: defer — it's a large faithful-simulation effort; the transparent policies answer
   "do the signals carry?" first)?

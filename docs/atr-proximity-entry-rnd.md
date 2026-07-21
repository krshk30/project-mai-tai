# ATR-Proximity Anticipatory Entry — R&D report (2026-07-21)

> **Status: PROMISING, NOT PROVEN. Nothing deployed, nothing routed. No live path imports any of
> this code.** One configuration produced a positive result with a confidence interval excluding
> zero, but ~174 cells were searched against a single 9-day dataset — the exact setup that
> produced the ORB "+11.2" that later died out-of-sample. **The required next step is an
> out-of-sample test, not more tuning.**

- **Code:** PR #500 (`claude/atr-proximity-rnd`) — `src/project_mai_tai/backtest/{dot_entry,proximity_sweep}.py`,
  `scripts/run_proximity_sweep.py`, `scripts/run_stop_floor_grid.py`, 24 unit tests.
- **Operator:** requested + directed every variation here.
- **Related:** [`project_mai_tai_v2_stop_slippage_rootcause`] (exit geometry closed 07-17),
  [`project_mai_tai_orb`] (the OOS-kill precedent), [`project_mai_tai_backtest_engine`].

---

## 1. The question

Today's live v2 "CW" entry buys **confirmation**:

> ATR trail flips long → wait 3 bars → track the 3-bar high → enter on the first bar whose HIGH
> breaks it. (`schwab_1m_v2.py::_cw_entry`)

The operator proposed buying **anticipation** instead:

> While the ATR trail is still **short** (the purple dots sit ABOVE price), enter when a bar
> **closes within X% below the trail** — before the cross happens at all.

Rationale: a cheaper entry when the cross does come. Cost: you pay for the ones that never cross.

Everything else was to be held constant — this was explicitly an **entry-only** change. The exit
geometry became part of the study later, at the operator's direction (§5).

---

## 2. The rule as implemented

```
proximity = (trail - close) / close * 100        # close BELOW trail, pre-cross
signal    = state == "short" and 0 <= proximity <= X
```

- **One entry per short-segment.** A segment is a contiguous run of `state == "short"`; the first
  qualifying bar consumes it; a new segment re-arms at the next SELL flip.
- **The ATR trail is not re-derived.** `backtest/atr_oracle.py` is the same parity-tested oracle the
  live v2 uses (ATRPeriod=5, ATRFactor=3.5, Wilders, seed=sma5) — so the purple line in the study is
  the purple line on the operator's chart.
- **Fill variants:**
  - `same_bar` — fill at the signal bar's close. **Optimistic**: the condition is only *known* once
    that bar closes. Upper bound, never the headline.
  - `next_open` — fill at the next bar's open. **The honest one. All headline numbers use this.**
- **Exit walk** is bar-level and deliberately pessimistic: a bar spanning both stop and target books
  the **STOP**. Assuming the good fill is the easiest way to manufacture a fake edge here.

### Exit geometries tested

| Mode | Behaviour |
|---|---|
| `target` | Hard take-profit at +2% (the incumbent CW geometry) |
| `floor_ladder` | **No hard target.** Once the high reaches +`floor_start`%, a floor is set at the whole percent reached and ratchets up 1% at a time. Exit when a bar's LOW falls back to the floor. |
| `trail2` | **No hard target.** Trail N% below the high-water mark once in profit. |

In all modes the initial stop stays live until a floor/trail takes over.

---

## 3. Data and method

| | |
|---|---|
| Window | 9 days, 2026-07-09 → 2026-07-21 |
| Universe gating | `scanner_confirmed_events` CONFIRM → (FADE \| RETENTION_DROP) — entries only while the scanner actually had the name confirmed. Same gating as the 07-17 studies, which is what makes these comparable. |
| Confirmed windows found | 239 |
| **Windows actually usable** | **97** (20 had no captured tape; 122 had <20 bars) |
| **Effective universe** | **24–30 names** depending on threshold |
| Bar source | `market_capture_trades` → `OrbTickAggregator` (the live bot's own aggregator) |
| Warmup | 40 min pre-window, marked `warmup` so no entry is taken before the CONFIRM |

**Reporting discipline applied throughout:** per-trade **%** (never dollars), **drop-one by NAME**,
95% CI on the mean, cells-searched declared, pre-stated expectation recorded before each run.

### ⚠️ Why the MEDIAN is unusable in most cells

Every `target` and `floor_ladder` cell reports a median of exactly **+2.000%** (or +3.000% at floor
3). That is not an edge — it is the target/floor acting as a hard ceiling while >50% of trades reach
it. The distribution is **bimodal** (a pile at the target, a pile at −5%, a hole between), so the
median just reports which mode holds the majority.

This is the documented exception in the percentages-not-dollars rule: *outliers ⇒ trust the median;
a hole in the middle ⇒ trust the mean.* **All conclusions below are read off the MEAN.**
(`trail2` is the exception — its distribution is not target-pinned, and its median is meaningful.)

---

## 4. Run 1 — entry only, incumbent +2% target (6 cells)

Pre-stated expectation: **null.**

| Proximity | Fill | n | Mean | Win % | CI | CI excl 0 |
|---|---|---|---|---|---|---|
| 0.5% | next_open | 66 | −0.124 | 66.7 | [−0.851, +0.604] | No |
| 0.5% | same_bar | 66 | −0.073 | 66.7 | [−0.793, +0.648] | No |
| 1.0% | next_open | 95 | −0.344 | 63.2 | [−0.974, +0.287] | No |
| 1.0% | same_bar | 95 | −0.270 | 64.2 | [−0.891, +0.351] | No |
| 1.5% | next_open | 111 | +0.061 | 70.3 | [−0.503, +0.624] | No |
| 1.5% | same_bar | 111 | −0.026 | 68.5 | [−0.591, +0.540] | No |

**Verdict: null, as pre-stated.** Not one CI excluded zero; 4/6 flipped sign under drop-one; means
were not monotone in the threshold.

### The structural reason it lost

With **+2% target / −5% stop**, breakeven win rate is `5/(5+2) = 71.4%`.

| Proximity | Win % | vs breakeven |
|---|---|---|
| 0.5% | 66.7 | −4.7 pts |
| 1.0% | 63.2 | −8.2 pts |
| 1.5% | 70.3 | −1.1 pts |

Every cell sat below the line. **The win rate looked excellent and still lost, because the payoff
geometry demanded 71.4%.** This is the same wall the 07-17 exit-geometry study hit.

### Fill-variant answer (operator's "does it go below if it doesn't break?")

The two fill modes differ by only **0.05–0.09pp**, and the sign of the difference is **not
consistent** (at 1.5% `next_open` is better; at 0.5%/1.0% slightly worse). **There is no systematic
pullback after the signal bar** — waiting a bar is neither a discount nor a tax. Use `next_open`; it
is the honest one and costs nothing.

---

## 5. Run 2 — exit geometry × confirmation filter (36 cells)

Triggered by the operator's question: *"are we taking only 2% while the stock goes higher?"*

### Answer: yes, and it was quantified

Same 111 trades, same 77 winners, only the exit differs:

| Exit | Winners | Winner median | **Winner mean** |
|---|---|---|---|
| Hard +2% target | 77 | +2.000% | **+2.000%** ← capped by construction |
| Floor ladder | 77 | +2.000% | **+2.805%** |

**The +2% cap was discarding ~0.81pp per winner.**

### Exit ordering — consistent in all 12 pairings

| Proximity | Filter | target | **floor_ladder** | trail2 |
|---|---|---|---|---|
| 0.5% | none | −0.124 | **+0.331** | −0.300 |
| 0.5% | volume_strict | −0.024 | **+0.345** | −0.600 |
| 1.0% | none | −0.344 | **+0.225** | −0.276 |
| 1.0% | volume_strict | +0.152 | **+0.545** | −0.461 |
| 1.5% | none | +0.061 | **+0.619** | −0.025 |
| 1.5% | volume_strict | +0.247 | **+0.786** | −0.120 |

- **`floor_ladder` > `target` in 12/12.**
- **`trail2` (2% trailing) lost in 12/12 — it is dead.** It exits 92 of 111 trades on the trail at a
  **median of −0.04%**, collapsing win rate from ~70% to ~42%. Too tight for these names: it is shaken
  out before the move develops.

### Filter contribution (1.5% / floor_ladder)

| Filter | n | % removed | Mean | Win % |
|---|---|---|---|---|
| none | 111 | — | +0.619 | 70.3 |
| script (MACD+Stoch+RSI) | 101 | 9% | +0.641 | 72.3 |
| volume_loose | 90 | 19% | +0.708 | 73.3 |
| volume_strict | 39 | 65% | +0.786 | 74.4 |

**The operator's thinkScript is near-inert as a filter** (9% removed; at 0.5% it removed *zero*).
Sensible in hindsight: "MACD/RSI/Stoch turned up off a recent low" is almost always true when price
is rising toward the trail from below — it measures what the proximity trigger already selected for.
**The volume requirement does the actual filtering.**

---

## 6. Run 3 — stop × floor-start (72 cells)

Operator hypothesis: *entering earlier should allow a tighter stop.*

### That hypothesis was WRONG — the data rejected it

1.5% proximity, floor 2%, unfiltered:

| Stop | Mean | Win % |
|---|---|---|
| **−5%** | **+0.619** | **70.3** |
| −4% | +0.489 | 64.9 |
| −3% | +0.459 | 58.6 |

Same direction at 0.5% (+0.331 → +0.257 → +0.033); roughly flat at 1.0%. **Tightening the stop costs
win rate quickly and does not buy back enough. The earlier entry did NOT shrink adverse excursion —
these names breathe more than 3% against you and still work out. Keep −5%.**

(One exception, `1.5/−3/floor2/volume_strict` = +0.795, the grid's best cell. With 72 cells searched
that is read as noise, not a reason to tighten.)

### Floor start

1.5% proximity, −5% stop, unfiltered:

| Floor start | Mean | Win % | Median |
|---|---|---|---|
| 2% | +0.619 | 70.3 | +2.000 |
| **3%** | **+0.711** | 59.5 | +3.000 |
| 4% | +0.592 | 51.4 | +0.375 |
| 5% | +0.546 | 46.8 | −0.381 |

Peak at 3%, decaying after — waiting for 4–5% before locking anything gives too much back.

**Proximity was monotone (1.5 > 1.0 > 0.5) to the edge of the swept range**, which motivated Run 4.

---

## 7. Run 4 — volume persistence + extended proximity (60 cells)

Operator's refinement: *volume should be checked over 2–3 bars, "holding, not declining" — not a
single-bar spike.* `volume_strict` was indeed single-bar. Two persistence filters were added:

| Filter | Definition |
|---|---|
| `volume_hold` | Non-declining staircase: `v[i] >= v[i-1] >= v[i-2] >= v[i-3]` |
| `volume_sustained` | Every one of the last 3 bars above the median of the 10 before them (tolerates a dip) |
| `volume_hold_macd` | `volume_hold` + MACD row + Stoch row |

Proximity extended to 2.0 / 2.5 / 3.0%. Stop fixed −5% (Run 3), exit fixed `floor_ladder` (Run 2).

### ★ The recommended configuration

> **proximity 2.0% · stop −5% · floor ladder starting 3% · NO filter**
> mean **+0.935%/trade** · median +3.000% · win **60.9%** · **n = 128 / 27 names**
> **95% CI [+0.16, +1.71] — EXCLUDES ZERO** · drop-one mean [+0.76, +1.09] · **no sign flip**

The only cell in ~174 searched whose CI excludes zero — and it is also the **largest-sample** cell
and required **no filter selection**, so it carries the least overfitting risk of anything tested.

### Structure 1 — proximity has an interior optimum at 2.0%

Unfiltered, floor 3%:

| Proximity | 0.5 | 1.0 | 1.5 | **2.0** | 2.5 | 3.0 |
|---|---|---|---|---|---|---|
| Mean | +0.555 | +0.390 | +0.711 | **+0.935** | +0.553 | +0.229 |

It does **not** keep rising. An interior optimum is much stronger evidence than
monotone-to-the-boundary: the real parameter is bracketed.

### Structure 2 — floor 3% beats floor 2% at EVERY proximity (6/6)

| Proximity | 0.5 | 1.0 | 1.5 | 2.0 | 2.5 | 3.0 |
|---|---|---|---|---|---|---|
| floor 2% | +0.331 | +0.225 | +0.619 | +0.582 | +0.217 | +0.063 |
| **floor 3%** | **+0.555** | **+0.390** | **+0.711** | **+0.935** | **+0.553** | **+0.229** |

Floor 3% trades win rate (69.5% → 60.9% at 2.0) for trade size, and size wins.

### Structure 3 — the oscillator rows contribute ~nothing

`volume_hold` and `volume_hold_macd` returned **identical** results at 0.5 / 1.0 / 1.5 (n = 9, 10,
13) — adding MACD + Stoch on top of volume persistence changed **not one trade**. With the
thinkScript filter removing only ~9% earlier, the conclusion is consistent: **volume is the entire
filter; the oscillators are decorative at these moments.**

### Volume persistence: best means, unusable sample

| Cell | Mean | n | names |
|---|---|---|---|
| 2.0 / floor3 / volume_hold_macd | +2.534 | 12 | 8 |
| 2.0 / floor3 / volume_hold | +2.124 | 13 | 8 |
| 1.5 / floor3 / volume_hold | +1.954 | 13 | 9 |

**Not trusted.** The strict staircase cuts 128 → 13 trades (90%), every CI spans zero enormously, and
at 3.0% proximity both go **negative** (−0.60, −1.13). Twelve trades over eight names cannot separate
a filter from luck.

Middle ground worth one more look: **`2.5 / floor3 / volume_strict`** = +1.038%, n=56, 22 names,
drop-one [+0.88, +1.20], no flip, CI [−0.10, +2.18] — just misses.

---

## 8. VERDICT

**The anticipatory entry is the first configuration this month that is not dead.** That is a real
change from the standing position ("the entry family has no edge in any form"), and it came from
changing the **exit geometry**, not just the trigger.

What is genuinely supported:

1. The hard +2% target was **costing ~0.81pp per winner**; the floor ladder recovers it. (12/12)
2. A **2% trailing stop is dead.** (12/12 negative)
3. **Tighter stops do not help** — −5% ≥ −4% ≥ −3%. The operator's hypothesis was tested and rejected.
4. **Proximity optimum ≈ 2.0%**, an interior peak.
5. **Floor start 3% > 2%** at every proximity. (6/6)
6. **Volume is the whole filter**; MACD/Stoch/RSI add ~nothing.

What is **NOT** supported:

- **Any claim of a proven edge.** ~174 cells were searched against ONE 9-day dataset of 24–30 names.
  In Run 4 alone, 60 cells produced exactly **1** CI excluding zero — **pure chance would produce
  ~3**. The cells are not independent, so that arithmetic is not exact, but it decisively rules out
  reading "CI excludes zero" as proof.
- **Any live routing decision.** The sample is far too thin, and the coverage gap is material: only
  97 of 239 confirmed windows had usable tape.

**Honest summary: a coherent, internally consistent positive structure on a small in-sample dataset.
The structure is more believable than the level. +0.935%/trade is the number to try to KILL next,
not to trade.**

---

## 9. Proposed follow-up testing (in priority order)

### P1 — Out-of-sample split ← DO THIS FIRST, BEFORE ANY MORE TUNING
Lock the recommended config (**2.0% / −5% / floor 3% / no filter**) and test it on days it has never
seen. Two forms:
- **Immediate:** first-5-days vs last-4-days split of the current window. Cheap; either survives or not.
- **Better:** forward-test as `market_capture_*` accumulates (note the **14-day prune** — data ages out,
  so this must be captured deliberately, not assumed).

**Rationale: this is exactly what killed the ORB "+11.2"** — strong in-sample, dead out-of-sample.
Until this runs, every further sweep just fits the same 97 windows harder. **No parameter tuning
until P1 completes.**

### P2 — Fix the coverage gap
122 of 239 windows were dropped for <20 bars of tape. Determine whether that is genuine illiquidity
or a capture gap. If capture, the effective sample could roughly double, which matters more than any
parameter.

### P3 — Sample-size honest re-test of volume persistence
`volume_hold` shows the best means on n≈13. Re-run once P2 widens the sample; if it holds at n>50 it
is real, otherwise it was noise.

### P4 — Only if P1 survives: the live-fidelity questions
- Replace the bar-level exit walk with the tape-level `v2_sim::_run_exit` (observed-bid fills) — the
  bar-level walk is idealized and will overstate.
- Model the actual entry mechanics (resting order vs marketable), spread, and the measured latency band.
- Re-examine whether one-entry-per-segment is right, or whether re-arming helps.

### P5 — Deferred / not recommended yet
Floor 2 vs 3 as a preference call (win-rate vs size); proximity fine-tuning between 1.5 and 2.5.
Both are noise-level distinctions on the current sample.

---

## 10. Reproduction

```bash
# On the VPS, NICED (heavy R&D contends with the OMS loop -- the 07-08 stalls)
sudo systemd-run --uid=trader --quiet --wait --pipe --nice=19 \
  -p EnvironmentFile=/etc/project-mai-tai/project-mai-tai.env \
  --working-directory=/home/trader/project-mai-tai \
  /home/trader/project-mai-tai/.venv/bin/python scripts/run_stop_floor_grid.py --days 9
```

Grid constants live at the top of each runner (`STOPS`, `FLOOR_STARTS`, `FILTERS`,
`PROXIMITY_PCTS` in `proximity_sweep.py`). Detail JSON lands in `/tmp/`.

**Date-bounds to pin on every citation of these numbers:** 9 days ending **2026-07-21**, 97 usable
windows, 24–30 names, `market_capture_trades` as the decision source.

---

## Appendix A — the operator's thinkScript, decoded

```
MACD  : macd(6,13) > lowest(macd(6,13),3)[1]
STOCH : (stochasticfast() > lowest(stochasticfast(),5)[1] and stochasticfast() > 30)
        or stochasticfast() > 70
RSI   : (rsi() > lowest(rsi(),5)[1] and rsi() > 30) or rsi() > 70
allGreen = consensus >= 3        # ALL three
```

All three rows are **"turning up off a recent low and out of the basement"** — an **inflection**
detector, not a trend or level detector. This matters: guessing "MACD > signal" would have tested a
different rule entirely and the difference would have been invisible in the output.

### ⚠️ The off-by-one that fails silently

thinkScript `lowest(x,n)[1]` = min over the n bars **ending at the prior bar** — the current bar is
**excluded**. Including it turns *"MACD turned up off its 3-bar low"* into *"MACD **is** its own
3-bar low"*, which is essentially never true → **zero entries → the rule looks dead when it was never
tested.** Pinned in `test_dot_entry_rows.py` and mutation-checked.

### TOS defaults assumed (they move the numbers)

| Call | Assumed | Risk if wrong |
|---|---|---|
| `macd(6,13)` | Value = EMA6 − EMA13 (first plot) | if the histogram/Diff was meant, results shift |
| `stochasticfast()` | FastK, KPeriod 10 (TOS default, first plot) | a chart override changes the row |
| `rsi()` | length 14, Wilders | standard |

### Script vs verbal description — an unresolved discrepancy

The pasted thinkScript uses **MACD + StochasticFast + RSI**. The operator verbally described **MACD +
StochK + VOLUME**. Volume appears nowhere in the script. **Both were built and measured** rather than
one being guessed — and the volume version proved to be the one that matters.

---

## Appendix B — cells searched (multiple-comparison ledger)

| Run | Grid | Cells |
|---|---|---|
| 1 | 3 proximity × 2 fill | 6 |
| 2 | 3 proximity × 3 exit × 4 filter | 36 |
| 3 | 3 proximity × 3 stop × 4 floor × 2 filter | 72 |
| 4 | 6 proximity × 1 stop × 2 floor × 5 filter | 60 |
| | **TOTAL** | **174** |

**Cells with a 95% CI excluding zero: 1.** Chance alone at α=0.05 across 60 independent cells would
produce ~3. This ledger exists so the single significant cell is never quoted without it.

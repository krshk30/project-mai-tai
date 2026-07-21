# ATR-Proximity Anticipatory Entry — R&D report (2026-07-21)

> **⛔ STATUS: FAILED OUT-OF-SAMPLE. Nothing deployed, nothing routed, nothing deployable.**
> The in-sample search produced a headline of +0.935%/trade with a CI excluding zero. An honest
> walk-forward (select on the first 5 days, evaluate once on the last 4) took the selected cell from
> **+1.399% in-sample to −0.500% out-of-sample**. That is the ORB "+11.2" pattern reproducing exactly.
>
> **The in-sample sections (§4–§7) are retained deliberately** — they record what the search found and
> what it looked like *before* validation, which is the whole lesson. **Read §8 before quoting any
> number from them.**

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

## 8. Run 5 — OUT-OF-SAMPLE VALIDATION ★ THE DECIDING TEST

`scripts/run_oos_split.py`. Split: **in-sample = 07-09, 07-10, 07-13, 07-14, 07-15**;
**out-of-sample = 07-16, 07-17, 07-20, 07-21**.

Two tests were run, and only the second is real.

### TEST A — stability of the recommended config (WEAK)

Not out-of-sample: that config was chosen while looking at all 9 days, so both halves informed the
choice. It can detect instability; it can never confirm an edge.

| Half | n | Mean | Win % | CI |
|---|---|---|---|---|
| First 5 days | 109 | +0.922% | 61.5 | [+0.11, +1.74] |
| **Last 4 days** | **22** | **+0.190%** | 50.0 | [−1.97, +2.35] |

Positive in both halves but **decayed ~80%**, on only 22 OOS trades. Not falsified, not confirmed.

### TEST B — honest walk-forward (THE REAL TEST)

The whole grid was re-run on the **first half only**; the winner there was selected using nothing
from the second half, then evaluated on the second half **exactly once**. Minimum n=30 to be
selectable, so a 12-trade cell could not win.

| | Config | n | Mean | Win % | CI |
|---|---|---|---|---|---|
| In-sample | 2.5% / floor 3% / volume_strict | 43 | **+1.399%** | 69.8 | [+0.13, +2.67] **excl 0** |
| **Out-of-sample** | *same* | 14 | **−0.500%** | 50.0 | [−2.87, +1.87] |

**Decay −1.90pp. VERDICT: FAILED.** The selected cell had a CI excluding zero in-sample and still
went negative out-of-sample.

### ★ Which parameters overfit — the most useful output of the whole study

Sign flips (in-sample → OOS) across all 48 cells:

| Family | Sign flips (of cells with OOS data) |
|---|---|
| `volume_strict` | **9 of 12** |
| `volume_sustained` | **8 of 9** |
| `none`, floor 3% | 3 of 6 |
| **`none`, floor 2%** | **1 of 6** |

**The volume filters are the overfit** — they flip sign almost universally, including `volume_strict`
at n=40–47 in-sample, which was not obviously thin. The problem was not sample size; the filter was
fitting noise.

**A direct reversal of an in-sample "structural" finding — the head-to-head, unfiltered:**

| Proximity | in f2 | in f3 | IS winner | OOS n | OOS f2 | OOS f3 | **OOS winner** |
|---|---|---|---|---|---|---|---|
| 0.5% | +0.264 | +0.707 | floor3 | 10 | **+0.177** | -0.869 | **floor2** |
| 1.0% | +0.106 | +0.407 | floor3 | 12 | **+0.624** | -0.175 | **floor2** |
| 1.5% | +0.482 | +0.750 | floor3 | 17 | **+0.735** | -0.182 | **floor2** |
| 2.0% | +0.475 | +0.922 | floor3 | 22 | **+0.353** | +0.190 | **floor2** |
| 2.5% | +0.087 | +0.583 | floor3 | 26 | **+0.842** | +0.166 | **floor2** |
| 3.0% | -0.035 | +0.231 | floor3 | 29 | **+0.536** | +0.019 | **floor2** |

**In-sample floor3 wins 6/6. Out-of-sample floor2 wins 6/6.** A total reversal.
**OOS average: floor2 +0.545% vs floor3 -0.142%** — floor 3 is NEGATIVE out-of-sample.
At the locked proximity 2.0%, floor 3 looked twice as good in-sample (+0.922 vs +0.475) and gave back
**6x more** (-0.73pp vs -0.12pp decay). That is the overfitting signature in a single row.

§7 explicitly argued that this kind of monotone structure was more believable than any single level.
**That reasoning was wrong here** — floor-start WAS structure, 6/6, and it still did not generalise.
Recorded because it is the most transferable lesson in this document. **Any lock uses floor 2%;
§7's floor-3 preference is an in-sample statement that this section supersedes.**

The classic overfitting signature is visible in the ordering: **the more selection choices a cell
used, the worse it decayed.** Unfiltered cells made the fewest choices and held up best.

### The one surviving thread

Unfiltered, floor 2%, positive in BOTH halves, no sign flip:

| Proximity | In-sample | OOS |
|---|---|---|
| 1.0% | +0.106 | +0.624 |
| 1.5% | +0.482 | +0.735 |
| 2.0% | +0.475 | +0.353 |
| 2.5% | +0.087 | +0.842 |

Small, consistent, and **the simplest configuration in the entire study** — the opposite of what the
in-sample search recommended.

### Two caveats against over-reading the OOS result itself

1. **The OOS sample is thin** — 10–29 trades per cell, 22 for the locked config. It can reject a
   strong claim (and did) but cannot confirm a weak one.
2. **The OOS period is not just "later," it is quieter** — 109 in-sample trades vs 22 OOS across a
   5:4 day split. The operator independently observed a slow market with no fleet activity on 07-21.
   So the OOS window may be a different *regime*, not merely a different sample. This weakens the
   failure verdict slightly — but the burden of proof is on the edge, not on the test.

---

## 9. VERDICT

**Nothing here is deployable.** The in-sample headline (+0.935%/trade, CI excluding zero) did **not**
survive contact with unseen data, and the filter work — the part that looked most promising — is
exactly where the overfitting concentrated.

The standing position from earlier this month is **unchanged**: no entry variant has demonstrated an
edge that survives validation. This study did not overturn it.

What survived out-of-sample (weak, and the only thread left):

- **Unfiltered, floor 2%, proximity 1.0–2.5%** — small positive means in both halves (+0.09→+0.84%),
  no sign flips. The simplest configuration tested, and the one the in-sample search ranked *lowest*.

What did NOT survive:

- The recommended config's magnitude (+0.935% → +0.190%, an ~80% decay).
- Every volume filter (9/12 and 8/9 sign flips).
- The floor-3% > floor-2% "structure" (6/6 in-sample, reversed out-of-sample).

What remains true as *mechanics* (measured directly, not selected for, so not at risk of the same
overfitting — but only ever demonstrated on this dataset):

1. The hard +2% target **caps winners**: same 77 winners averaged +2.805% under a ladder vs +2.000%
   capped — a mechanical fact about the geometry, not a fitted parameter.
2. A **2% trailing stop is dead** (12/12 negative; it exits 92/111 trades at a median of −0.04%).
3. **Tighter stops did not help** (−5% ≥ −4% ≥ −3%); the earlier entry did not shrink adverse excursion.
4. **The oscillator rows contribute ~nothing** — `volume_hold` and `volume_hold_macd` returned
   identical trades at three thresholds; the thinkScript filter removed only ~9% of signals.

### The methodological lesson (the most valuable output)

In §7 the report argued that *structure* (monotone/interior-optimum patterns across many cells) was
more believable than any single cell's *level*. **The walk-forward falsified that for floor-start:**
a 6/6 monotone result reversed out-of-sample. Consistency across cells of a heavily-searched grid is
**not** independent evidence — the cells share the same data and the same noise. Only unseen data
distinguishes structure from a shared artefact.

Superseded — what the earlier draft of this document claimed:

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

**Honest summary: the search found a coherent positive structure in-sample; validation destroyed the
level and reversed part of the structure. +0.935%/trade was the number to KILL, and it was killed.**

---

## 10. Follow-up testing

### ✅ P1 — Out-of-sample split — **DONE 2026-07-21. FAILED.** See §8.
Walk-forward took the selected cell from +1.399% to −0.500%. **Stop searching this dataset.** Every
further sweep over the same 97 windows fits noise harder; the grid has already been searched ~222
cells deep against 9 days of 24–30 names.

### P2 — Fix the coverage gap ← NOW THE HIGHEST-VALUE WORK
122 of 239 windows were dropped for <20 bars of tape. Determine whether that is genuine illiquidity
or a capture gap. If capture, the effective sample could roughly double, which matters more than any
parameter.

### P3 — Forward test, not another split
Lock **proximity 1.5–2.0% / stop −5% / floor 2% / NO filter** (the only OOS survivor), record it
now, and evaluate on days that do not exist yet. ⚠ `market_capture_*` has a **14-day prune** —
forward data must be captured deliberately or it ages out before there is enough of it.
**No filter, no floor-3: both failed validation. Adding them back requires new evidence, not a re-run.**

### P4 — Only if P3 shows something: the live-fidelity questions
- Replace the bar-level exit walk with the tape-level `v2_sim::_run_exit` (observed-bid fills) — the
  bar-level walk is idealized and will overstate.
- Model the actual entry mechanics (resting order vs marketable), spread, and the measured latency band.
- Re-examine whether one-entry-per-segment is right, or whether re-arming helps.

### P5 — Do NOT do these
- **Re-tuning filters on this dataset.** They are where the overfitting lives (9/12 and 8/9 sign flips).
- **Floor 2 vs 3 fine-tuning.** The in-sample 6/6 result reversed out-of-sample; it is noise.
- **Proximity fine-tuning between 1.5 and 2.5.** Noise-level distinctions on this sample.
- **Another split of the same 9 days.** The data is exhausted; only new data can inform this now.

---

## 11. Reproduction

```bash
# On the VPS, NICED (heavy R&D contends with the OMS loop -- the 07-08 stalls)
sudo systemd-run --uid=trader --quiet --wait --pipe --nice=19 \
  -p EnvironmentFile=/etc/project-mai-tai/project-mai-tai.env \
  --working-directory=/home/trader/project-mai-tai \
  /home/trader/project-mai-tai/.venv/bin/python scripts/run_stop_floor_grid.py --days 9
```

```bash
# The out-of-sample validation (section 8) -- the test that matters
... scripts/run_oos_split.py --days 9 --split 5
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
| 5 (OOS) | 6 proximity × 2 floor × 4 filter | 48 |
| | **TOTAL** | **222** |

**In-sample cells with a 95% CI excluding zero: 1** (of 174). Chance alone at α=0.05 across 60
independent cells would produce ~3.

**That one cell, and the walk-forward's own in-sample winner (which also had a CI excluding zero),
BOTH failed out-of-sample.** This ledger exists so no number from §4–§7 is ever quoted without the
search depth attached — and §8 is why that matters.


---

## 12. Session addendum (2026-07-21 PM) - execution, not strategy

After the OOS failure the operator redirected: *"it's not about the strategy, it's about our
execution -- we need to be on the trade earlier."* All numbers below are HONEST fills (ask in /
bid out, market-on-touch) with the **live 07:00-16:30 ET window applied**, on 92 windows.

### 12.1 The live-window filter was worth ~0.6pp - and the study had been missing it

The study up to this point had **no trading-window filter**. On 2026-07-20, **10 of 15 trades fired
outside 07:00-16:30** (05:45, 17:48, 19:42 ET...) - times v2 cannot enter
(`strategy_schwab_1m_v2_entry_window_*` = 07:00-16:30) and Webull stop-market does not work.

| Chase entry, prox 2.0% / floor 2% / stop -5% | n | mean | win |
|---|---|---|---|
| all hours | 118 | **-0.474%** | 57.6% |
| **in-window only** | **54** | **+0.134%** | **68.5%** |

**The out-of-window trades were the losers.** "Five good trades, not fifty" is measurably right, and
every number in sections 4-8 is contaminated by unfillable trades.

### 12.2 Three entry concepts, measured

| # | Concept | Instrument | n | mean | win | mean STOP fill |
|---|---|---|---|---|---|---|
| 1 | bar closes near the trail | market @ bar close | 54 | +0.134% | 68.5% | -5.31% |
| 2 | buy on strength toward the trail | buy STOP above mkt | 69 | **-1.961%** | 55.1% | **-8.16%** |
| 3 | **buy a pullback at a better price** | **buy LIMIT below mkt** | **36** | **+0.565%** | **75.0%** | -5.27% |

**Concept 2 is dead**: it buys accelerating momentum and eats the full reversal (stops -8 to -9%). It
also pays a WORSE price than the chase - trail*(1-1%) sits ABOVE where the chase enters - so it does
the opposite of what was asked.

**Concept 3 (limit 1% below the signal close) is the best result measured**: +0.565% vs chase +0.134%
(+0.43pp), win 68.5% -> 75.0%. Winner/loser SIZES are unchanged (+2.34 / -5.27); the gain is that
**more setups survive** - a cheaper basis means a trade that would have stopped out instead reaches
the floor. An execution edge, as predicted. Interior optimum: -0.5% -> +0.033, **-1.0% -> +0.565**,
-2.0% -> -0.189.

WARNING - its cost: 18 of 54 in-window signals never filled, and **every one of them crossed**
(`no_fill_no_cross = 0`). The pullback rule systematically skips setups that run straight up. It still
wins on 8 days, but if those runners had been the big winners this reverses.

**CI [-0.53, +1.66] spans zero, n=36. NOT walk-forward validated. Same shape as the +0.935% that died
out-of-sample this morning. Do not treat it as an edge.**

### 12.3 A bug worth recording (found and fixed same session)

The first concept-2 run reported a **2.9% win rate and -9.3% stop fills** - impossible numbers, and the
tell that it was a bug not a result. Cause: the trail sits ABOVE price while short, so `trail*(1-X%)`
is normally **above the ask**; a buy LIMIT there is marketable and fills instantly at a worse price.
The correct instrument is a buy STOP (arm below, trigger on the way up) - the same constraint the OCO
work proved live (`STOP_PRICE_MUST_BE_GREATER_THAN_MARKET`). Both semantics now pinned in tests.
**Lesson: an impossible-looking win rate is a bug signal, not a finding.**

### 12.4 THE OPEN ITEM - the proximity rule has no MINIMUM

Operator forensics on ADVB 2026-07-20 12:47 (config A, -5.47% stop). Tape check 12:46:50-12:48:30:
**no print above the trail (8.0567); highest was 8.0399.** The entry at 8.04 WAS before the cross, by
~30s. **But it was only 0.2% below the line.**

| Bar | Close | Proximity |
|---|---|---|
| 12:46 | 7.6600 | 5.18% |
| **12:47** | **8.0300** | **0.33%** <- signal fired |

The bar jumped **+4.83% in one minute** and closed 0.33% under the trail. **The rule accepted it
because proximity has a MAXIMUM (2%) but no MINIMUM.** We bought sitting on the line with no cushion,
which is why the stop was hit.

**FIX TO TEST: a proximity BAND (e.g. 1.0% <= prox <= 2.0%)** - enter only with real room below the
line, rejecting spike bars that close on it. A 1% floor rejects the 12:47 trade outright. Stacks with
concept 3: the band gives room at SIGNAL time, the limit gives a better FILL.

### 12.5 Next, in order

1. **Walk-forward concept 3 (-1%)** - the same first-5/last-4 split that killed the previous winner.
   No further variations until this runs.
2. **Test the proximity band** (min 1.0%), alone and stacked with concept 3.
3. **Re-run sections 4-8 with the window filter applied** - those conclusions are drawn from data
   including ~2/3 unfillable trades.

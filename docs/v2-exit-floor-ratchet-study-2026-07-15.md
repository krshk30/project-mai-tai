# v2 CW exit — floor-ratchet study (2026-07-15)

> **STATUS: RESEARCH ONLY. NOTHING WAS CHANGED.** The live OMS exit and the backtest exit are
> untouched and still identical (both call `exit_logic/cw_exit.py::cw_exit_decision`). Operator
> directive 2026-07-15: *"No need to change backtesting engine / live engine right now… the live
> and backtest are the same, just leave it like that."* This document exists so the result can be
> **looked back on and re-run at any time**.
>
> Reproduce: `scripts/legacy/floor_sweep_2026_07_15.py` (see **How to re-run** at the bottom).

---

## The question (operator, 2026-07-15)

> *"We reached 2%, 2.5%, then it came back down to 2, we sold it, then the stock went 3%, 4%. …
> If the stock never pinned back down to 2%, if it's going up, we always going to raise the floor.
> We can take some free money. By doing this, are we gonna lose anything? Of course not."*

**The mechanic:** floor arms at **+2%** and then jumps up to the **highest whole % the bid has
reached** (peak +3.5% → floor +3%). Never below +2%, so a winner can never pay less than +2%.

---

## Verdict

**The operator is right on both counts, and the effect is small.**

1. **It is free.** The floor is `max(2%, …)`, so it can never book below +2%. Confirmed on every
   winner in the sample — no trade was ever worse than live.
2. **It does pay** — `+$0.04 / 4 days @ qty2`. It fires **once in 35 winners** (VMAR 07-14, peak
   +3.48% → exits +3.00% instead of +2.00%).
3. **Finer trails pay more, monotonically**, and are equally free.

| Mode | Floor rule | 4-day @qty2 | vs live |
|---|---|---|---|
| **A — LIVE (deployed)** | pinned at +2% | **+$4.70** | — |
| **B — OPERATOR** | `max(2%, int(peak))` → fires at +3% | **+$4.74** | **+$0.04** |
| E | `max(2%, peak − 0.50%)` | +$4.77 | +$0.07 |
| F | `max(2%, peak − 0.25%)` | +$4.83 | +$0.13 |
| **G** | `max(2%, peak − 0.10%)` | **+$5.03** | **+$0.33** |

Sample: **50 trades / 35 winners**, 2026-07-09..07-14, all confirmed symbols, qty 2.

### ⚠️ A correction worth remembering

The first run of this study reported **B = $0.00, "never fires."** That was **wrong** — it
implemented `max(2%, int(peak) − 1%)` (an invented −1 offset) which cannot move until **+4%**, not
the operator's actual `max(2%, int(peak))` which moves at **+3%**. **The operator rejected the
result on instinct and was correct.** Recorded here because the failure mode — *testing a
subtly-different mechanic than the one proposed and reporting it as the proposal* — is exactly the
class of error this file exists to prevent.

---

## Why the gain is small: the peak distribution

`PEAK%` = the highest bid reached **before** the pullback that exits us. This is the number that
decides which step sizes are reachable at all.

- **34 of 35 winners peak between +2.01% and +2.43%.**
- **Exactly one exceeds +3%** (VMAR 07-14, +3.48%) — the only trade the operator's mechanic fires on.
- Next highest: CRMT +2.81%, SOBR +2.78%.

So a **1%-granularity** ratchet is ~10× too coarse for the actual behaviour of these names; a
**0.10–0.25%** trail sits where the data actually lives.

### ⭐ The tails are real — and unreachable by any pullback exit

`AVAIL%` = the max bid available before the ATR flip. **Every winner takes +2% while the tail runs
away:**

| Trade | AVAIL% | PEAK% (before the dip) | Live took |
|---|---|---|---|
| VEEE 07-13 10:29 | **+96.17%** | +2.08% | +2.00% |
| VEEE 07-13 12:40 | +48.15% | +2.14% | +2.00% |
| SOBR 07-13 13:24 | +44.44% | +2.78% | +2.00% |
| JZXN 07-10 10:00 | +30.24% | +2.02% | +2.00% |
| LEDS 07-14 10:10 | +26.19% | +2.38% | +2.00% |
| AGEN 07-13 10:00 | +23.43% | +2.43% | +2.00% |
| SUNE 07-10 14:04 | +21.90% | +2.02% | +2.00% |
| EHGO 07-13 11:26 | +21.46% | +2.15% | +2.00% |
| VMAR 07-13 14:43 | +14.69% | +2.26% | +2.00% |
| JZXN 07-10 12:49 | +13.85% | +2.05% | +2.00% |
| SNAL 07-10 13:48 | +13.05% | +2.30% | +2.00% |
| MIMI 07-13 09:25 | +12.79% | +2.30% | +2.00% |
| MTVA 07-13 11:13 | +11.11% | +2.14% | +2.00% |
| NXTC 07-14 11:12 | +10.39% | +2.08% | +2.00% |

**The dip always precedes the run.** VEEE had +96% available but peaked at **+2.08%** before pulling
back through the floor — it ran *after* we were out. Two variants were tested to try to hold through
the shakeout, and **both are worse than live**:

| Variant | 4-day @qty2 | Why it fails |
|---|---|---|
| ROOM — `max(1%, int(peak)−1%)` | **+$3.05** (−$1.65) | buys room by giving up 1% on every fader; even then VEEE's +96% only yields +3% |
| RIDE — no target, −5% stop / ATR flip only | **−$23.18** | the ATR flip fires long after the fade |

**⇒ The +2% floor is not what's costing us the tail. The shakeout is.** No exit that sells on a
pullback can survive a dip that precedes a +96% run. The lever is **re-entry after the dip**, not
the floor.

---

## Method (what this models, and what it doesn't)

Entry gate **mirrors the live bot exactly** (copied from `atr_cw_v2_variants.sim`):
ATR(5, 3.5) BUY flip → trigger = max high of flip bar + next 2 → intrabar break on a real trade
print → rule 7 (price AND forming-bar low above the flip level) → **09:30–10:00 ORB skip** →
`scanner_confirmed_events` **[confirm→drop]** window → **7:00–16:30 ET** entry window (via the live
`is_fillable_et_session` helper) → **reclaim OFF** (1 entry per BUY-flip segment). Exit bounded by
the next ATR **SELL** flip. Trades bounded to their own 1-min bar `[low, high]` (drops odd-lot /
Form-T prints — the 07-14 FIX 2).

**Honest limits:**
- Floor exits are booked at the **floor level**; live sends a market sell and fills at the **bid**,
  which has been running slightly *better* than the model (live's one CW_FLOOR exit, NXTC 07-14,
  filled +2.47% vs the modelled +2.00%). This does not affect a **$0.00** or **+$0.04** delta.
- 50 trades / 4 days / qty 2 is a **thin** sample.
- 2026-07-09 contributes no winners (no qualifying entries under the live gate).

### 🔴 The 07-14 sweep harness is BROKEN — do not trust it without checking

`/home/trader/wt-atr-ab/atr_cw_v2_variants.py` **cannot import**:

```
ImportError: cannot import name 'cw_ratchet_exit_decision' from 'project_mai_tai.exit_logic.cw_exit'
```

`cw_ratchet_exit_decision` has **never existed in any commit on any branch**
(`git log -S --all` returns nothing). `run_exit_trailing` was refactored to "delegate to the SHARED
live helper" for backtest/live parity and the helper was never written. **The trailing-floor numbers
in the 2026-07-14 handoff entry (`+$3.25 / +$3.42 / +$3.70`) came from the pre-refactor local
implementation and are not reproducible today.** This study re-implements the entry/exit path
independently rather than repairing that file (operator: leave the engines alone).

---

## What to test next (not now)

1. **Mode G (0.10% trail)** — free by construction, best on every cut, `+$0.33/4d @qty2`
   (~+7% on +$4.70; ~+$1.65 @qty10). State it needs (`peak_profit_pct`) is **already persisted** on
   `oms_managed_positions`, so restart-safety is largely in place.
2. **Note what G converges to:** as the trail → 0 it becomes *"sell at the bid at first touch"* —
   which is what live did **before #453** replaced the hard +2% target with the pinned floor. The
   pinned floor books a flat +2.00%; the old hard target captured the actual bid (+2.1–2.2% typical,
   and gap overshoots such as VEEE 07-13 **+7.86%**). #453 gave that up, and the backtest could not
   see it because it books both modes at exactly +2%.
3. **Re-entry after the shakeout** — the only mechanism that can reach the +96% / +44% / +26% tails.
   Reclaim was turned OFF on 07-14 (#456) because *same-bar* reclaim bled; a *post-dip re-break*
   trigger is a different thing and is the real open question.
4. **All of it is second-order to the entry:** 15 wins @ ~+2.2% vs 13 stops @ ~−5.7% ⇒ **~72%
   payoff-implied breakeven win rate; live is at 50%.** No exit change closes a 22-point gap.

---

## How to re-run

```bash
# on the VPS (off-hours: this contends with the OMS event loop -- always nice it)
set -a; . /etc/project-mai-tai/project-mai-tai.env; set +a
export PYTHONPATH=/home/trader/project-mai-tai/src:/home/trader/wt-atr-ab:/home/trader/project-mai-tai
cd /home/trader/wt-atr-ab
nice -n 19 /home/trader/project-mai-tai/.venv/bin/python floor_sweep_2026_07_15.py \
    --dates 2026-07-09 2026-07-10 2026-07-13 2026-07-14
# add --syms NXTC SHPH --detail for a per-quote trace of every floor move
```

The script is versioned at `scripts/legacy/floor_sweep_2026_07_15.py` (copy it to
`/home/trader/wt-atr-ab/` to run — it reuses that harness's data plumbing: `atr_cw_v2`,
`atr_wait3_oos.fetch_quotes`, `scripts.atr_intrabar_run`).

Modes are declared in one `MODES` list at the top — add a row to test another floor rule.

[[project_mai_tai_v2_no_exits]] · [[project_mai_tai_cw_v2_exit_rnd_2026_07_14]] ·
[[project_mai_tai_v2_confirmed_window_ruleset]]

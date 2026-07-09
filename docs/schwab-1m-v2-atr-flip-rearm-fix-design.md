# schwab_1m_v2 ATR-Flip — "burn-the-fake, miss-the-real-flip" fix (DESIGN — review before code)

**Status:** DESIGN-FIRST, no code yet. Live-money strategy change → design → backtest-reproduce →
fix → validate → attended deploy. Operator review at each gate.

**Found by:** operator chart-reading of NVVE 2026-07-08 (a bar's HIGH grazes the ATR line then fades —
no cross — the bot fires the *fake*, then the *real* flip comes and we're already spent → miss).
Confirmed 3 ways: chart, live DB (one NVVE ATR emit 13:19 = the fake, rejected; **zero at 13:29** = the
real flip), and code.

---

## 1. Root cause (exact, in `schwab_1m_v2.py`)

The ATR-Flip entry is **variant B (touch)**: it fires when a bar HIGH (or intrabar quote) reaches the
prior-bar trail while short — *anticipating* the flip — gated by a 20s hold-confirmation. Two spots
interact to lose the real flip:

**(a) `atr_fired_in_short_seg` is claimed on the TOUCH, not on a successful entry.**
- `on_quote` L520 and `_update_atr_state` L725 set `atr_fired_in_short_seg = True` the moment a touch
  arms a hold — *before* the verdict.
- It only resets on a **SELL flip** (new short segment: L673 anchor, L740 flip) — **never on a
  rejected hold.**

**(b) `_resolve_hold` REJECT path (L551–552) returns None without resetting the guard.**
- False touch → arm → 20s later price fell back → `net_bps < threshold` → **skip (reject)** → but
  `atr_fired_in_short_seg` stays **True**.
- The guard now blocks every later touch in this short segment (L515, L721).

**(c) The confirmed BUY flip emits NOTHING (L744–746).** Variant B has *no* flip-close entry — the flip
only mutates state. So once the touch is consumed, the real flip produces no entry.

**(d) Bonus miss path — `_resolve_hold_on_bar` L562–565 `drop_flip`.** If a hold is pending when the bar
flips long, the hold is **dropped ("setup invalidated")** — i.e. the price sustained up and actually
*flipped* (the strongest possible confirmation) and we throw the entry away.

**Net:** a graze-and-fade before the true flip permanently consumes the segment's one entry, and the
true flip is un-enterable. Systematic on any choppy pre-flip name — a strong candidate for a chunk of
the v2 "death-by-hard-stop / breakeven-negative" pattern.

---

## 2. Requirement

1. **Keep rejecting false touches** (the hold-confirm already does this correctly — don't regress it).
2. **Never miss the confirmed BUY flip.** The short→long flip (close crosses the trail — the dots on
   the operator's TOS chart) is the ground-truth signal; it must always produce an entry when flat +
   off-cooldown, regardless of prior rejected touches this segment.
3. **No double-entry / no churn.** One position per flip; existing cooldown (5 bars) still applies.
4. **Flag-gated + off-by-default byte-identical**, per live-money discipline.

---

## 3. Current live-vs-backtest divergence (must fix too)

The v2 **backtest** (`backtest/v2_sim.py` / `v2_atr_param_sweep.py::_touches`) is a **separate
implementation** from the live strategy and has **drifted**:
- It **fills every touch** (no hold-confirm model) → it invents trades the live bot rejects (this is why
  the NVVE backtest showed a −$1.40 fill at 13:19 that the live bot *rejected*).
- Its own `fired` guard also resets only on SELL → same miss, plus the extra fills.

So "test the fix in the backtest" is only trustworthy once the backtest **mirrors** the live decision.
Two ways (recommend the first, like ORB PR #403):
- **(SoT) Extract the ATR-Flip entry decision into a shared leaf** in `strategy_core/` that BOTH
  `schwab_1m_v2.py` and the backtest import — single source of truth (⚠ the file's "touch ONLY this
  file" rule needs the operator's blessing to extend to a shared leaf; ORB already set this precedent).
- **(Port)** Pragmatic first step: port the hold-confirm verdict + the flip-backstop into `simulate_v2`
  so it *behaves* identically, pinned by a parity test. Less ideal (two copies) but unblocks validation.

---

## 4. Proposed fix (strategy) — two changes, guard redefinition

**Change 1 — reset the guard on a rejected/dropped hold.** Redefine `atr_fired_in_short_seg` to mean
*"an entry has SUCCEEDED (or a hold is genuinely pending) this segment"*, not *"a touch was attempted."*
- On a hold that resolves to **skip / skip_gated** (`_resolve_hold` L544, L551) and on **`drop_flip`**
  (`_resolve_hold_on_bar` L562) → set `atr_fired_in_short_seg = False` so a later touch can re-arm.
- Keep it True on **confirm / fallback_thin** (a real entry) and while a hold is **pending** (so the
  bar-close touch L721 doesn't double-arm during the window).

**Change 2 — flip-close backstop (the actual "don't miss the real flip").** On the confirmed **BUY
flip** (L744–746), if `atr_fired_in_short_seg` is False AND no hold is pending AND flat + off-cooldown
→ **emit the ATR-Flip entry at the flip-bar close** (variant-A-style backstop). This guarantees the real
flip is taken even if every touch this segment was rejected or none occurred.
- Reuse `_build_hold_draft`-style intent construction with a new mode tag e.g. `"flip_close"` for
  telemetry.
- Also handles path (d): if a hold is pending at the flip, resolve it as **confirm** (the flip *is* the
  confirmation) instead of `drop_flip` — OR simply clear the pending and let the backstop fire (choose
  one to avoid double-emit; see edge cases).

**Why a backstop rather than only re-arm:** re-arm alone still leaves a gap (the flip can occur with no
touch pending, e.g. the last graze's hold rejected at 13:28:40 and the flip is 13:29:00). The flip-close
backstop closes that gap deterministically. Re-arm (Change 1) still helps by allowing the *earlier*
confirmed touch to enter ahead of the flip when the move is real.

---

## 5. Edge-case + overlapping-path audit

| # | Edge case | Handling |
|---|---|---|
| 1 | Touch entered (holding) then the flip comes | flip-emit gated by `position_qty > 0` → no double-enter |
| 2 | Touch entered, scratched (hard-stop) BEFORE the flip, then flip | flat again → flip backstop re-enters; **cooldown (5 bars) gates churn** — verify cooldown covers it, else add a same-segment "already entered once" cap |
| 3 | Hold pending AT the flip bar | resolve as `confirm` OR clear+let backstop fire — pick ONE (proposal: clear pending on flip, fire the backstop; single code path, no double) |
| 4 | Many grazes in one segment | at most one hold per 20s (pending blocks new arm); re-arm only after each verdict → bounded, no thrash |
| 5 | `fallback_thin` (coverage < min_ticks) currently ENTERS (L547) | leave as-is this PR (separate "Path-B leak" decision); guard stays True on it (a real entry) |
| 6 | Variant A (not B) | unaffected — A already enters at flip-close; the backstop must be **B-only** so A is byte-identical |
| 7 | Flag OFF | byte-identical — all new logic behind `hold_confirm_enabled`/`atr_variant=="B"` (already the gate at L484) + a new sub-flag for the backstop so it can be rolled independently |
| 8 | Session anchor reset mid-hold (04:00) | anchor reset already clears the guard (L673); ensure it also clears any pending hold |

**Overlapping-path audit:** the touch guard is read/written in 4 places (on_quote arm L520, resolve
L537/L551, on_bar touch L725, flip resets L673/L740). The redefinition must be consistent across ALL of
them — a single helper `_claim_segment(state, on: bool)` to centralize, so no path is missed (this was
the #237 streamer-regression lesson: audit every path that touches the mutated state).

---

## 6. Validation plan (failing-test-first)

1. **Reproduce the miss (RED):** a unit test on the NVVE 07-08 bar/quote sequence (or a synthetic
   graze-then-flip fixture) asserting the CURRENT behavior: fake touch taken/rejected at the graze, and
   **no entry at the real BUY flip** — i.e. the bug, pinned.
2. **Backtest faithfulness (RED→GREEN):** a parity test that `simulate_v2` (with the hold-confirm model)
   reproduces the live decision on the fixture (rejects the fake, currently misses the flip).
3. **Apply the fix → GREEN:** the same fixture now enters at the confirmed flip (12:26 / 13:29), still
   rejects the fake, no double-entry.
4. **Determinism/oracle pin unchanged:** `_update_atr_state` still matches `compute_atr_trail`
   (the flip TIMES don't change — only whether we ENTER on them).
5. **Byte-identical when off:** full v2 suite green with the backstop flag OFF.
6. **Re-run the 07-08 v2 sheet** post-fix: NVVE should now show the real 13:29 flip entry (or a clean
   flat if gated), and the fake 13:19 should NOT be a fill.

---

## 7. Rollout

- New sub-flag `strategy_schwab_1m_v2_atr_flip_rearm_enabled` (default **False** = byte-identical).
- Ship the backtest-faithfulness + tests first (no live behavior change), review the reproduced miss.
- Then the strategy fix behind the flag; validate in backtest on multiple days (not just NVVE).
- **Attended deploy** (fleet-flat, v2-only choreography), operator GO, per the live-money discipline.
- Rollback = flag False + restart.

---

## 8. Open questions for the operator (before code)

1. **SoT vs Port** for the backtest (§3): extract a shared ATR-entry leaf (best, needs the
   "touch-only-this-file" waiver), or port the hold-confirm into the backtest (faster, two copies)?
2. **Edge #2 (scratch-then-flip re-entry):** cap at one entry per short segment even for the backstop,
   or allow re-entry after a scratched touch (cooldown-gated)? (Churn vs. missing a second leg.)
3. **`fallback_thin` (Path-B, edge #5):** leave in this PR, or fold the known "Path-B leak" decision in?

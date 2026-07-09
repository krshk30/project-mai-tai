# schwab_1m_v2 ATR-Flip — "burn-the-fake, miss-the-real-flip" fix (DESIGN v2 — review before code)

**Status:** DESIGN-FIRST, **no code written.** Live-money strategy change. Its own **new PR** — NOT
PR #403 (that's ORB: different bot/broker/strategy; mixing breaks one-change-at-a-time). Sequence:
design → your review → backtest-faithful + RED test → live fix → GREEN → attended deploy.

**Found by** operator chart-reading NVVE 2026-07-08: a bar HIGH grazes the ATR line then fades (no
cross) → bot fires the *fake* → the *real* flip comes → we're spent → **miss.** Confirmed: chart; live
DB (NVVE = one ATR emit 13:19 = the fake, **rejected**; **zero at 13:29** = the real flip); and code.

---

## 1. Root cause — verified by reading the LIVE code directly (`schwab_1m_v2.py`, not `_touches`)

The ATR-Flip entry is **variant B (touch)**. Two live paths detect the touch, both guarded by one flag:
- **Intrabar** (`on_quote` L512–528): a quote crosses the resting trail while short → arms a 20s
  hold-confirm; **sets `atr_fired_in_short_seg = True` on the ARM** (L520).
- **Bar-close** (`_update_atr_state` L717–725): `cur.high >= prev_trail` while short → touch; same flag.

The flag `atr_fired_in_short_seg` **only resets on a SELL flip** (L673 anchor, L740 fresh short seg).
The three failure points:
- **(a) `_resolve_hold` REJECT (L551–552)** returns None but **does NOT reset the flag** → the segment
  is permanently "spent" after one *rejected* touch.
- **(b) The confirmed BUY flip emits nothing in variant B.** `_maybe_atr_emit` variant-B (L811–814)
  fires only on `touch`; the BUY flip (L744–746) just mutates state. Once the touch is spent → no entry.
- **(c) `_resolve_hold_on_bar` `drop_flip` (L562–565):** a hold pending when the bar flips long is
  **dropped** — i.e. the price sustained up and *actually flipped* (the strongest confirmation) and we
  throw the entry away.

**Verified interactions (per your instruction #1):**
- **Cooldown / one-position:** the ATR emit is reached only when **flat + no cooldown** — gated at the
  caller (L891–894 under-warm path; same in the warm path) and re-checked inside `_resolve_hold`
  (L542). Cooldown = 5 bars, decremented every bar (L892/L906). So any fix in `_maybe_atr_emit`
  inherits flat+cooldown for free.
- **Key simplifier:** **variant A ALREADY fires the flip-close entry** (L807–810: `flip=="BUY"` →
  entry at `cur.close`). So the fix is not new machinery — it's *"let variant B fall back to variant
  A's flip-close entry when its touch didn't enter this segment."*
- **Oracle pin:** `_update_atr_state` matches `compute_atr_trail` (determinism test). The **flip TIMES
  do not change** — only whether we *enter* on them. So the ATR-line pin is untouched by this fix.

---

## 2. Frequency (per your instruction #2) — Polygon 5/3.5, 392 confirmed name-days, ~10 days

| metric | value |
|---|---|
| real BUY flips | **5,197** |
| flips preceded by an earlier graze (fires variant-B first) | **1,270 = 24%** |
| daily range | **22–27%** (steady, not a tail) |

**~1 in 4 real ATR flips is at-risk**: an earlier graze fires the touch first, and under the current
guard, *if that graze's hold-confirm rejects, the real flip is un-enterable.* The actual miss rate is
the subset whose graze rejects (a false touch) — and the hold-confirm exists precisely to reject false
touches, so a large share of the 24% become real misses. This is a **material, systematic** entry
defect, and it strongly implicates the "v2 death-by-hard-stop / breakeven-negative" verdicts (§9).

---

## 3. Architectural decision (settled): keep the two impls **for this PR**, extract the shared module **next**

Your framing is exactly right — ORB has one shared `orb_tick_entry.py` (can't drift); v2 has two impls
(`schwab_1m_v2.py` + `_touches`/`v2_sim`) pinned by parity, and **parity mistook agreement for
correctness** (both share this defect). Long-term the v2 entry SHOULD be one shared module.

**Decision: do NOT extract the shared module inside this bugfix PR.** Rationale:
- Extracting the ATR entry (deeply woven into `SymbolState` + the `on_bar`/`on_quote` flow +
  hold-confirm timing) into a pure leaf, **byte-identical**, is a large behavior-sensitive refactor of a
  **live-money** strategy. Bundling it with the bugfix violates one-change-at-a-time *within* the PR and
  balloons risk/review.
- **But we honor the anti-drift intent:** the bugfix PR fixes **live + backtest together** (same logical
  change, no window where they diverge) AND **re-pins the parity/golden test to the CORRECTED behavior**
  (§7) — so parity can no longer certify the bug.
- **Follow-up (separate PR, logged):** extract `strategy_core/atr_flip_entry.py` as the single shared
  entry module (consumed by both live + backtest, ORB-style), done **characterization-first**
  (green on current behavior → refactor → prove identical). That permanently eliminates the drift class.

Net: **one PR, one logical change (live + backtest), parity re-pinned to correct** now; **shared-module
extraction next**, characterization-tested.

---

## 4. The fix (three coordinated changes, all flag-gated)

New sub-flag **`strategy_schwab_1m_v2_atr_flip_rearm_enabled`** (default **False** = byte-identical).

- **C1 — re-arm on rejection.** In `_resolve_hold`, on **skip / skip_gated** (L544/L551) and in
  `_resolve_hold_on_bar` on **`drop_flip`** (L562), set `atr_fired_in_short_seg = False`. Redefine the
  flag to mean *"an entry SUCCEEDED, or a hold is genuinely pending"* — not *"a touch was attempted."*
- **C2 — variant-B flip-close backstop.** In `_maybe_atr_emit`, for variant B, if
  `atr_signal["flip"] == "BUY"` AND `not atr_fired_in_short_seg` AND no hold pending → emit at
  `cur.close` (reuse variant A's L810 path, tagged `mode="flip_close"`). Guarantees the real flip is
  taken when flat + off-cooldown + no prior successful entry.
- **C3 — flip-while-pending = confirm, not drop.** When a hold is pending and the bar flips long,
  **clear the pending and let C2 fire the flip-close** (single entry path; do NOT also resolve the hold
  → avoids double-emit). Replaces the `drop_flip` miss.

Centralize the flag writes behind one helper `_claim_segment(state, on)` so all 6 touch-points
(on_quote arm, resolve skip/reject, on_bar touch, both flip resets) stay consistent (the #237
overlapping-path lesson).

---

## 5. Edge-case + overlapping-path audit

| # | Case | Handling |
|---|---|---|
| 1 | Touch confirmed → holding → flip comes | flip backstop gated by `position_qty>0` → no double |
| 2 | Touch entered, **scratched** (hard-stop) before flip, then flip | flat again → **cooldown (5 bars) gates churn**; OPEN Q: also cap one *successful* entry per short segment? |
| 3 | Hold pending AT the flip bar | C3: clear pending, C2 fires once (no double) |
| 4 | Many grazes in one segment | ≤1 hold / 20s (pending blocks new arm); re-arm only after each verdict → bounded |
| 5 | `fallback_thin` (coverage < min_ticks) currently ENTERS (L547) | leave as-is (separate Path-B decision); counts as a real entry → flag stays True |
| 6 | Variant A | untouched — the backstop is **B-only**; A stays byte-identical |
| 7 | Flag OFF | byte-identical: C1/C2/C3 all behind `rearm_enabled` |
| 8 | 04:00 session anchor mid-hold | anchor reset already clears the flag (L673); also clear any pending hold there |

---

## 6. Backtest faithfulness (the measuring instrument) — fix FIRST, in this PR

`v2_sim`/`_touches` currently **fills every touch (no hold-confirm)** → it invents trades the bot
rejects (the NVVE −$1.40 at 13:19). Before validating the live fix, the backtest must **model the
hold-confirm rejection** (the 20s net_delta verdict → skip false touches) AND the C1/C2/C3 re-arm.
Then the backtest measures what the bot actually does. (This is why it's step 1 of the PR.)

---

## 7. Validation — failing-test-first, and **re-pin parity to CORRECT behavior**

1. **RED:** on the NVVE 07-08 fixture (graze-then-flip): assert the *current* behavior — fake taken/
   rejected at the graze, **no entry at the real BUY flip.** Pins the bug.
2. **Backtest-faithful:** `simulate_v2` with the hold-confirm model reproduces the live decision on the
   fixture (rejects the fake, currently misses the flip).
3. **Apply the fix → GREEN:** same fixture now enters at the confirmed flip (13:29), still rejects the
   fake, no double-entry.
4. ⚠ **Re-pin the "v2 touch parity" golden test to the CORRECTED behavior** (real flip fires) — NOT to
   whatever the code emits post-change. Parity-to-a-bug is exactly what hid this; the golden must encode
   *intended* behavior, verified by hand against the chart, not the code's output.
5. **Byte-identical off:** full v2 suite green with `rearm_enabled=False`; oracle/determinism pin
   unchanged.
6. Re-run the 07-08 v2 sheet: NVVE now shows the 13:29 flip entry (or a clean flat if gated), no 13:19
   fill.

---

## 8. Rollout

- Flag `strategy_schwab_1m_v2_atr_flip_rearm_enabled` default **False**. Golden green, CI pass.
- Ship backtest-faithful + RED test first (no live behavior change) → you review the reproduced miss →
  then the live fix behind the flag → validate on multiple days.
- **Attended deploy** (fleet-flat, v2-only choreography), operator GO. Rollback = flag False + restart.
- **Nothing deploys until reviewed.**

---

## 9. After the fix — re-run D3 and D5 on the CORRECTED entry

The "no edge" verdicts for D3/D5 were measured on the **broken** entry (24% of flips missed, fakes
filled in backtest). Once the entry is corrected + the backtest is faithful, **re-run D3 and D5** — the
edge conclusions must be re-measured on the fixed entry, not carried forward.

---

## 10. Open questions for your review (before any code)

1. **Edge #2:** after a touch enters then scratches, allow the flip backstop to re-enter (cooldown-
   gated) or cap at one *successful* entry per short segment? (Catch-second-leg vs. churn.)
2. **`fallback_thin` / Path-B (edge #5):** leave in this PR, or fold the pending "Path-B leak" decision
   in now?
3. **Confirm the architectural call (§3):** two-impls-fixed-together + parity-re-pinned now, shared
   `atr_flip_entry.py` extraction as the next (characterization-first) PR — agree?

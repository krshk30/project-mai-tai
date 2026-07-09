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

## 2. Frequency + TRUE COST (per your instruction #2) — Polygon bars+trades, hold-confirm MODELED, ~10 days

Of **5,191** real BUY flips, **1,267 (24%)** are graze-first (at-risk). Modeling the live hold-confirm
verdict (20s net_delta + tick coverage) on the FIRST graze of each:

| verdict of the first graze | count | share of grazes |
|---|--:|--:|
| **confirm** — real touch, entered EARLY | 226 | 18% |
| **fallback_thin** — Path-B bar-close fill (NOT a miss; excluded) | 390 | 31% |
| **REJECT → real flip MISSED** | **651** | **51%** |

**→ TRUE COST = 651 missed real flips / 10 days = 12.5% of ALL ATR BUY flips (~65/day).**

**Frame it precisely — this is missed SIGNAL, not missed profit.** We do NOT know those 651 flips were
winners. The correct claim: **the D3/D5 "no-edge" verdict was measured with 12.5% of real flips ABSENT,
so it is uninterpretable.** The fix makes the *measurement* honest; the re-run (§9) decides whether the
flip actually has edge. This is **not** "the fix recovers 12.5% of edge."

**The 18% "confirm" cases matter:** variant B's touch-anticipation genuinely *works* when the move is
real (early entry on a sustained touch). So the fix is **"stop it burning the segment on a fake,"**
NOT "kill variant B."

**⚠ Path-B masking interaction — LOAD-BEARING ordering (see the Path-B ticket,
`schwab-1m-v2-path-b-decision.md`):** the 390 `fallback_thin` fills mean the known Path-B leak has been
**masking this re-arm bug 31% of the time** — the two defects partially cancel. If Path-B is ever closed
(hold-confirm made a hard gate) **before** the re-arm fix lands, those 390 fills become misses too:
**651 + 390 = 1,041 ≈ 20% of all signal.** Therefore **fix re-arm FIRST, then revisit Path-B — never the
reverse.**

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

The D3/D5 "no-edge" verdicts were measured with **12.5% of real flips absent** (missed) and the backtest
**filling fakes the bot rejects** — so they are **uninterpretable**, not "13% too low." Once the entry
is corrected AND the backtest is faithful, **re-run D3 and D5** on the fixed entry. The re-run — not this
fix — is what decides whether the ATR flip has edge. Do NOT carry the old verdicts forward, and do NOT
claim the fix "adds X% edge": it makes the measurement honest; the edge question is then open again.

---

## 10. Open questions for your review (before any code)

1. **Edge #2:** after a touch enters then scratches, allow the flip backstop to re-enter (cooldown-
   gated) or cap at one *successful* entry per short segment? (Catch-second-leg vs. churn.)
2. **`fallback_thin` / Path-B (edge #5):** leave in this PR, or fold the pending "Path-B leak" decision
   in now?
3. **Confirm the architectural call (§3):** two-impls-fixed-together + parity-re-pinned now, shared
   `atr_flip_entry.py` extraction as the next (characterization-first) PR — agree?

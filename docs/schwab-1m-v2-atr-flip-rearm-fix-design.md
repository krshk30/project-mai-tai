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

**⚠⚠ 651 is a DENSE-POLYGON UPPER BOUND, not the live count.** The live bot + the v2 backtest run on the
**sparse Schwab feed**, where many of those grazes have thin coverage and hit `fallback_thin` → ENTER
(not skip). So the live skip-miss count is **well below 651**. From the **DB (06-24..07-08)**: 55 ATR
emits → 26 filled, **27 rejected + 2 cancelled = 29 emits (53%) opened NO position.** The 651 does not
travel unqualified.

**⚠ Second, independent source of misses (DB-confirmed).** The 29 non-filling emits cluster in
**API-open-restricted names** (AZI 5/5 rejected, CUPR/JEM/TDTH 2/2, TC/DXF/EHGO/DGNX/DSY/UPC/BTCT/LGCL/
IOTR/BYAH/NVVE 0-filled). Every one **claimed its segment on an emit that could never fill** → the
segment's real flip was missed. This is NOT the hold-confirm-skip path — it's **emit-without-fill** —
and it's exactly why the fix must be stated as an *invariant* (§4), not a per-path patch.

**⚠ D3/D5 corruption is a MIX, not "12.5% of flips absent."** On the Schwab-feed backtest it's *fake
grazes entering via `fallback_thin`* + *some real flips missed* + *restricted-name emits consuming
segments*. Different mechanism than the Polygon counterfactual — still **invalid**, but do not describe
it as "12.5% missing." The re-run (§9) is what re-measures on the corrected entry.

**⚠ Path-B masking — LOAD-BEARING ordering (see the Path-B ticket, `schwab-1m-v2-path-b-decision.md`):**
`fallback_thin` fills have been **masking the re-arm bug** — the two defects partially cancel. If Path-B
is ever closed (hold-confirm made a hard gate) **before** the re-arm fix lands, those masked fills become
misses too. **Fix re-arm FIRST, then revisit Path-B — never the reverse.**

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

## 4. The fix — ONE INVARIANT (+ backstop), flag-gated

New sub-flag **`strategy_schwab_1m_v2_atr_flip_rearm_enabled`** (default **False** = byte-identical).

**THE INVARIANT (replaces the old 3-rule patch):**
> **The segment guard is claimed only when a position is actually OPENED (a fill).**

Hold-confirm reject, emit-without-fill (restricted names), `drop_flip` — all are *"claimed by something
that wasn't an entry"* and must NOT consume the segment. One statement covers both miss sources (§2),
tighter than enumerating rejection paths.

**But claiming strictly on-fill leaves a double-entry gap** (a working order isn't yet a position, so the
caller's flat/cooldown checks don't cover it). So the guard is a **3-state pending-order lifecycle**, not
a bool:

| guard state | set when | meaning |
|---|---|---|
| **UNCLAIMED** | initial / after release / new short seg | free to arm a touch or take the flip backstop |
| **PROVISIONAL** | an emit is sent (touch-confirm / fallback_thin / flip-close) | a working order exists → do NOT re-emit (prevents double-entry) |
| **CLAIMED** | a FILL lands (`position_qty > 0`) | segment done — one position |

Transitions: PROVISIONAL → **CLAIMED** on fill; PROVISIONAL → **UNCLAIMED (released)** on terminal
no-fill (broker reject / cancel / hold-confirm skip / drop_flip) → re-arm. This is what stops
"missed-entry fixed" from becoming "double-entry created" (which costs money).

⚠ **Open design point (verify in build):** does `schwab_1m_v2` consume order-terminal events to trigger
PROVISIONAL→UNCLAIMED? If yes, drive it off that. If NOT, release on a bounded timeout (emit produced no
`position_qty` within N s / M bars) — explicit, no silent leak. TBD which.

**Backstop:** in `_maybe_atr_emit` variant B, if `flip=="BUY"` AND guard is UNCLAIMED AND flat +
off-cooldown → emit the flip-close (reuse variant A's L810 path, `mode="flip_close"`). Guarantees the
real flip is taken when nothing entered the segment.

Centralize all guard writes behind one helper `_claim_segment(state, to_state)` so every touch-point
stays consistent (the #237 overlapping-path lesson).

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

## 7. Validation — RED suite (a)+(b)+(c), and **re-pin parity to CORRECT behavior**

RED first (assert intended behavior; fails on current code; GREEN after fix). Synthetic proves branch
isolation; **real data proves truth** (a synthetic-only suite is how parity-to-a-bug happened):

- **(a) Core re-arm bug — synthetic fixture.** Dense graze → net_bps < 5 → **hold-confirm skip** → real
  BUY flip. RED: current code consumes the segment on the skip, misses the flip. GREEN: guard released
  on skip (UNCLAIMED) → flip-close backstop enters; no double-entry.
- **(b) Guard-on-fill invariant — synthetic fixture.** An emit that resolves to **no fill** (broker
  reject / cancel) must NOT consume the segment. RED: current code claims on emit → flip missed. GREEN:
  PROVISIONAL released on terminal no-fill → flip taken. (The restricted-name / NVVE-live mechanism.)
- **(c) Real-data golden regression.** Pull ONE actual name-day from the graze-first set, **hand-verify
  the flip times against the TOS chart**, pin it as a golden. RED now, GREEN after fix. Truth, not
  coder-intent.
- **Backtest-faithful (step 1):** confirm `simulate_v2` already models the hold-confirm skip (it does,
  L205–206) and extend it to the pending-order lifecycle so it reproduces the live emit/fill outcome
  (incl. restricted-name no-fills) — pinned by (c).
- ⚠ **Re-pin the "v2 touch parity" golden to the CORRECTED behavior** (real flip fires), hand-verified
  vs the chart — NEVER to the code's post-change output. Parity-to-a-bug is what hid this.
- **Byte-identical off:** full v2 suite green with `rearm_enabled=False`; oracle/determinism pin
  unchanged (flip TIMES don't move — only whether we enter).
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

# A1 — THE OVERSELL CLASS, AND THE TWO WAYS AN EXIT DIES

**Status: DESIGN ONLY. Nothing built.** Live exit path ⇒ design-first, attended, one item at a time.
Derived 2026-08-04 from `broker_order_events`, which had carried the answer the whole time.

> ## ⛔⭐ THE FRAME (operator, 2026-08-04)
> **Every broker rejection is OUR defect.** The reject text is a defect report we already paid for.
> Tested against the whole population: **zero classes are market-caused.** They all say we sent
> something invalid — no price, no shares, no credentials, a stop on the wrong side, or shares our
> **own other order** had already reserved.
>
> ⛔ The reason lives in **`broker_order_events.payload->>'reason'`**, NOT `broker_orders.payload`.
> Querying the wrong table returns `(none)` and produces the false conclusion "we never capture it."

---

## 1. THE SETTLED NUMBERS — enumerated, never pattern-matched

Real money only (`live:schwab_1m_v2` + `live:orb`). Derived by enumerating every distinct
`trade_intents.reason` on a rejected sell — **no `split_part`, no `ILIKE`, no `(other)` bucket.**
Both looser methods produced wrong answers first (§7).

| exit rule | episodes | rejects | orders/episode |
|---|---|---|---|
| `CW_HARD_STOP` | **24** | **985** (87% of all) | **41.0** |
| `CW_FLOOR` | 26 | 114 | 4.4 |
| `HARD_STOP_NATIVE_BACKUP` | 16 | 17 | ~1 — a refusal, **not a storm** |
| `STOP_REJECTED_FALLBACK` · `V2_OVERNIGHT_FLATTEN` · `HARD_STOP` · `CW_FLIP` | 6 | 18 | ~1 |
| **`CW_TARGET`** | **0** | **0** | — |

**An episode = one (account, symbol, day).** 57 total, reconciling exactly:
FLOOR-ONLY 12 · STOP-only 10 · both 14 · neither-CW 21 = **57**, never-closed 10+6+5+7 = **28**.
⛔ Quote **FLOOR-ONLY 10/12** for any abandonment claim — in a floor+stop episode the non-closure
cannot be attributed to either rule.

**Worst state, post-#566 only: 4 episodes where a stop fired and no sell filled that day.**
⚠️ Pre-#566 (before 07-27/#566, 07-28/#572) never-closed counts are **unreliable** — an OCO close
left no `fills` row. 14 of 16 floor episodes sit in that era, so **mode 2's COST is unresolvable**
for its own period. Answer it prospectively; do not infer it.

---

## 2. THE STORM MECHANISM — a storm needs a PERSISTENT RESERVATION

Rejects/episode by day is **bimodal and stable**: either **1–6** or **62–129**, nothing between.
The largest (07-13, 129/ep) **predates the fan-out (~07-24), #625 (07-30) and #566 (07-27)**.

⇒ There is **no regression and no undercount**. An era split of 189/27 vs 885/15 was only ever
which side of 07-28 the storm days happened to fall on.
⇒ **Storm length is set by how long the shares stay reserved, not by retry count.**
⇒ ⛔ **#608's consecutive-failure bound caps the NOISE and cannot be the fix.** It was promoted on
reading ~41/episode as the problem; the problem is what is holding the shares. **Demoted.**

---

## 3. A1a / A1b — E5 has two sources, derived two independent ways

409 Schwab oversell rejects. Split by whether cancelled sells existed that day:

| day | oversell rejects | cancelled sells that day | source |
|---|---|---|---|
| 07-13 | **127** | 0 | **A1b** |
| 07-31 (KUST) | 126 | 12 | **A1a** |
| 08-04 | **115** | 0 | **A1b** |
| others | 41 | ~0 | mixed |

- **A1a — unconfirmed cancel (~126).** Our own cancelled-but-unconfirmed limit exits still reserve
  the shares. KUST 07-31: 12 cancelled LIMITs 09:26–09:33, then **125 rejected MARKET sells
  09:33:42–09:39:10, zero overlap, strictly sequential**, closing at 09:39:41.
- **A1b — a live protective leg still resting (~242).** No cancels involved; a working broker order
  holds the lot. 08-04's 115 is the 16:00 jam, independently traced to the EOD transition leaving
  broker OCO legs alive — **the same conclusion from two unrelated derivations.**

⭐ **KUST also settles the causal direction: E5 is DOWNSTREAM of the reprice churn, not upstream.**
The 12 cancelled limits carry **zero** oversell rejects. So **C1/P0a is not chasing a symptom** — it
targets the churn, which precedes and causes the oversell. P0a's scope stands.

⛔ **#625 does not cover this.** It fixed *"an unconfirmed cancel is not a cancellation"* in the
shared adapter — it corrects **our accounting**, not the **broker-side reservation**. KUST is the
day after it shipped. *The fix exists and is wired* is a different question from *the fix addresses
this defect.*

---

## 4. MODE 2 — the exit that is ABANDONED, not delayed

The same reject surface carries a **second, previously unseparated failure mode.**

**FLOOR-ONLY: 10 of 12 episodes never closed that day (83%).** `CW_HARD_STOP`: 11 of 24.
Reject volume **114 vs 985**. The loop does not try *less* on a floor — the floor **stops existing**.

### ⭐ The mechanism is a PRECONDITION ASYMMETRY (not a lost flag)

| | condition | behaviour under rejection |
|---|---|---|
| **hard stop** | `price ≤ entry×(1−5%)` — **absorbing**: only becomes *more* true as price falls | retries indefinitely ⇒ **41 orders/episode** |
| **floor** | armed only while `price ≥ entry×(1+target%)` — **transient** | ~**4.4 orders/episode**, then price leaves the arming zone and it can never re-arm |

⛔ **Two wrong root causes were proposed and both are dead** (§7): *"the flag is cleared on emit and
never re-arms"* fails because the floor demonstrably re-emits **4.38 times per episode** — one emit
would mean exactly 1. And *"the churn worked an order the strategy no longer considered armed"*
fails for the same reason.

⇒ **The floor is lost because the rejection window outlasts the condition that permits it.** The
position then runs with **no profit protection and only the −5% stop beneath it**.
⇒ A **RATCHET** — armed once, stays armed — would remove the transience. **But it is NOT the
mode-2 design question, and it must not enter A1 as plumbing:**

1. ⛔ **The ratchet is a STRATEGY change, not an execution fix.** "Armed once, stays armed" fires the
   floor at a price the current strategy does not permit. That is **P4.1, which the operator parked.**
   Offer it to him as an option; do not build it inside an execution item.
2. ⛔ **FIX THE RESERVATION FIRST.** Modes 1 and 2 share one driver — **reservation duration**.
   A1a/A1b may dissolve mode 2 without touching floor logic at all. Building a ratchet first adds
   permanent complexity to compensate for a defect we are in the middle of removing.

⇒ **Sequence: land A1a/A1b, then MEASURE whether mode-2 residue remains.** Decide the ratchet on
that measurement, not on this note.

⚠️ `disarm-on-emit` **is** real (`oms/service.py:2459` clears `_cw_floor_armed` and
`_cw_flip_pending` immediately after emit, unconditionally, with `close_on_fill` already threaded
into that very call, and #392 fixed exactly this submit-vs-fill shape on the same path). It is
worth fixing on the #392 precedent — but it is **not** what abandons the floor. Do not let it be
recorded as the fix.

### ⛔ `_cw_flip_pending` — ONE STRUCTURE, TWO DEFECTS, TWO FIXES
Cleared on the same line, so it looks like one item. It is not:
1. **disarm-on-emit** — same shape as above;
2. **armed for ONE account only** — the flip signal publishes a single `broker_account_name`, so the
   `("live:orb", sym)` row never matches. **27 arms vs 0; 7 `CW_FLIP` exit emits vs 0.**
⛔ **Disarm-on-fill will not fix the Webull deafness.** Keep them separate or the second gets marked
done by the first.

---

## 5. DESIGN CONSTRAINTS — decided before the design, not around it

1. ⛔ **Do not trade a reject for a longer unprotected window.** Waiting for cancel confirmation
   before selling stops the oversell and *lengthens the naked window on an already-triggered stop*.
   Safe against one failure, worse against the other. The fix must be an **atomic replace/OCO swap**,
   or **not cancelling at all while the exit is marketable** — which is P0a doing its job.
2. ⭐ **`CW_TARGET = 0` hands us the shape.** The target *is* the resting order and fills in place;
   floor and stop **emit a second sell alongside a live protective order** — precisely when the
   reservation bites. **Emit-a-second-sell collides; modify-the-resting-order cannot.** A1b should be
   an atomic modify of the resting protective order, which **dissolves the class** rather than
   bounding it.
3. ⛔ **No threshold or tolerance band-aids.** Price moves 30–50% in seconds.
4. ⛔ **Bounding the retry must resolve the reservation, never abandon the sell.** Giving up on an
   emergency exit is worse than retrying it.

## 6. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence |
|---|---|---|
| A1 | A1b: zero oversell rejects where a live protective leg exists | replay 07-13 / 08-04; both are known-bad tapes |
| A2 | A1a: zero oversell rejects following our own cancel | replay 07-31 (KUST) |
| A3 | The naked window does **not** grow | measure trigger→fill either side; ⛔ a fix that removes rejects by waiting **fails** |
| A4 | **Mode-2 RESIDUE is measured, not assumed** — after A1a/A1b land, re-run FLOOR-ONLY never-closed | ⛔ NOT "a floor exit rejected while armed still exits" — that phrasing presumes a mode-2-specific fix and forces the ratchet by the back door. Measure first, then decide |
| A5 | No change to the target path | `CW_TARGET` stays at 0 rejects — it is the control |
| A6 | Webull flip deafness is **still open** after A1 | ⛔ prove the second defect was not marked done by the first |

⭐ **A3 and A6 are the ones most likely to be skipped** — A3 because the reject count will look
great, A6 because the structure makes it look already fixed.

## 7. ⛔ METHOD — every number here was wrong at least once first

Recorded because the failure mode repeated all day: **a figure that looks like an observation but is
a calculation with an unexamined transform inside it.**

| wrong number | cause |
|---|---|
| "we never capture the reject reason" | queried `broker_orders`, not `broker_order_events` |
| "`missing reference_price` is the top live defect" | pooled PAPER with LIVE — it is 1588/1588 simulated |
| "A2 understated 8×" | same pooling; the handoff's ~3/day was right |
| "42 hard-stop episodes, understated 75%" | `ILIKE '%HARD_STOP%'` swept in a one-shot mechanism |
| "87% withdrawn" | the withdrawal was unnecessary; it was right |
| "the flag is cleared on emit and never re-arms" | it re-emits 4.38×/episode |

⇒ **Enumerate, never pattern-match. Split by account, never pool. State the window. A bucket named
`(other)` must be zero or enumerated. Classification must be tested, never read from a comment.**

## Related
[`v2-eod-oco-jam-design.md`](v2-eod-oco-jam-design.md) · [`oco-bracket-design.md`](oco-bracket-design.md) ·
`v2-premarket-exit-protection-design.md` *(PR #646)*

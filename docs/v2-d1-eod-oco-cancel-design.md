# D1 — CANCEL THE LAPSING OCO LEGS AT 16:00 — design note

**Status: DESIGN ONLY. Nothing built. No live change until the operator approves.**
Re-scoped 2026-08-06 after D1 shrank **three times**. This note states the shrunken scope honestly
up front, because the raw reject count sizes it wrong.

> ⛔⭐ **ACCOUNT VISIBILITY OF EVERY NUMBER: `live:schwab_1m_v2` ONLY.**
> `oversold/overbought` is Schwab's string; a query keyed to it is blind to Webull by construction.
> [[feedback_reject_query_states_account_visibility]]

---

## 0. ⛔ SIZE THIS CORRECTLY OR NOT AT ALL

**D1 is LOW FREQUENCY, HIGH SEVERITY. It is a near-single-incident fix.**

| | |
|---|---|
| rejects in scope | **115** |
| **days it fired** | **2 of 12** |
| **concentration** | **113 of the 115 are ONE incident (08-04 AAOG)**; 07-22 contributes the other 2 |
| names | AAOG, KUST |

### ⛔⭐ AND IT IS ALREADY MITIGATED — size the CURRENCY, not just the volume

**`MAI_TAI_OMS_V2_EOD_OCO_TRANSITION_ENABLED` was flipped to `false` on 08-04 evening as the
deliberate jam mitigation** (recorded then as a behaviour change separate from the #647 rollout; the
08-05 21:06 env mtime is re-persistence during that deploy). **It stays off.**

**Both of D1's days — 08-04 (113) and 07-22 (2) — predate the flip. There have been ZERO D1-class
rejects since.**

⇒ **D1's justification is "it removes the need for the mitigation", NOT "it fixes a live failure."**
A reader who sees 115 rejects and no mention that the class has been disarmed for two days will
rank this item wrong in the other direction.

⛔ **A reader who sees "115 rejects" without the per-day split will size this wrong.** It is not a
recurring drip. It requires a specific and uncommon conjunction — **a bracket lapsing while a
position is held through 16:00** — and when that happens it is violent: 113 rejections in eight
minutes, and ~0.6–1.0 percentage points of slippage per leg.

**Why it is still next in the queue despite being the smallest of the three:**
**it is the only one of the three whose mechanism is established.** A1a/slice-C's cause is
contradicted for its largest day (§5); A2's dominant population is an entry-cadence defect. D1's
premise was established by elimination on 2026-08-06 (§2). Taking a bigger item first would mean
designing against an unestablished cause — the exact trap avoided this morning.

---

## 1. SCOPE — after three shrinks

The board's `A1a/A1b (~409)` is the Schwab oversold class. It decomposes exactly:

| slice | rejects | days | owner |
|---|---|---|---|
| **16:00–16:30 boundary** | **115** | 2 | **D1 — this note** |
| after 16:30 (EH tail) | 10 | 4 | unassigned |
| mid-RTH 09:30–16:00 | **284** | 9 | **slice C — cause OPEN, see §5** |
| | **409** | | |

**Shrink 1:** the jam note's "426" was two defects; 313 were Webull's A2. → 113 Schwab on 08-04.
**Shrink 2:** mid-RTH rejects are a different mechanism. → boundary only.
**Shrink 3:** the boundary slice is 2 days, one of them n=2. → a near-single-incident fix.

---

## 2. THE PREMISE — established by elimination, and the residual gap named

D1's entire basis is: **an expired-but-not-cancelled OCO leg still RESERVES the shares.**

**08-04 AAOG, Schwab — every sell order of ours that day:**

```
08:14:01  filled     10:57:49  filled (oco_exit)     12:01:13  filled (oco_exit)
16:00:04  <- FIRST oversold reject
16:00:06  rejected   <- OUR FIRST SELL ATTEMPT, TWO SECONDS LATER
```

At the instant Schwab refused we **held 2 shares and had zero working orders of our own.**
⇒ The competing hypothesis — *our own 1–2 s retry storm was reserving the shares* — is dead: the
reject **preceded our first submission**. The block then cleared at **16:08:05** with no action by
us.

⇒ Nothing in our records other than the broker-side OCO legs can account for the reservation.
**The premise holds, and D1 therefore loses no protection — the legs were already dead.** That is
what keeps D1 clear of the never-lengthen-the-naked-window constraint.

⚠️ **RESIDUAL GAP, stated not buried:** the legs were never *directly observed* in a WORKING state.
OCO children are broker-created and never land in `broker_orders`, so our DB structurally cannot
hold them. **This is elimination, not observation.** Direct confirmation needs a broker
order-history read against a held position with a lapsed bracket — **the same opportunity window as
Gate 1**, and it is owed. ⛔ Do not build the cancel-by-broker-id path without it: it is the one
step that assumes the legs exist and are addressable.

---

## 3. THE CHAIN (unchanged from the jam note, restated for the shrunken scope)

1. **16:00:03** — `_v2_eod_oco_transition` releases the native-OCO stand-down on a still-held
   position and hands the exit to the software EH ladder.
2. ⛔ **The broker's OCO sell legs still RESERVE.** The transition's docstring reasons *"a
   session=NORMAL DAY order cannot fill in EH, so nothing is lost by letting it lapse."*
   **A leg that cannot FILL still RESERVES.** The handoff is to a ladder structurally unable to sell.
3. Price is at the stop → `CW_HARD_STOP` every 1–2 s → all rejected oversold.
4. ⛔ #608's bound never engages because the symbol is **genuinely HELD** — the accumulator resets
   every pass and `_V2_EXIT_ABANDON_AFTER_FAILURES = 8` is unreachable. That is **D2's** problem,
   on its own timetable.

⚠️ **CURRENT STATE CHANGE — verify before building.** `MAI_TAI_OMS_V2_EOD_OCO_TRANSITION_ENABLED`
is now **`false`** (env mtime 08-05 21:06 ET); the last transition line ever is 08-04 20:00 UTC.
**With the flag off, step 1 does not fire** — and the stand-down still releases organically via
`_refresh_native_oco_armed_state` (30 s confirmation age-out / ~5 s refresh / 90 s resolution grace,
all bounded, fail-open). Proven live: `08-05 16:09:03 ET [OMS-OCO-STAND-DOWN-CLEARED] GTE` →
`16:24:03 CW_FLIP filled`. ⇒ **D1 must be designed against the flag-OFF world**, or the flag's
status must be settled first. Building D1 as a modification to a transition that no longer runs
would be building against a dead path.

---

## 4. PROPOSAL — invert the ordering: never hand off until the handoff is real

Same principle as #647. Ordered, and each step gated on the previous:

1. **Read the broker for the position's live OCO legs.** The `childOrderStrategies` walk in
   `fetch_armed_native_oco_symbols` already finds them; it returns *symbols* and this needs a
   variant returning **order ids** — a new, small adapter surface.
2. **Cancel them.** `_cancel_order` (`schwab.py:709`) exists but is driven from an OMS order row,
   and these legs have none ⇒ needs a **cancel-by-broker-id** entry point.
   *(Proven reachable: an ad-hoc qty-safe script hit the same endpoint successfully on 08-04.)*
3. **Re-read the broker and confirm zero live sell legs.** Only then release the stand-down.
4. ⛔ **On failure: do NOT release.** Leave the stand-down in place and log loudly. A position whose
   legs we could not cancel is better left with the broker owning it than handed to a ladder that
   cannot sell.

⛔ **No protection is lost by cancelling** — the legs cannot fill in EH anyway (Schwab refuses a
STOP leg there; measured 08-04). The software ladder becomes the *only* owner, which is already the
transition's stated intent.

### ⭐ THE FORK IS DECIDED IN PRINCIPLE — the duration route, not the cancel route

The question was: cancel at 16:00, or **never emit a lapsing bracket at all** (give RTH brackets a
duration that dies cleanly at the close)?

**§0 decides it.** With the class already disarmed by a flag, this is not a cleanup task — the
question is **should an RTH bracket ever be able to lapse?** The cancel route is a smaller change
that leaves the class alive and the mitigation flag load-bearing forever. **The duration route
removes the class and makes the flag unnecessary.** Rule 12.

⇒ **Price the duration route first; the expectation is that it wins.** Steps 1–4 above become the
FALLBACK, to be built only if a bracket duration that expires at the close turns out to be
unavailable at the broker — an open API question, not an assumption.

⛔ Note what this does to §2's residual gap: **the duration route does not need the
cancel-by-broker-id path at all**, and therefore does not need the Gate-1 broker read to be built
safely. The Gate-1 read is still owed — for slice C (§5) and to *confirm* the premise — but it stops
being a blocker on D1 itself.

---

## 5. ⛔ SLICE C IS **NOT** A1b — its cause is OPEN

Recorded here so D1 is not mis-sold as covering it, and so the label does not stick by repetition.

**A1b would mean a live resting protective leg reserves the shares. For 283 of slice C's 284
rejects, no sell of ours was live at the instant of the reject.** (07-13 AGEN — 45 % of the slice —
was established on 08-05 as having no sell order of any kind live; that finding is what killed
route (c).)

**And its second-largest day had no bracket either.** 07-31 KUST, 125 rejects:
```
09:11:02 ET  buy filled (PRE-MARKET)
             [V2-OCO-EMIT] KUST SKIPPED (outside regular hours) -- plain entry,
                           software ladder owns the exit
09:26-09:31  our sells CANCELLED (the reprice churn), 11-19 s spans
09:33-10:42  125 oversold rejects
```
⇒ **No native OCO bracket ever existed for that position.** So slice C is neither our live orders
nor a live bracket.

**Leading candidate — a broker-side reservation we cannot see, in two flavours:**
(a) **lingering reservations from just-cancelled orders** — the churn cancels and re-submits on
11–44 s gaps; if Schwab's release lags our recorded cancel, the next order oversells. Self-inflicted
and tied directly to the exit-churn defect. (b) **pre-market shares not yet sellable.**

⛔ **BLIND SPOT IN MY OWN ELIMINATION TEST — do not read 283/284 as stronger than it is.** The test
keyed on `submitted_at <= T <= updated_at`, i.e. orders live *in our books*. **If Schwab's
reservation release lags our recorded cancel, the test says "not live" while the broker still
reserves** — which is exactly candidate (a). The test rules out orders we know were live; it cannot
rule out lingering broker-side reservations.

### ⭐ P0a (#633) AS A CANDIDATE FIX FOR SLICE C — half established, half not

**#633 merged 07-31 13:05 ET; the OMS restarted 13:06 ET.** Slice C essentially stops after it.

| | |
|---|---|
| ✅ **ESTABLISHED** | **07-31's incident PRECEDES the deploy** — KUST's 125 rejects ran **09:33–10:42 ET**, ~2.5 h before 13:06. With 07-13 (127), **89 % of slice C is pre-P0a.** |
| ✅ **ESTABLISHED** | **"We simply stopped trading" is RULED OUT.** Mid-RTH sell attempts/day: **7.6 pre-P0a** (excluding the two storm days) vs **11.7 post-P0a**. We traded *more*, not less. |
| ⛔ **NOT ESTABLISHED** | **The post-P0a silence is indistinguishable from chance.** Only **3 trading days / 35 attempts / 2 rejects**. Storms hit **2 of 13** pre-P0a days ⇒ **P(no storm in 3 days, given nothing changed) = 60.6 %**. On the weaker any-reject test, **P(at most 1 of 3 days) = 33.0 %**. Both are commonplace under the null. |

⇒ **Slice C CANNOT be retired yet.** The favourable half is real and worth recording; the conclusion
is simply not available at n=3.

⛔ **The mechanism test I wanted is not available from this data.** `broker_orders.status =
'cancelled'` appears on **only one day in the whole window (8, on 07-31)**, so cancel-rate cannot
serve as the before/after link between the churn and the reservations. Whatever records the exit
churn, it is not this column. [[feedback_authoritative_for_a_is_not_for_b]]

⚠️ Also: reject-count ÷ sell-attempts exceeds 100 % on 07-22 (13 rejects, 12 attempts). Reject
**events** and sell **orders** are different populations — one order can emit several events — so
that ratio is a shape, not a rate. Do not quote it as a probability.

**The cheapest route to settling it is already deployed:** Part 2's `[OMS-P0A-HOLD]` lens shipped
with #647 and has been on the box since 08-05 21:06 ET — **0 lines emitted so far, UNEXERCISED**.
Once it emits, holds can be correlated against reject absence directly, which beats waiting for
enough quiet days to accumulate.

### ⭐⭐ THE PRECONDITION TEST — dose-response, the strongest observational evidence available

⛔ **Better than accumulating quiet days: check whether the PRECONDITION recurred, not the outcome.**
If the setup stopped happening, quiet days say *nothing* — that would be unpowered for a
**structural** reason, and no number of further days would fix it.

**It recurred, on every post-P0a day, more often than on the storm day itself.**
`[V2-OCO-EMIT] SKIPPED (outside regular hours)` — an entry the emit path declined to bracket:

| day | SKIPPED lines | symbols | all `V2-OCO-EMIT` (denominator) |
|---|---|---|---|
| 07-31 | 2 | KUST ← *the storm* | 69 |
| 08-03 | 2 | UPC | 104 |
| 08-04 | 4 | AAOG, AMIX | 42 |
| 08-05 | **8** | BJDX, GTE | 99 |
| 08-06 | **9** | CLRO, PAVS, WYHG | 9 (partial day) |

*(marker confirmed present in deployed source — guarded against a false zero.)*

#### ⛔⭐ RETRACTED — THERE IS NO DOSE-RESPONSE

**An earlier revision of this note claimed a "~15× dose-response collapse" in retry attempts
(KUST 138 pre-P0a vs ≤9 post). That claim is WITHDRAWN.** It compared **one** pre-P0a position
against a few post ones. Pulling the **full** distribution — all 11 pre-P0a no-bracket positions,
not just the worst — dissolves it:

| era | attempts per no-bracket position (sorted) | median |
|---|---|---|
| **PRE-P0a** (n=11) | 2, 3, 3, 3, 4, 6, 7, 8, 9, **133**, **138** | **6** |
| **POST-P0a** (n=4) | 1, 5, 9, **117** | **7** |

**The medians are indistinguishable — 6 vs 7.** The distribution is **bimodal** (a quiet mode of
2–9, and storms), and the retracted claim had quoted its **maximum as though it were its level**,
then excluded the post-period storm on a per-lot argument the data cannot settle.

**Storm rate is no better:** pre **2/11 (18 %)**, post **1/4 (25 %)** counting AAOG, or 0/4 if
AAOG's storm belongs to its bracketed lot — and *that* is precisely what cannot be determined
(§ below).

⇒ ⛔ **On the evidence available, P0a is NOT shown to have reduced the churn.** The favourable
findings that survive are only these two: the precondition **did** keep recurring (so the test is
not structurally unpowered), and **"we stopped trading" is ruled out**. Neither is evidence of a
fix.

⭐ Note the shape of this correction: **widening the unit to the whole distribution retracted the
headline.** [[feedback_query_unit_must_match_hypothesis_unit]]

⚠️ Useful control inside the data: **KUST appears twice** — 07-15 with 2 attempts / 0 rejects and
07-31 with 138 / 125. Same symbol, same no-bracket precondition, opposite outcome. Whatever
distinguishes a storm from a quiet exit, **it is not the precondition alone.**

#### Per-opportunity rate, denominator extended backwards

Unit = **a position we actually hold with no bracket** (a filled outside-RTH buy on
`live:schwab_1m_v2`):

| period | no-bracket positions | escalated | storms | worst |
|---|---|---|---|---|
| PRE-P0a (…07-30) | 10 | **3 (30 %)** | 1 | 127 |
| 07-31 (straddles the 13:06 deploy) | 1 | 1 | 1 | 125 |
| **POST-P0a (08-03…)** | **4** | **0 (0 %)** | 0 | 0 |

⚠️ **3/10 vs 0/4 is p ≈ 0.33 (Fisher).** The *rate* comparison remains underpowered. The
dose-response, not the rate, is what carries the weight.

⭐ 07-13's AGEN storm sits inside that pre-P0a set — so **both** storms shared the no-bracket
precondition, which is what makes it a real precondition rather than a quirk of KUST.

#### ⛔ TWO UNIT TRAPS, recorded because each nearly produced a wrong answer

1. **symbol-day vs LOT.** `AAOG 08-04` reads as **117 attempts / 113 rejects on a "no-bracket" day,
   post-P0a** — which would sink the hypothesis. It is a **different lot**: the SKIPPED lines belong
   to the **08:14 pre-market** entry; the storm was the **13:28 RTH** lot, which *had* a bracket
   (proved by `[OMS-V2-EOD-OCO-TRANSITION] AAOG` at 16:00:03 — a line that exists only for a
   position with a native OCO). **That storm is D1's, not slice C's.**
   ⛔ **Caught only by a cross-check that happened to exist**, not by design.
2. **emit-evaluation vs POSITION.** `SKIPPED` fires even when Schwab then **rejects** the entry
   (UPC 08-03: SKIPPED logged, Schwab `rejected`, only the Webull leg filled). The log is a
   **superset**; the hypothesis needs a held position. The per-opportunity table uses the position
   unit deliberately.

⇒ [[feedback_query_unit_must_match_hypothesis_unit]] — **when two sources disagree, ask which unit
each measures before asking which is broken.** Five instances this week; not once was a source
actually broken.

#### ⛔⭐⭐ THE PER-LOT GAP IS THE BLOCKER, AND IT IS THE ATTRIBUTION ITEM

The symbol-day graining is not a caveat to note and move past — **it decides this note's headline.**
Whether AAOG's 113-reject storm belongs to its **08:14 no-bracket lot** or its **13:28 bracketed
lot** determines:

- which board item owns the storm — **slice C** or **D1**; and
- whether the post-P0a period has **0 storms or 1** — i.e. whether P0a looks like a fix at all.

It was settled here only by a lucky cross-check (`[OMS-V2-EOD-OCO-TRANSITION] AAOG` at 16:00:03, a
line that exists only for a position with a native OCO). **Nothing in the data model answers it.**

⭐ **This is the SAME defect that leaves ~9 trades a day as `close_candidate_*` instead of asserted
pairs: a sell is not linked to the entry lot it closes.** One fix, two workstreams unblocked.
⇒ **Re-frame the attribution work as unblocking ANALYSIS, not tidying REPORTING** — it caps the
resolution of the exit-churn study, the P0a validation and the slice-C decomposition alike.
⛔ The link must be **captured at emit time**, never inferred — FIFO already invented a −8.40 %
trade once. [[project_mai_tai_per_lot_attribution_gap]] · [[feedback_capture_attribution_never_infer]]

#### ⛔ STILL NOT A RETIREMENT — but the null's burden has changed

3 days · 4 no-bracket positions · **AMIX still threw 2 rejects**, and **AAOG threw 117 attempts /
113 rejects post-P0a** (lot ownership undetermined). The class is **not shown to be smaller at all**.

⛔ **The null's burden did NOT increase — I claimed it had, on the retracted dose-response.**
What is actually left:

| claim | status |
|---|---|
| the precondition kept recurring | ✅ established (2→9 SKIPPED/day) |
| "we simply stopped trading" | ✅ ruled out (7.6 → 11.7 attempts/day) |
| escalation rate fell | ⛔ 3/10 vs 0/4, **p ≈ 0.33** — underpowered |
| churn magnitude fell | ⛔ **RETRACTED** — medians 6 vs 7 |
| P0a fixed slice C | ⛔ **NOT SUPPORTED by any surviving evidence** |

⇒ **Do not re-rank the queue** until more post-P0a days accumulate, the lens emits, **or per-lot
attribution lands** — which would settle AAOG and is now the highest-value of the three.

---

⇒ **Slice C needs the same broker order-history read that §2's residual gap needs.** One
Gate-1-window read serves both. **Do not design slice C until it has one.**

---

## 6. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence required (observed, not inferred) |
|---|---|---|
| **A1** | at the transition the broker reports **zero live sell legs** before the stand-down is released | one timeline: cancel → **broker re-read confirms zero** → release |
| **A2** | a position held through 16:00 **can be sold at 16:00:05** | a successful EH exit, or a deliberate qty-1 manual sell that is accepted |
| **A3** | **zero** oversold rejects in the 16:00–16:30 window on a day with a held position | 08-04 (113) is the known-bad tape to replay against |
| **A4** | ⭐ if cancellation FAILS, the stand-down is **NOT** released | prove by **deliberate mutation** — force the cancel to fail, confirm the bracket keeps ownership |
| **A5** | D1 does not touch the flat / UNKNOWN paths #608 fixed | #608's tests stay green; NCRA's 145-retry case stays bounded |
| **A6** | Ship 2 stays green throughout | the managed row never lapses while we cancel and re-read |
| **A7** | ⛔ slice C is **not** claimed as covered | §5 |

⭐ **A4 is the one most likely to be skipped and most likely to bite** — failure paths are boring,
and a cancel that silently fails while the stand-down releases anyway reproduces the incident with
extra steps.

---

## 7. ROLLOUT
Attended · flag-gated, default **off** · deploy **after the close** · PR + Validate · explicit GO
before merge+restart · OMS-only (`stop strategy → restart oms → start strategy`) with the
pre/post-restart bar-gap checklist. ⛔ **No v2 restart** (Bug 2: `cw_entries_this_flip` is
unpersisted and re-issues the entry cap on every armed segment).

## 8. RELATED
[`v2-eod-oco-jam-design.md`](v2-eod-oco-jam-design.md) *(its 426 → 113; D2 stays there, own timetable)* ·
[`v2-a2-reverse-reject-design.md`](v2-a2-reverse-reject-design.md) *(the other 313)* ·
[`v2-premarket-exit-protection-rollout.md`](v2-premarket-exit-protection-rollout.md) *(Gate 1 — the owed broker read)* ·
[[project_mai_tai_schwab_eh_no_stop_leg]] · [[project_mai_tai_exit_churn_kust]] ·
[[project_mai_tai_v2_close_retry_sawtooth]]

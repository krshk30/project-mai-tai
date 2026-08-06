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

## 2b. ✅ PREMISE DIRECTLY OBSERVED 2026-08-06 — and it REVERSES a safety claim

The broker order-history read (read-only GET, no live conditions needed) closed the gap above:

```
1007463113230 FILLED   BUY  2.0/2.0 MARKET      NORMAL DAY  13:28:43 -> 13:28:43  TRIGGER
  1007463113233 EXPIRED SELL 2.0/0.0 STOP 4.26  NORMAL DAY  13:28:43 -> 16:08:04  <- child leg
  1007463113232 EXPIRED SELL 2.0/0.0 LIMIT 4.57 NORMAL DAY  13:28:43 -> 16:08:04  <- child leg
```

| | |
|---|---|
| 16:00:03 | transition releases the stand-down — *"RTH OCO expired at 16:00 ET"* |
| 16:00:04 → **16:08:03** | 113 oversold rejects |
| **16:08:04** | **both SELL legs EXPIRE** |
| 16:08:05 | the very next sell **FILLS** |

Each leg reserved 2 shares against a 2-share position. The block ended **one second** after the legs
died, with no action by us. **This is observation, not elimination.**

### ⛔⭐⭐ MECHANISM CORRECTION — the note and the transition log are BOTH wrong
*"RTH OCO expired at 16:00 ET"* is **false**. The legs expired at **16:08:04**. The transition handed
the exit to the software ladder believing the legs were dead **while they had eight minutes to
live**. The defect is not *"a leg that cannot fill still reserves"* — it is **a leg that was still
LIVE**. Different sentence, different fix.

### ⛔⭐⭐ SAFETY CLAIM REVERSED — this was mine, and it was wrong
An earlier revision of this note said:
> *"D1 therefore loses no protection — the legs were already dead."*

**FALSE. They were live.** Cancelling at 16:00 would remove a **working STOP at 4.26** on a held
position. ⇒ **The never-lengthen-the-naked-window constraint is BACK IN FORCE for D1**, and any
route that cancels must be judged against it.

### ▶ "LIVE" SPLITS IN TWO — and both halves are now ESTABLISHED
| | claim | status |
|---|---|---|
| **(a)** | the leg **RESERVES** the shares | ✅ **ESTABLISHED** |
| **(b)** | the leg **WOULD ACTUALLY EXECUTE** | ✅ **ESTABLISHED — IT IS FALSE** |

#### ✅ (b) ANSWERED FROM OUR OWN BARS — the stop was CROSSED while alive and did NOT fill
⛔ **`previewOrder` cannot answer this.** Preview validates a **newly submitted** order — it only
re-establishes what Probe P already showed. Whether an **existing** `session=NORMAL` `DAY` stop,
placed during RTH, triggers in the 16:00–16:08 tail is an **observational** question, and the data
was already loaded. AAOG's stop trigger was **4.26**; the leg was alive until **16:08:04**:

| bar (ET) | open | high | **low** | close | vol | |
|---|---|---|---|---|---|---|
| 16:00 | 4.3200 | 4.3200 | 4.3100 | 4.3100 | 2 602 | |
| 16:01 | 4.2800 | 4.2800 | 4.2800 | 4.2800 | 250 | |
| 16:04 | 4.2900 | 4.2900 | 4.2900 | 4.2900 | 751 | |
| **16:05** | 4.2486 | 4.3000 | **4.2486** | 4.3000 | **5 588** | ⭐ **BELOW the 4.26 trigger, on real volume** |
| 16:06 | 4.2625 | 4.2625 | 4.2625 | 4.2625 | 500 | |

**The 16:05 bar traded through the trigger on 5 588 shares while the leg was alive — and the leg
expired UNFILLED at 16:08:04 with `filledQuantity 0.0`.** It was also the **first** crossing since
the 13:28:43 entry, so there is no earlier opportunity to explain it away.

⇒ ⭐⭐ **THE LEG RESERVES WITHOUT PROTECTING.** Exactly as the EH-session rule predicts: Schwab will
not execute a STOP in extended hours, but it still holds the shares against one.

#### ⇒ THE SAFETY REVERSAL DISSOLVES — but the ORIGINAL WORDING STAYS WRONG
Cancelling those legs at 16:00 **costs nothing real**, and the never-lengthen-the-naked-window
constraint **does not bind D1**. The conclusion is restored — for a materially different reason, and
the note must **not** revert to the old sentence:

| | |
|---|---|
| ❌ *"the legs were already dead"* | **false** — alive until 16:08:04 |
| ✅ **"the legs were alive but INERT — they reserve and cannot execute"** | **observed** |

⚠️ **This is a property of the EH SESSION, not of expiry.** Any future change that lets a bracket leg
live into a session where it *can* execute re-arms the constraint. The finding is **not** "cancelling
brackets is always free."

⭐ **And it upgrades D1 from a slippage fix to a correctness fix:** for 8 minutes the position had
**no working protection at all** — the software ladder was blocked and the broker leg was inert.
That is a Class A *no owner* condition hiding inside what the board files as Class B.

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
unavailable at the broker.

### ⛔⭐ THAT API QUESTION IS NOW A CONCRETE BLOCKER, NOT A THEORETICAL ONE
The observed legs were `session=NORMAL`, **`duration=DAY`** — and **`DAY` did NOT mean 16:00. It
meant 16:08:04.** So "give RTH brackets a duration that dies cleanly at the close" may be
**inexpressible**: if Schwab's `DAY` already runs eight minutes past the close, the route needs a
specific close-time expiry the broker may not offer. **Establish what durations Schwab actually
accepts before committing to this route.**

### ⛔⭐ SWEEP OWED — "DAY orders are gone at the close" is an assumption we may have made elsewhere
`DAY` meaning **16:08:04** invalidates any logic that assumes a DAY order is dead at 16:00. Grep for
every place that reasons about end-of-day order expiry — the EOD transition is the one we know
about; **it is unlikely to be the only one.**

⛔⭐ **BUT THE TAIL ITSELF IS UNMEASURED — `n = 1`.** 16:08:04 is a **single observation**, not a
constant. The KUST/AGEN reads were checked for more expiry timestamps and returned **zero EXPIRED
orders on either day**, so nothing corroborates it.
⇒ **Do not let 16:08:04 harden into "the tail is ~8 minutes."** If the tail varies day to day, the
daily unprotected window varies with it, and any fix timed against a fixed offset is wrong.
⇒ **The sweep stands regardless** — it only needs "DAY ≠ 16:00", which one observation is enough to
establish. Sizing the exposure needs many; asserting the assumption is false needs one.

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

### ⭐⭐ LEADING CANDIDATE, NOW WITH A MECHANISM SEEN AT THE BROKER
The AAOG order history (§2b) surfaced something bigger than the question it was run for. Between
**09:54 and 10:43** on 08-04 there are **NINE `STOP_LIMIT` BUY brackets placed and cancelled roughly
every minute** — and **each one spawns TWO child SELL legs**, all cancelled within 60–90 s:

```
09:54:03  BUY STOP_LIMIT 4.32 TRIGGER   -> SELL LIMIT 4.38 + SELL STOP 4.08   both CANCELED 09:59:15
10:00:05  BUY STOP_LIMIT 4.31 TRIGGER   -> SELL LIMIT 4.37 + SELL STOP 4.07   both CANCELED 10:03:15
10:09:02  ... 10:13 ... 10:24 ... 10:26 ... 10:31 ... 10:33 ... 10:43  (nine in total)
```

⭐ **This is the resting-entry reprice churn, visible at the BROKER — and every cycle creates and
destroys a PAIR of SELL legs that our books never record**, because OCO children are broker-created
and never land in `broker_orders`.

⇒ **That is exactly the invisible reservation slice C has been hunting.** It is no longer a guess
about "lingering cancels": we can see the objects, we can see their cadence, and we can see that our
own records omit them entirely. It also explains why the elimination test (§ blind spot) could not
find them — it searched a table they never enter.

#### ⛔⭐⭐ TESTED IMMEDIATELY — AND FALSIFIED. The churn is NOT slice C's mechanism.
The same read was run against both slice-C storms. **Neither had a single bracket child:**

| | orders at the broker | depth-0 | **children** | cancelled BUY brackets |
|---|---|---|---|---|
| **KUST 07-31** | 157 | 157 | **0** | **0** |
| **AGEN 07-13** | 150 | 150 | **0** | **0** |

*(Sanity-checked: the 07-13 response held 190 orders across 8 symbols with AGEN the largest at 150,
and 07-31 held 454 including 179 children on **other** symbols — so the machinery finds children
when they exist. These zeros are real, not a mis-targeted query.)*

⇒ **Both storms were pure pre-market entries** — `[V2-OCO-EMIT] SKIPPED (outside regular hours)` —
so **no bracket was ever emitted and there were no children to reserve.**

#### ⛔⭐ WHY IT DIED — generalised from a case that COULD NOT APPLY
The hypothesis was built on **AAOG**, whose brackets exist *because it was an RTH resting entry*.
**Slice C's storms are pre-market entries with no bracket BY CONSTRUCTION.** The evidence came from
a population structurally incapable of exhibiting the thing being explained.

⭐ **That is the symbol-day-vs-lot error one level up:** there the *unit* of the query mismatched the
unit of the hypothesis; here the *population* did. Same family — check that the evidence population
can actually contain the mechanism before generalising from it.
[[feedback_query_unit_must_match_hypothesis_unit]]

⚠️ **The AAOG churn remains a REAL observation still looking for its own item.** Nine `STOP_LIMIT`
BUY brackets placed and cancelled every ~minute between 09:54 and 10:43, each spawning two child
SELL legs, all cancelled in 60–90 s — objects our books never record. **Do not let it disappear
just because it was disproved as slice C's cause.** It belongs with the resting-entry reprice-churn
work (the 12:1 order-churn item), where it is the first direct broker-side sighting.

### ⇒ SLICE C'S CAUSE IS NARROWER NOW, AND THE ELIMINATION IS BROKER-CONFIRMED
| candidate | status |
|---|---|
| a live order of ours | ⛔ excluded — 283/284 had none live |
| a live broker-created bracket child | ⛔ **excluded at the broker — zero children on both storms** |
| **pre-market shares not yet sellable** | ⭐ **the only surviving candidate** |

⭐ It also fits every other observation: the discriminator's necessary condition is exactly **AM
entry + sell attempted before 09:30**; the outcome is **binary** (0 or 125+, never a handful); it
**clears without action**; and price, liquidity, quantity, lot count and cadence are all irrelevant.
That is the signature of an **account-state flag**, not a race or a reservation.

⛔ **Still not established** — "surviving candidate" is not "cause". Settling it needs the broker's
*account/position* view during a pre-market hold (available vs settled vs held quantity), which is a
**different read** from order history and needs a live pre-market position.

### ⛔⭐⭐ THREE DISTINCT BROKER READS, THREE DISTINCT WINDOWS — one opportunity does NOT cover all
Listed explicitly because "one instrument, four questions" was **right about the instrument and
wrong about the window**, and assuming a single Gate-1 opportunity settles everything would stall
two of these indefinitely.

| # | read | what it settles | window required | status |
|---|---|---|---|---|
| **A** | **order history** (GET `/orders`) | D1's premise · AAOG's lot · the churn test | **none — retrospective, any time** | ✅ **DONE 08-06** |
| **B** | `preview_exit_only_oco` (**Gate 1**) | the exit-only OCO **shape** | v2 holds a **Schwab long during RTH**, shares **unreserved** and **ours** ⛔ not CYN | ⏳ opportunistic |
| **C** | **account / position detail** | ⭐ slice C's surviving candidate — available vs settled vs held | a **live PRE-MARKET hold** (07:00–09:30), i.e. the hazard condition itself | ⏳ not yet attempted |
| **D** | `previewOrder` on **durations** | whether a close-expiring bracket duration exists | likely a held position (Probe P's control rejected on the position check when flat) | ⏳ deprioritised — see §4 |

⭐ **A needed nothing and was free** — it should have been run days ago. **C is the one that matters
now**, and its window is the pre-market hazard condition itself, so it recurs most mornings.

### ⛔⭐⭐ THE RULE THIS EXPOSED — AGGREGATION IN THE **PLAN**, NOT IN THE DATA
Read A sat behind Gate 1's opportunity **for days, for no reason.** Four questions were bundled
under one label ("the broker read"), and **A inherited the group's worst constraint** — a live RTH
position it never needed.

⭐ **This is the aggregation bug class relocated from the measurement to the SCHEDULE.** Same shape
as the five data instances, one difference: **the data versions produce a wrong answer; this one
produces DELAY.** It is quieter, it never shows up as an error, and nothing forces its discovery.

⇒ **RULE: a bundled work item inherits the WORST constraint of its members. Before parking anything
behind a gate, check the ITEM's own requirement, never the group's label.**
[[feedback_aggregation_masked_the_event]] · [[feedback_authoritative_for_a_is_not_for_b]]

#### ▶ THE SAME PASS OVER THE REST OF THE QUEUE — at least two more are mis-parked
Gate 1 is currently treated as blocking four things. Checked against each item's **actual**
requirement:

| item | what it ACTUALLY requires | behind Gate 1? |
|---|---|---|
| **#647 Gate 2** (`rth_edge_bracket_enabled`) | Gate 1's shape proof — do not place an unproven shape on a live position | ✅ **YES, legitimately** |
| **#647 Gate 3** (stand-down re-arm) | Gate 2 proven — it reuses Part 1's emit wholesale | ✅ yes, transitively |
| **P0a VALIDATION** | the `[OMS-P0A-HOLD]` lens to EMIT. **Part 2 is not flag-gated and has been deployed since 08-05 21:06.** | ⛔ **NO — MIS-PARKED** |
| **A3 forced stand-down** | a deliberate forced stand-down on a marketable exit | ⛔ **NO — MIS-PARKED** |

⛔⭐ **The rollout runbook ALREADY SAYS SO, and was not read that way:**
> *"Turning #646 on does **not** validate P0a. Item 11 and P0a are **separate questions** … that
> still points at the **A3 forced stand-down** as the only reliable route, **and it is untouched by
> this rollout**."*

The document stated the independence; the **board** re-coupled them under the Gate-1 label. Nobody
was misled by evidence — they were misled by a heading.

#### ⛔⭐⭐ AND THE FAILURE FORMS LIVE — IT IS NOT ONLY IN OLD BUNDLES
**Recurred within 24 hours of the rule being written.** A3 was made *"conditional on tonight's
census"*. The census then failed to ship because **GitHub Actions went into a `major_outage`** —
so A3 inherited a blocker with **no relationship to it whatsoever**.

⇒ The mis-parking failure is **not a legacy artefact to be cleaned up once.** It **forms every time
a "conditional on" is added without asking whether the dependency is REAL.**

**Test the dependency's direction and strength before writing it down:**
- the census **could CANCEL** A3 (if `held>0`, A3 may be unnecessary) — one-directional and weak
- the census **cannot INFORM** A3's design: it is a deliberate forced stand-down whose method and
  acceptance are already written

⇒ **A dependency that can only cancel is not a blocker. BOOK IT ANYWAY.** Cancelling a booked item
is cheap; not booking it has already cost a week.

### 📅 A3 — BOOKED, INDEPENDENT OF THE CENSUS
**Friday 2026-08-07, after the 16:00 close. Attended.** Not conditional on #660, not conditional on
Actions recovering, not conditional on the census. If the census lands first and shows `held>0`,
**cancel it** — that is the cheap direction.
⛔ A3 is a **deliberate act we schedule**, never an opportunity we wait for. Treating it as
opportunistic is what kept P0a unvalidated from 07-31.

⇒ **Consequences, both actionable without Gate 1:**
1. **P0a's real blocker is the lens not emitting**, and the *instrument-the-negative* change (log
   when the hold path is evaluated and **declines**) addresses it directly. That is an OMS change
   needing an **after-close attended deploy — not an RTH opportunity.** Available tonight.
2. **A3 is not opportunistic at all.** A forced stand-down is a **deliberate act we schedule**, not
   a condition we wait for. It has been queued as though it were the latter.

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

**Storm rate:** pre **2/11 (18 %)**, post **1/4 (25 %)** as counted above.

### ✅ AAOG RESOLVED BY EVIDENCE 2026-08-06 — the post-period storm comes OUT
The order-history read (§2b) settles the lot question the distribution could not: AAOG's 113 rejects
belong to the **13:28 bracketed lot ⇒ D1**. Its **no-bracket** lot was bought **07:53:12** and sold
**08:14:01**, both `AM` session, both `SINGLE`, **closed eight hours before the storm** — it cannot
have been involved, and it contributes **~1 sell attempt**, not 117.

Restricting to the discriminator's hazard condition (**AM entry + first sell attempted before
09:30**):

| | n | storms |
|---|---|---|
| **pre-P0a** | 3 | **2** — KUST 07-31, AGEN 07-13 |
| **post-P0a** | 3 | **0** — BJDX, GTE, CLRO |

**Post-P0a has ZERO storms, not one.** ⚠️ **Fisher p ≈ 0.20 — NOT significant.** Better than the
0.33 it replaces, and the awkward counter-example was removed **by evidence, not by the per-lot
argument I could not settle** — but it is still not a result.

⛔ **The other 14 positions remain SYMBOL-DAY grained and could carry the same error AAOG did**
(AGEN 07-13 alone had 3 entry lots). Only AAOG has been resolved per-lot. Treat every other row as
provisional at that grain.

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

## 7b. 🔎 BOARD ITEM — SILENT INTENT DROP AT THE INELIGIBLE SHORT-CIRCUIT (**visibility**)

> ⛔⭐ **FILE UNDER VISIBILITY, NOT UNDER SCHWAB REJECTS.** The operator has **parked** the API-open
> reject class and PAVS — Webull covers those names and the fan-out works as intended. **This is a
> different defect that merely surfaced there**, and it is broker-agnostic. Filing it beside the
> parked item would park it too.

**Defect.** An `open` intent for a symbol cached as ineligible is marked rejected and a rejected
event published — but **no `broker_orders` row is written and NOTHING IS LOGGED.**

| | |
|---|---|
| site | `oms/service.py:870-887` (schwab) and **`:893-910` (webull)** — symmetric, **neither logs** |
| what happens | `mark_intent_status(intent,"rejected")` → `_build_rejected_event(reason="schwab_ineligible_cached" \| "webull_ineligible_cached")` → publish → return |
| what does NOT happen | no `broker_orders` row · **no log line** |
| only trace | `trade_intents.status='rejected'` + a published event nobody reads |

**Why it is general, not Schwab's.** The Webull block at `:893` is identical in shape, so the same
blindness will hide **non-Schwab** cases as soon as the fan-out evicts a name. Whatever is parked
about *why* a broker refuses, **the disappearance itself must be visible.**

**Live instance (2026-08-06, PAVS).** Schwab refused PAVS at 08:12:27; at 11:34:38 the bot logged
`[V2-RESTING-PLACE]` and `emitted intent … qty=2`, and **no Schwab order ever existed.** From the
tape that is indistinguishable from an order placed and never filled — and the resting path then
cancelled it at 11:37:02 as `flip_no_fill`, a **misleading** reason for an order that was never on
the book. ⛔ There is **no `ineligible` line anywhere in the v2 log for PAVS.**

### ⛔⭐⭐ THE SHARP EDGE — THE TAPE DOES NOT OMIT THE EVENT, IT ASSERTS SOMETHING FALSE
At 11:37:02 the resting path cancelled PAVS with:
```
[V2-RESTING-CANCEL] PAVS reason=flip_no_fill level=7.0528
```
**`flip_no_fill` describes an order that WAS on the book and did not fill. There was never an
order.**

> **Silence reads as NO information and invites a look. A plausible false reason reads AS
> information and STOPS the investigation.**

⇒ **A wrong reason is worse than a missing one** — a *distinct* failure from the silent-zero family
this note has been cataloguing all week. That one withholds; this one **misdirects**, and it
consumes the attention that would have found the defect.
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]]

### ⛔ CHECKABLE CONSEQUENCE — CONTAMINATED DENOMINATORS, MEASURED
A wrong reason does not stay in the log; it flows into every count keyed on it. **Any measure of
"how often does the resting order fail to fill" includes orders that were never placed.**

| day | v2 open intents | **never reached the broker** | filled / ALL | **filled / PLACED** |
|---|---|---|---|---|
| 08-05 | 200 | **103 (51.5 %)** | 22.5 % | **46.4 %** |
| **08-06** | 126 | **73 (57.9 %)** | 21.4 % | **50.9 %** |
| 08-03 | 165 | 61 (37.0 %) | — | — |

⇒ **Conversion roughly DOUBLES on the honest denominator.**

#### ⛔⭐ UNIT CORRECTION ON MY OWN HEADLINE — the intent count is CHURN-INFLATED
An earlier revision said *"roughly half of every v2 open intent never reaches a broker"*. **True and
misleading.** The intent denominator counts every reprice of the resting ladder, so a handful of
blocked names inflates into a large-looking share:

| day | dropped intents | **distinct symbols** | intents/symbol | symbols |
|---|---|---|---|---|
| 08-03 | 61 | **4** | 15.3 | EZRA, FUSE, HYFM, UPC |
| 08-04 | 3 | **1** | 3.0 | AMIX |
| 08-05 | 103 | **6** | 17.2 | BJDX, GTE, INLF, JLHL, YXT, ZYBT |
| 08-06 | 95 | **7** | 13.6 | AZI, BYAH, CLRO, PAVS, PN, WLDS, WYHG |

⇒ **~15 intents per blocked symbol IS the resting reprice cadence, not 15 missed trades.** The
honest statement is **"6–7 symbols a day are blocked"**, never *"half your trading"*.
⛔ Report distinct symbol-days **alongside** any intent count. Same rule that caught the earlier
symbol-day-vs-lot and population errors, now applied to a headline of mine.
[[feedback_query_unit_must_match_hypothesis_unit]]

⚠️ **The EOD's resting conversion — `9 / 32 live in-window arms = 28.1 %` — is a FLOOR, biased
DOWN.** Its denominator counts arms on names Schwab had already refused, which were structurally
incapable of converting. ⛔ I am **not** restating it as a corrected number: that needs the
arm → intent → order chain, which the per-lot gap makes unreliable. What is established is the
**direction and the magnitude of the bias**, not a replacement figure.
⛔ Anything derived from 28.1 % inherits the same bias.

### ⭐⭐ THE REAL COST IS HALF-SIZE — SIZED, FOR THE OPERATOR'S CALL
On a Schwab-ineligible name the Webull leg fills **qty 1** while the Schwab leg (**qty 2**) is
dropped, so we take **1 share of an intended 3**. Measured across every `live:orb` fill on a blocked
name, 08-03 → 08-05:

| day | fills | names | median % | actual | at full size | forgone |
|---|---|---|---|---|---|---|
| 08-03 | 11 | 4 | +1.31 % | −$0.36 | −$1.09 | −$0.73 |
| 08-04 | 6 | 1 | +1.30 % | +$0.33 | +$0.99 | +$0.66 |
| 08-05 | 28 | 6 | +1.32 % | −$1.38 | −$4.14 | −$2.76 |
| **total** | **45** | | **+1.31 %** | **−$1.41** | **−$4.24** | **−$2.82** |

Full size would have lost **$2.82 more**.

### ⛔⭐⭐ BUT THIS TABLE MEASURES THE **EDGE**, NOT THE **BLOCK** — PARK IT AND STOP PRICING IT
**The sign of the `forgone` column is determined entirely by the edge.** Size is a multiplier: on a
negative edge, taking less of it "saves money" **as arithmetic, not as protection.** Nothing in this
table says anything about whether the block is good or bad.

⇒ **This is a STRATEGY measurement wearing an EXECUTION label**, and strategy is parked.
⛔ **Do not let a strategy result set an execution item's status — in either direction.** An earlier
revision of this section said half-size "is supported by the data" and would "become a cost if
selection improves". **That second clause was the tell**: an answer that flips on a strategy variable
was never an execution answer. Both claims are **withdrawn**.

⛔ **And the shape is not a new result.** Median **+1.31 %** with a negative sum is the **parked
+2 % / −5 % payoff geometry** — many small winners, a few large losers — reappearing, not a finding.
Do not re-derive it as though it were.
[[project_mai_tai_v2_three_exit_rules]] · [[feedback_percentages_not_dollars]]

⇒ **The item is PARKED. It is not to be priced again.** The figures above are retained only so the
next reader can see that the question was asked and why the answer does not belong here.

⚠️ **The VISIBILITY fix below stands entirely on its own** and is the only actionable part of §7b.
It does not depend on this sizing, on the edge, or on which names are blocked — **the next blocked
name might not be one the operator is happy to skip, and nothing would tell him.**

### THE FIX — one line, and one new string
1. **An INFO line at each short-circuit**, naming symbol · account · reason · intent id.
2. ⭐ **Give the never-placed case its OWN reason string** — do not let the resting path stamp
   `flip_no_fill` on an order that never existed. Same one-line change, costs nothing extra, and
   without it the fix leaves the misdirection in place while merely adding a line elsewhere.

⛔ The suppression itself is correct and protective and must NOT change.

⚠️ **Same family as `[OMS-P0A-HOLD]` at zero** (#660): a correct decision taken silently is
indistinguishable from the code never running. [[feedback_a_watch_that_fails_to_a_false_clean]]
⚠️ Also locates at least one instance of **open thread #4** — *"an unnamed suppression stops a
rejected-symbol retry: risk PASSES, no broker order is created, nothing is logged."*

## 8. RELATED
[`v2-eod-oco-jam-design.md`](v2-eod-oco-jam-design.md) *(its 426 → 113; D2 stays there, own timetable)* ·
[`v2-a2-reverse-reject-design.md`](v2-a2-reverse-reject-design.md) *(the other 313)* ·
[`v2-premarket-exit-protection-rollout.md`](v2-premarket-exit-protection-rollout.md) *(Gate 1 — the owed broker read)* ·
[[project_mai_tai_schwab_eh_no_stop_leg]] · [[project_mai_tai_exit_churn_kust]] ·
[[project_mai_tai_v2_close_retry_sawtooth]]

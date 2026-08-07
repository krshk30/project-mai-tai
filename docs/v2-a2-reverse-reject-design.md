# A2 — THE WEBULL REVERSE-REJECT — design note

**Status: DESIGN ONLY. Nothing built. No live change until the operator approves.**
Moved up the queue 2026-08-06: A2 is **bigger and more current than the board showed**, and part of
its volume was being double-counted as the 16:00 jam.

> ⛔⭐ **ACCOUNT VISIBILITY OF EVERY NUMBER IN THIS DOC: `live:orb` (Webull) ONLY.**
> `ORDER_NOT_SUPPORT_REVERSE_OPTION` is Webull's string. A query keyed to it is blind to Schwab by
> construction, and the converse blindness already produced two wrong readings in one session.
> [[feedback_reject_query_states_account_visibility]]

---

## 0. THE THREE LINES TO READ IF YOU READ NOTHING ELSE

1. ⛔ **FASTER RETRYING CANNOT HELP.** AAOG threw **313 attempts in 816 s — one every 2.6 s, every
   one rejected.** The blocker is **broker-side account state**, not price and not our limit. Anyone
   who reads the reject count will reach for retry tuning; it is the one lever that provably does
   nothing here.
2. ⛔ **ERNA 07-15 and AGEN 07-13 ARE THE ACCEPTANCE TEST.** Those two **never filled at all**. A
   design that improves the median and leaves the never-cleared tail intact **is not a fix** — the
   same way turning a false positive into a false negative is not a fix.
3. ⛔ **PART OF A2 IS AN ENTRY DEFECT AND NO EXIT-PATH FIX CAN REACH IT.** See §5.

---

## 1. THE BOOKKEEPING CORRECTION — one item up, one down

The 16:00-jam note records **"426 rejected sells"** on 2026-08-04 and prescribes **D1** (cancel the
expired OCO legs). Split by account *and* reason:

| account | reason | n | window (ET) |
|---|---|---|---|
| `live:schwab_1m_v2` | `This order may result in an oversold/overbought position` | **113** | 16:00:04 → 16:08:03 |
| `live:orb` | `ORDER_NOT_SUPPORT_REVERSE_OPTION … (http 417)` | **313** | 16:01:31 → 16:15:07 |

⇒ **The 313 are A2, already carried on the board at 394/14d.** The jam's 426 double-counts them.
**A2 grows; the jam item shrinks by the same 313.** Not a new item.
⛔ D1 would have prevented **zero** of the 313.

---

## 2. THE SHAPE — and why the jam note generalised from A2's OUTLIER

Time from the entry fill to the first reverse-reject:

| minutes since entry | instances |
|---|---|
| **≤ 5 min** | **13 of 14** — NXTC 0.3 · SLGB 0.4 · AMIX 0.4 · AMIX 0.6 · EHGO 0.6 · CJMB 0.7 · **ZYBT 0.9** · ERNA 1.0 · RUBI 1.6 · YXT 1.8 · FCUV 2.7 · CNET 3.9 · UPC 4.9 |
| **152.7 min** | **1 — AAOG 08-04** |

⭐ **A2 is a fast POST-ENTRY condition; its median instance fires ~1 minute after we bought.**
The jam note's Webull case (AAOG) is **A2's single outlier**, and "the same jam on both brokers" was
built on it.

**The *string* is not the argument** — a reject string is authoritative for what the broker said,
never for why. The **timing distribution** is: Schwab's oversell fired 2.5 h after entry at a
session boundary; Webull's fires inside 5 minutes of the buy, 13 times in 14.

**ERNA 2026-07-15 is in this list at 1.0 min** — the documented Webull fill-settlement-lag /
false-flat naked position (#464 added a 120 s grace). A2 is that shape, unclosed and recurring.

---

## 3. TWO SUB-POPULATIONS — and ⛔ THE DISCRIMINATOR IS STRUCTURALLY UNAVAILABLE

How each blocked position eventually got sold:

| resolved via | n | meaning |
|---|---|---|
| **`oco_exit`** — the broker's own bracket leg filled | **5** | a live bracket owned the exit throughout — **never naked**, the rejects are noise |
| `limit` — our ladder | 9 | our ladder was the only owner |
| **never filled** | **2** | ERNA, AGEN |

⇒ **DANGEROUS population = 11** (the 9 + the 2 that never filled). **HARMLESS = 5.**

### ⛔ THE BLOCKING CONSTRAINT — read before proposing any conditional logic
What separates the halves is **whether a broker OCO leg was live at the moment of the reject.**
**We cannot see that.** OCO children are created BY the broker, atomically with the parent; the OMS
never places them, so **they never land in `broker_orders`** and no DB query can recover them. This
is the same fact that forced `_refresh_native_oco_armed_state` to ask the broker rather than the DB.

⇒ **The bound MUST NOT be conditioned on which half we are in.** Any design that says "if a bracket
is live, do X" is unimplementable with current visibility.

**This note takes the first of the two available routes, explicitly:**

- ✅ **CHOSEN — be safe for both halves, by not needing the distinction** (§6 P3). Gate on the
  **outcome** (is the position still held at the bound?) rather than the **cause** (is a bracket
  live?). The two halves separate themselves: the live-bracket half self-resolves to flat, the
  settlement half does not.
- ❌ **NOT CHOSEN — acquire the visibility first** via a Schwab/Webull order-history read. That is
  the same opportunity window as **Gate 1**, and it remains **owed for D1's premise**. A2 is
  deliberately built so it does **not** wait on it.

---

## 4. THE NAKED WINDOW — measured trigger→fill, ON THE DANGEROUS POPULATION ONLY

### ⛔ POPULATIONS — STATE THEM ONCE, THEN NEVER MIX THEM
Three numbers circulate in this note and they are **not interchangeable**. An earlier revision
labelled the median's denominator as "the dangerous 9" while listing the 2 never-filled inside the
same column — two different sets under one heading.

| set | n | what it is |
|---|---|---|
| **ALL instances** | **16** | every `live:orb` reverse-reject episode |
| ├ resolved via the broker's own `oco_exit` (**harmless**) | 5 | never naked — the bracket exited it |
| └ **DANGEROUS** | **11** | our ladder was the only owner |
| &nbsp;&nbsp;&nbsp;├ with a measurable trigger→fill | **9** | ⭐ **the MEDIAN denominator** |
| &nbsp;&nbsp;&nbsp;└ **never filled at all** | **2** | ERNA 07-15, AGEN 07-13 — **no duration exists**, so they cannot enter a median |

⇒ **MEDIAN is on the 9. THE BOUND IS PRICED ON THE 11** (the 2 never-filled always escalate, at any
bound). ⛔ Neither is "the population"; say which every time.

⭐ **The fix is judged on the dangerous set, not on all 16.** The harmless half was never naked;
including it mixes signal with noise.

| statistic | **dangerous, n=9 filled** | (all 14 filled, superseded) |
|---|---|---|
| **MEDIAN** | **271.0 s (4.5 min)** | 278.5 s |
| max | **1 831 s (30.5 min)** — CNET 07-28 | 40 253 s |
| **never filled (EXCLUDED from both medians)** | **2 — ERNA, AGEN** | same |

Sorted, seconds: `30, 60, 60, 60, 271, 286, 812, 827, 1831`

**Drop-one by name: 165.5 s … 278.5 s.** ⚠️ On this smaller sample the drop-one is **more
sensitive** — four names (AAOG, CNET, VEEE, ZYBT) each pull the median to 165.5 s. State the median
as **~4.5 min with a drop-one floor of ~2.8 min**, never as a point estimate.

⭐ **The 11.2 h outlier (AMIX 07-29) sat in the HARMLESS half** — the re-scope removes it as a side
effect, which is why the dangerous median barely moved while the distribution got much cleaner.

**The gap survives the re-scope:** there is still **no instance between 60 s and 271 s**. Bounds
from ~90 s to ~250 s remain behaviourally identical. (It is no longer the *widest* gap — that is now
827→1831 s — but it is the one a low bound sits in.)

---

## 5. ⭐ PART OF A2 IS AN ENTRY DEFECT — ZYBT, 2026-08-05, 15:47 ET

```
15:43:44  buy  filled  1          <- lot A
15:46:07  sell REJECTED           <- ladder tries to close lot A
15:46:08  sell REJECTED
15:46:15  sell FILLED  oco_exit   <- lot A exits via the BROKER's bracket
15:46:18  buy  filled  1          <- lot B: RE-ENTERED 3 SECONDS AFTER THE EXIT
15:47:14  sell REJECTED           <- 56 s after buying lot B; 40 rejects over 7m22s
15:54:37  last reject
16:00:46  sell FILLED  limit      <- block total 812 s (13.5 min)
```

**We sold lot A and bought lot B three seconds later, then tried to sell lot B 56 seconds after
that. Webull refused because of the churn — not because of anything about lot B.**

⛔ **NO EXIT-PATH FIX CAN ADDRESS THIS POPULATION.** The refusal was *caused by entry cadence*. A
backoff makes the symptom quieter and leaves the cause untouched. **This belongs with the
entry-composition work**, not with the exit ladder, and it must be carried there rather than being
counted as fixed by anything in §6.

⛔ This storm ran **15:47–15:54 ET**. A2 is **not** a 16:00 item and must not inherit the jam's
schedule.

---

## 6. PROPOSAL

### P1 — classify, don't pattern-match a string
Normalise to an internal class `EXIT_REFUSED_POSITION_NOT_SELLABLE`, mapped from **both** brokers'
strings. Every downstream rule keys on the class. ⛔ Matching Webull's literal string is how this
defect stayed invisible in Schwab-keyed queries for 14 days.

### P2 — back off, because retrying cannot work
On the class, switch from the 1–2 s ladder cadence to a bounded backoff. See §0.1 — 313 attempts in
816 s bought nothing and cost a rejected-order record each.
⛔ **Back-off is NOT abandonment.** The position stays managed and stays owned — Ship 2 must keep
seeing an open managed row throughout, or the watch will correctly page.

### P3 — ⭐ gate on the OUTCOME, never on the cause
**Do not attempt to determine whether a bracket is live — §3 says we cannot.** At the bound, ask the
only question the system can actually answer:

> **Is the position still HELD?**

- **Flat** → it resolved. The broker's bracket did the job (or ours did). **No escalation.**
- **Still held** → we have been unable to exit for the whole bound. **Escalate**, whatever the cause.

This is safe for both halves *by construction*: the live-bracket half self-resolves and is never
escalated for the right reason rather than by a guess.

⛔ Use the existing **tri-state** `_broker_symbol_is_flat` (HELD / FLAT / **UNKNOWN**) and
⛔ **do not collapse UNKNOWN into flat** — that collapse is exactly the #608 defect (145 rejected
sells). UNKNOWN must be treated as still-held for escalation purposes.
⚠️ `account_positions` syncs on ~1-minute cadence, so a position that went flat seconds before the
bound can still read HELD. At a 90 s bound that is at most one sync interval of over-escalation —
acceptable, and it must be stated rather than discovered.

### P4 — exhausted-budget behaviour (the question the board demands be stated)

⛔ **The bound is an OPERATOR RISK DECISION, not a statistic.** "How long may we be unable to
execute a hard stop" is not derivable from how long the broker happened to take. What the data can
do is price each choice. **Priced on the dangerous 11** (still blocked at the bound, the 2
never-filled always counted):

| bound | escalates on |
|---|---|
| 60 s | **7 of 11** |
| **90 s** | **7 of 11** |
| 180 s | 7 of 11 |
| 300 s | 5 of 11 |
| 600 s | 5 of 11 |

⭐ **60 / 90 / 180 s are identical** — that is the 60–271 s gap showing through. So the bound belongs
**in the gap, not at a percentile**.

### ✅ SETTLED — **90 SECONDS**, operator decision 2026-08-06
**The bound is 90 s.** It sits inside the bimodal gap where **every choice from ~90 s to ~250 s
behaves identically** on the measured set, so the number is not bought at the cost of anything else:
past the entire fast-settlement mode, catching every real block at the earliest honest moment.

⛔ **This is a RISK CALL, not a derived value.** Do not re-derive it, do not "optimise" it against a
percentile, and do not let a future sample move it without a fresh risk decision — *"how long may we
be unable to execute a hard stop"* is not answerable from how long the broker happened to take.
The data's only job was to show that **nothing is traded away anywhere in 90–250 s**, and it did.

⇒ **A2 IS UNBLOCKED AND BUILDABLE AT 90 s.**

At the bound, with the position still held:

1. **Never silently abandon.** 384 of 394 are CW_HARD_STOP; abandoning is precisely the ERNA outcome.
2. **PAGE.** A stop we cannot execute is an operator decision and is currently invisible: Ship 1
   pages on rejects so it *would* fire, but it cannot say *"and the block is structural — we have
   been trying for ninety seconds."*
3. **Do not widen tolerance or slow the ladder to make the symptom go away** — a threshold band-aid
   on a path where price moves 30–50 % in seconds. [[feedback_root_cause_over_bandaid]]
4. **Open question — do NOT build either without an operator decision:**
   (a) fall back to a different order shape, or
   (b) cancel our own live bracket leg to free the shares, then sell.
   ⛔ (b) removes protection to attempt an exit and can *lengthen* the naked window. It must not be
   built on the assumption it is free — and per §3 we cannot currently even see the leg it would
   cancel.

---

## 7. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence required (observed, not inferred) |
|---|---|---|
| **A1** | trigger→fill median improves **on the dangerous 9** | replay 07-28 (67), 08-04 (313), 08-05 (40) — the three known-bad tapes |
| **A2** | ⭐ **the never-cleared tail is closed** | **ERNA 07-15 and AGEN 07-13 both reach a terminal state — a fill, or a page.** Median improvement without this is NOT a fix |
| **A3** | reject volume falls **without** trigger→fill getting worse | ⛔ reject count alone is not acceptance; a defer that hides rejects and lengthens exposure FAILS |
| **A4** | **a block that RESOLVES inside the bound is never escalated** | outcome-based and testable. ⛔ *Supersedes the earlier "A2-live-bracket is never escalated", which was unachievable — §3 shows we cannot identify that half* |
| **A5** | the managed row stays open throughout a backoff | Ship 2 stays green — no position becomes unowned while we wait |
| **A6** | the classifier catches **both** brokers' strings | Schwab oversell + Webull reverse both map to the class; one fixture per broker |
| **A7** | ⛔ the ZYBT/entry-cadence population is **explicitly carried to the composition work**, not counted as fixed here | §5 |

⭐ **A3 and A5 are the two most likely to be skipped.** A3 because reject count is the easy number
and it is the wrong one; A5 because a backoff that quietly drops the managed row would look like a
success and would create the exact silent-unowned position Ship 2 exists to catch.

---

## 8. ROLLOUT
Attended · flag-gated, default **off** · deploy **after the close** · PR + Validate · explicit GO
before merge+restart · OMS-only (`stop strategy → restart oms → start strategy`) with the
pre/post-restart bar-gap checklist. ⛔ **No v2 restart** (Bug 2: `cw_entries_this_flip` is
unpersisted and re-issues the entry cap on every armed segment).

## 9. RELATED
[`v2-eod-oco-jam-design.md`](v2-eod-oco-jam-design.md) *(its 426 shrinks to 113 — see §1)* ·
[[project_mai_tai_false_flat_naked_position]] (ERNA, #464's 120 s grace) ·
[[project_mai_tai_v2_close_retry_sawtooth]] (#608 — the tri-state collapse P3 must not repeat) ·
[[project_mai_tai_entry_composition_cap]] (where §5 belongs) ·
[[feedback_a_reject_is_our_defect]]

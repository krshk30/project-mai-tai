# A2 — THE WEBULL REVERSE-REJECT — design note

**Status: DESIGN ONLY. Nothing built. No live change until the operator approves.**
Moved up the queue 2026-08-06: A2 is **bigger and more current than the board showed**, and part of
its volume was being double-counted as the 16:00 jam.

> ⛔⭐ **ACCOUNT VISIBILITY OF EVERY NUMBER IN THIS DOC: `live:orb` (Webull) ONLY.**
> `ORDER_NOT_SUPPORT_REVERSE_OPTION` is Webull's string. A query keyed to it is blind to Schwab by
> construction, and the converse blindness already produced two wrong readings in one session.
> [[feedback_reject_query_states_account_visibility]]

---

## 1. THE BOOKKEEPING CORRECTION — one item up, one down

The 16:00-jam note records **"426 rejected sells"** on 2026-08-04 and prescribes **D1** (cancel the
expired OCO legs) as the remedy. Splitting that 426 by account *and* reason:

| account | reason | n | window (ET) |
|---|---|---|---|
| `live:schwab_1m_v2` | `This order may result in an oversold/overbought position` | **113** | 16:00:04 → 16:08:03 |
| `live:orb` | `ORDER_NOT_SUPPORT_REVERSE_OPTION … (http 417)` | **313** | 16:01:31 → 16:15:07 |

⇒ **The 313 are A2, already carried on the board at 394/14d.** The jam's 426 double-counts them.
**A2 grows; the jam item shrinks by the same 313.** Not a new item.
⛔ D1 would have prevented **zero** of the 313.

---

## 2. THE SHAPE — and why the jam note generalised from A2's OUTLIER

Time from the entry fill to the first reverse-reject, every instance:

| minutes since entry | instances |
|---|---|
| **≤ 5 min** | **13 of 14** — NXTC 0.3 · SLGB 0.4 · AMIX 0.4 · AMIX 0.6 · EHGO 0.6 · CJMB 0.7 · **ZYBT 0.9** · ERNA 1.0 · RUBI 1.6 · YXT 1.8 · FCUV 2.7 · CNET 3.9 · UPC 4.9 |
| **152.7 min** | **1 — AAOG 08-04** |

⭐ **A2 is a fast POST-ENTRY condition. Its median instance fires ~1 minute after we bought.**
The jam note's Webull case (AAOG, 152.7 min) is **A2's single outlier**, and the whole "same jam on
both brokers" reading was built on it.

**That is also the honest reason the two are different defects.** The *string* is not the argument —
a reject string is authoritative for what the broker said, never for why. The **timing distribution**
is the argument: Schwab's oversell fired 2.5 h after entry at a session boundary; Webull's fires
inside 5 minutes of the buy, 13 times in 14.

**ERNA 2026-07-15 is in this list at 1.0 min** — the documented Webull fill-settlement-lag /
false-flat naked position (#464 added a 120 s grace). A2 is that shape, unclosed and recurring.

---

## 3. ⭐ TWO SUB-POPULATIONS — and only one is dangerous

How each blocked position eventually got sold:

| resolved via | n | meaning |
|---|---|---|
| **`oco_exit`** — the broker's own bracket leg filled | **5** | a live bracket owned the exit the whole time |
| `limit` — our ladder finally got through | 9 | our ladder was the only owner |

⇒ **A2-live-bracket.** A working OCO leg reserves the shares, so our *additional* sell is refused —
and then the bracket exits the position anyway. The position was **never naked**; the rejects are
noise. (This sub-population *is* mechanically the Schwab reservation story, which is why the two
looked alike.)

⇒ **A2-settlement.** Just-entered, no usable bracket yet, Webull will not accept a sell at all.
**This is where the 384 CW_HARD_STOPs live and this is the dangerous one** — we are trying to stop
out of a position the broker will not let us leave.

⛔ **The current code cannot tell these apart, and neither can the reject string.** Any fix that
treats A2 as one thing will either add latency to the harmless half or abandon the dangerous half.

---

## 4. THE NAKED-WINDOW CONSTRAINT — measured, not assumed

The board's rule is right: **acceptance must measure trigger→fill, never reject count.**
First reverse-reject → the sell that actually filled (`live:orb` only):

| statistic | value |
|---|---|
| **MEDIAN block** | **278.5 s (4.6 min)**, n=14 |
| worst excluding the one outlier | **1 831 s (30.5 min)** — CNET 07-28 |
| outlier | **40 253 s (11.2 h)** — AMIX 07-29, filled 21:11 ET |
| **never filled at all** | **2 — ERNA 07-15, AGEN 07-13** (excluded from the median) |

Sorted, in seconds: `1, 16, 30, 60, 60, 60, 271, 286, 292, 545, 812, 827, 1831, 40253`

**Drop-one by name: 165.5 s … 286 s.** Only **AMIX** moves it materially — to **165.5 s (2.8 min)** —
because AMIX owns both the 11.2 h outlier and a 292 s instance. Every other name leaves it at
271–286 s.

**So the naked window is a median of ~4.6 minutes — and twice it never ended.**
⛔ Those two are the acceptance test. A design that improves the median and leaves the
never-cleared tail intact has not addressed A2.

**Retry burn, for scale:** AAOG threw 313 attempts in 816 s — **one every 2.6 s, every one
rejected.** Faster retrying cannot help: the blocker is broker-side account state, not price and
not our order's price.

---

## 5. THE CASE — ZYBT, 2026-08-05, 15:47 ET (mid-RTH, nowhere near 16:00)

```
15:43:44  buy  filled  1     <- lot A
15:46:07  sell REJECTED      <- ladder tries to close lot A
15:46:08  sell REJECTED
15:46:15  sell FILLED  oco_exit   <- lot A exits via the BROKER's bracket
15:46:18  buy  filled  1     <- lot B: re-entered 3 SECONDS after the exit
15:47:14  sell REJECTED      <- 56 s after buying lot B. 40 rejects follow, over 7m22s
15:54:37  last reject
16:00:46  sell FILLED  limit     <- block total 812 s (13.5 min)
```

⭐ **Read the 3-second re-entry.** We sold lot A and bought lot B three seconds later, then tried to
sell lot B 56 seconds after that. Webull refused **because of the churn**, not because of anything
about lot B. A2's dominant population is partly **self-inflicted by re-entry cadence** — which ties
it to the entry-composition work, not only to the exit ladder.

⛔ **This storm ran 15:47–15:54 ET.** A2 is **not** a 16:00 item and must not inherit the jam's
schedule.

---

## 6. PROPOSAL

### P1 — classify, don't pattern-match a string
Normalise to an internal class `EXIT_REFUSED_POSITION_NOT_SELLABLE`, mapped from **both** brokers'
strings. Every downstream rule keys on the class. ⛔ Matching Webull's literal string is how this
defect stayed invisible in Schwab-keyed queries for 14 days.

### P2 — back off, because retrying cannot work
On the class, switch from the 1–2 s ladder cadence to a bounded backoff. The blocker is broker
account state; 313 attempts in 816 s bought nothing and cost a rejected-order record each.
⛔ **Back-off is NOT abandonment.** The position stays managed and stays owned — Ship 2 must keep
seeing an open managed row throughout, or the watch will correctly page.

### P3 — split the two sub-populations before deciding anything
Ask the broker whether a **live exit leg exists** (the `fetch_armed_native_oco_symbols` walk
already does this):
- **leg live** → A2-live-bracket → the exit is owned; log once, stop hammering, let it work.
- **no leg** → A2-settlement → the position is genuinely unsellable and a hard stop is pending.
  This is the case that gets the escalation below.

### P4 — exhausted-budget behaviour (the question the board demands be stated)

⛔ **The bound is an OPERATOR RISK DECISION, not a statistic.** "How long may we be unable to
execute a hard stop" is not derivable from how long the broker happened to take. What the data can
do is price each choice. Against the 14 measured instances:

| bound | pages on | leaves untouched |
|---|---|---|
| 60 s | **8 of 14** | the six that cleared in ≤60 s |
| 180 s | 8 of 14 | *(no instance lands between 60 s and 271 s — the bound is insensitive here)* |
| **300 s** | **5 of 14** | everything through the 292 s instance |
| 600 s | 4 of 14 | adds YXT 545 s |

⭐ **Note the gap: nothing resolves between 60 s and 271 s.** The distribution is bimodal — fast
settlement (≤60 s) or a real block (≥271 s) — so anything from ~90 s to ~250 s buys identical
behaviour. That gap, not a percentile, is where the bound belongs. **I recommend 90 s**: it is
past the entire fast mode, catches every real block at the earliest honest moment, and no choice
inside the gap trades away anything.

With that bound, for A2-settlement only:

1. **Never silently abandon.** 384 of 394 are CW_HARD_STOP; abandoning is precisely the ERNA
   outcome.
2. **At the bound: PAGE.** A stop we cannot execute is an operator decision, and it is currently
   invisible — Ship 1 pages on rejects, so it *would* fire here, but it cannot say *"and the block
   is structural, we have been trying for three minutes."*
3. **Do not widen tolerance or slow the ladder to make the symptom go away** — threshold band-aid
   on a path where price moves 30–50 % in seconds. [[feedback_root_cause_over_bandaid]]
4. **Open question for the operator — do NOT build either without a decision:**
   (a) fall back to a different order shape (Webull may accept a shape it does not call a reverse), or
   (b) cancel our own live bracket leg to free the shares, then sell.
   ⛔ (b) removes protection to attempt an exit and can *lengthen* the naked window. It must not be
   built on the assumption it is free.

---

## 7. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence required (observed, not inferred) |
|---|---|---|
| **A1** | trigger→fill median improves, measured on `live:orb` | replay 07-28 (67), 08-04 (313), 08-05 (40) — the three known-bad tapes |
| **A2** | **the never-cleared tail is closed** | ERNA 07-15 and AGEN 07-13 both reach a terminal state — a fill, or a page |
| **A3** | reject volume falls **without** trigger→fill getting worse | ⛔ reject count alone is not acceptance; a defer that hides rejects and lengthens exposure FAILS |
| **A4** | A2-live-bracket is never escalated | a bracket-owned block logs once and pages **zero** times |
| **A5** | the managed row stays open throughout a backoff | Ship 2 stays green — no position becomes unowned while we wait |
| **A6** | the classifier catches **both** brokers' strings | Schwab oversell + Webull reverse both map to the class; prove with a fixture per broker |

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
[[project_mai_tai_dual_broker_fanout_build]] · [[feedback_a_reject_is_our_defect]]

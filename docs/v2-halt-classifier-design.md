# LULD HALT CLASSIFIER — design note

**Status: DESIGN ONLY. Nothing built.** Entry-path change ⇒ design-first is mandatory
([`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md)).
Requested by the operator 2026-08-04: *"add the halt classifier to the entry filter."*

> ## ⛔⭐ READ THIS FIRST — the literal request does NOT fix the trade that prompted it
> Every halt-adjacent entry on 2026-08-04 happened **BEFORE** the halt, never during or after:
>
> | entry (ET) | halt began | lead time |
> |---|---|---|
> | AMIX 09:30:21 | 09:31 | **0.6 min** |
> | AMIX 11:06:27 | 11:17 | 10.5 min |
> | AMIX 15:28:07 | 15:28 | already starting |
>
> **Zero of 9 AMIX entries occurred during or after a halt.** A "block entry during/after a halt"
> gate would have blocked **none** of them and would not have prevented the **−5.71%**.
>
> ⇒ **A halt is not predictable from the halt.** It is predictable from the *volatility that trips
> the band* — which is what a **gap cap** catches, not a halt classifier. §5 covers that; it is a
> separate change and it is the one that addresses the observed loss.
>
> The classifier is still worth building — for **three different jobs** (§3). Building it as an
> entry veto and calling the AMIX case closed would be treating a real mechanism as a fix for
> something it cannot reach.

---

## 1. WHAT A HALT LOOKS LIKE IN OUR DATA

AMIX halted **11 times** on 2026-08-04. The signature is unambiguous:

| property | value |
|---|---|
| gap width | **exactly 5.0 or 10.0 minutes** — never 4.7, never 6.3 |
| move across the gap | **+20.7 / −2.3 / +24.2 / +12.1 / +15.4 / +13.7 / +13.4 / +10.2 / +14.2 / +21.5 / −4.3 %** |
| bars either side | heavy volume (200k–780k/min), so it is emphatically **not** a quiet name |
| Schwab REST | returns **0/54** of those minutes — it has no candles because **no trading occurred** |

⭐ **The control that proves it is not a data defect:** on the same REST call, the same day, VGAS
filled **264/297** missing minutes. The fetch path is fine; AMIX's minutes genuinely do not exist.

## 2. ⛔ A BAR GAP IS NOT ALWAYS A DEFECT — classify before repairing

| shape | meaning | action |
|---|---|---|
| **exactly 5/10 min + large move across** | **LULD halt** | nothing to fill; nothing broken |
| 1-bar holes on a thin name | quiet minutes | REST has nothing either |
| long, irregular, on an active name | **real data loss** | backfill repairs it (VGAS) |

⚠️ Both misreadings happened on 2026-08-04: VGAS's real loss was dismissed as "a silent name", and
AMIX's halts were escalated as a suspected REST bug. **The classifier's first value is telling
these apart** — it is as much a data-integrity tool as a trading one.

---

## 3. WHERE THE CLASSIFIER ACTUALLY BELONGS — three jobs, none of them "veto on halt"

### J1 — SUSPEND THE RESTING ENTRY WHILE HALTED *(highest value, and the real exposure)*
A resting buy-stop-limit sits **at the broker**. It cannot fill during a halt — but at the
**resumption auction** it triggers into whatever price the reopen prints. AMIX's resumptions moved
**+20.7%, +24.2%, +21.5%**. A resting order left across a halt is an order to buy the reopen at any
price up to its limit band.

⚠️ *[inferred — did NOT occur on 08-04]* No resting order sat across a halt today, so this is an
unrealised exposure, not an observed loss. It is nonetheless the one place a halt classifier
prevents money loss rather than merely improving data hygiene.

⛔ **Constraint — the #580 orphan.** Suspending must CANCEL and keep the order managed, never
"skip and leave it live" (the liquidity-floor precedent: `_queue_resting_cancel(...)`, and the
`schwab_1m_v2.py:1792` comment *"an order already working must keep being managed even if the tape thins"*).

### J2 — TREAT PRE-HALT TRIGGER LEVELS AS STALE AFTER A RESUMPTION
`cw_trigger` (frozen 2-bar high) and `cw_segment_high` are built from bars **before** the halt. A
resumption 10–24% away makes both meaningless as a break level — the first post-halt quote clears
them trivially. This is the same failure shape as the 09:30–10:00 ORB window, which *paused* setups
and then released them all at one clock edge into stale triggers, buying 19–24% past the signal.

**Proposal:** on detecting a resumption, **disarm the segment** (do not merely pause it) and require
a fresh flip. ⛔ Pausing, not cancelling, is what made the ORB window actively harmful.

### J3 — DO NOT COMPUTE ATR ACROSS A HALT
`href`/`lref` reference `prev.close`, so a single bar carries the whole halt into a 5-period Wilder —
**identical arithmetic to the restart bar-hole that put resting orders ~8% off on 07-30**, but a
completely different cause, and **no restart checklist will ever catch it** because nothing
restarted. Same guard as #620, triggered by halt-shaped gaps as well as outage-shaped ones.

---

## 4. DETECTION — and its one hard limit

**Post-hoc (reliable):** gap of exactly 5 or 10 minutes between consecutive bars ⇒ halt. Cheap,
unambiguous, already computable from `strategy_bar_history`.

**Live (what J1/J2 actually need):** we must know we are halted *while* it is happening, not after
the resumption bar arrives. Candidates, in order of preference:
1. **Schwab quote/stream state** — does LEVELONE expose a halt/security-status field? **Unknown;
   this is the first thing to check and it decides whether J1 is buildable at all.**
2. **Bar starvation + live quotes** — no completed bar for ≥2 min while quotes still arrive. Weak:
   indistinguishable from a thin name, which is exactly the false positive that would suspend
   legitimate resting orders on quiet symbols.
3. ⛔ **Do NOT infer a halt from a bar gap alone at runtime.** By the time the gap is measurable the
   resumption has already printed, which is precisely too late for J1.

⇒ **Open the build with a spike on (1).** If Schwab exposes no live halt state, J1 is not reliably
buildable and the note should be re-scoped to J2/J3 only, which work post-hoc.

---

## 5. WHAT WOULD ACTUALLY HAVE STOPPED THE 08-04 LOSS — the gap cap, not this

AMIX: armed **5.3095**, bought **5.7799** — **+8.9% past the flip** — stopped out in the same
minute at **−5.71%**, and the stock halted the next minute.

The entry was late relative to its own signal. A **gap cap** refuses when `px > trigger × (1+cap)`;
ORB already ships `orb_running_high_gap_cap_pct = 1.5%`. That is a separate change, it is pinned as
the preferred answer in the P1 discussion, and **it — not a halt classifier — addresses this loss.**

⛔ Do not let the halt classifier be recorded as the fix for AMIX. It is not.

---

## 6. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence |
|---|---|---|
| **A1** | The classifier labels 08-04 AMIX's 11 halts as halts, and VGAS's 297-bar hole as **data loss** | run against both known tapes; a classifier that calls VGAS a halt is worse than none |
| **A2** | Zero false positives on thin names | AAOG's 30 one-bar holes must **not** classify as halts |
| **A3** | J1 suspends and **cancels**, never skips | prove by mutation: a skipped-not-cancelled path recreates the #580 orphan |
| **A4** | J2 **disarms**, never pauses | a paused setup released at resumption is the ORB-window harm, reproduced |
| **A5** | J3: ATR across a halt is not computed from `prev.close` spanning the gap | pin the ATR value either side of a known halt |
| **A6** | **No behaviour change** when no halt is present | the entire 08-03 tape must replay byte-identical |

⭐ **A2 and A6 are the ones most likely to be skipped** — a halt classifier that fires on quiet
names would suspend legitimate entries all day on exactly the thin symbols v2 trades.

## 7. ROLLOUT
Flag-gated, default **off** · attended · after the close · PR + Validate · explicit GO.
⛔ Entry-path change ⇒ **operator discussion before any build starts**, per the standing rule on
selection/entry changes.

## 8. OPEN QUESTIONS
1. **Does Schwab expose a live halt/security-status field?** Decides whether J1 exists at all.
2. **Should a halted symbol be evicted from the watchlist**, or held and re-armed after resumption?
3. **A name halting 11× in one session is its own regime** — is that a *selection* signal (avoid),
   or exactly the oscillation the operator wants? These point in opposite directions and the answer
   is not obvious. [[project_mai_tai_selection_spent_move]]

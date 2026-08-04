# THE 16:00 EXIT JAM — design note

**Status: DESIGN ONLY. Nothing built. No live change until the operator approves.**
Raised 2026-08-04 by the operator, who hit it from the other side: a hand-placed TOS sell was
rejected. Queue-jumped ahead of P1 by operator decision — it is repeatable, it costs money, and it
fires on roughly **one session in three**.

---

## 1. THE INCIDENT — 2026-08-04, real money

v2 held **AAOG** through the close (Schwab 2 @ 4.4799, Webull 1 @ 4.4797, both entered 13:28 ET).
Price fell to the −5% hard stop. Between **16:00 and 16:16 ET neither the bot nor the operator
could sell.**

```
16:00:03  [OMS-V2-EOD-OCO-TRANSITION] live:schwab_1m_v2 AAOG -> released the native-OCO
          stand-down; software EH-limit ladder now owns the exit
16:01:56  OPERATOR's manual TOS order REJECTED:
          "This order may result in an oversold/overbought position in your account."
          TA_krshk30gmailcom... SELL -2 AAOG @4.13 LMT EXT
16:03:35..16:15  our ladder fires CW_HARD_STOP every ~1-2s -- every one REJECTED
```

**426 rejected sells in 16 minutes** — `live:orb` **313**, `live:schwab_1m_v2` **113**.
For scale, the incident #608 was written to prevent was **145 in 55 minutes**.

**Cost — the exit was intended at −5%:**

| leg | in | out | result |
|---|---|---|---|
| Schwab (2) | 4.4799 @ 13:28:43 | 4.2297 @ **16:08:05** | **−5.58%** |
| Webull (1) | 4.4797 @ 13:28:46 | 4.2103 @ **16:15:08** | **−6.01%** |

≈ **0.6–1.0 percentage point** of pure slippage per leg, caused by the jam. It cleared only when
the broker legs expired — **nothing we did fixed it.**

---

## 2. THE CHAIN — four links, all required

1. **16:00:03** — `_v2_eod_oco_transition` releases the native-OCO stand-down on a **still-held**
   position and hands the exit to the software EH ladder.
2. ⛔ **But the broker's OCO sell legs still RESERVE the shares.** The transition's own docstring
   reasons: *"a session=NORMAL DAY order cannot fill in EH, so nothing is lost by letting it
   lapse."*
   **A leg that cannot FILL still RESERVES.** So the handoff is to a ladder that is *structurally*
   unable to sell.
3. Price is at the stop, so the ladder fires `CW_HARD_STOP` every 1–2s → **all rejected oversold**,
   on both accounts.
4. ⛔ **#608's bound never engages, because the symbol is GENUINELY HELD** (`oms/service.py:2762`):
   ```python
   if state is _PositionRead.HELD:
       self._v2_exit_close_failures[key] = 0   # we DO hold it -> keep managing, re-count later
   ```
   #608 correctly narrowed the reset from HELD-**or**-UNKNOWN to HELD-only (resetting on both is
   what let NCRA retry 145×). But **HELD is exactly this case**, so the counter resets every pass
   and `_V2_EXIT_ABANDON_AFTER_FAILURES = 8` is unreachable.

⇒ **The state model is missing a third case.** It knows *flat*, *held*, and *unknown*. It has no
notion of **held-but-BLOCKED** — we hold the shares and the sell can never succeed until something
external changes. "Keep managing" is right for a *transient* reject and wrong for a *structural*
one, and the loop cannot tell them apart.

⭐ **The discriminator already exists and is being discarded:** the broker returns the reason
verbatim — *"This order may result in an oversold/overbought position."* The retry loop keys on
**position state** and never inspects the **rejection reason**.
[[feedback_authoritative_for_a_is_not_for_b]]

---

## 3. IT IS NOT NEW, AND IT IS NOT RARE

**Rejected sells in the 16:00–16:30 ET window, per day:**

| day | account | rejected |
|---|---|---|
| 07-22 | schwab | 3 |
| **07-28** | **orb** | **66** |
| 07-29 / 07-30 / 08-03 | orb | 1 / 1 / 2 |
| **08-04** | **orb** | **313** |
| **08-04** | **schwab** | **113** |

`MAI_TAI_OMS_V2_EOD_OCO_TRANSITION_ENABLED=true` has been live since **2026-07-27** (first
transition log line). **07-28 is the same jam at 66 rejects** — it simply was not recognised.
2026-08-04 is the first time the **Schwab** leg jammed too.

**Blast radius — positions held through 16:00 ET, by day:** 07-07, 07-13, 07-14, 07-15, 07-22,
07-28 (1 each) and 08-04 (2). **7 of ~20 sessions ≈ one in three.**

⚠️ *[inferred]* Earlier days pre-date the transition flag, so they had no handoff to jam — but they
did hold through the close, so the exposure is structural, not incidental.

---

## 4. TWO DEFECTS — fix independently

### D1 — the transition lets the legs LAPSE instead of CANCELLING them
The docstring's premise is wrong on one word: *cannot fill* ≠ *does not reserve*.

**Proposal — invert the ordering, matching #647's principle: never hand off until the handoff is
real.**
1. At the transition, ask the broker for the position's live OCO legs (the existing
   `childOrderStrategies` walk in `fetch_armed_native_oco_symbols` already finds them — it returns
   symbols; this needs a variant returning **order ids**: new, small adapter surface).
2. **Cancel them.** The DELETE path exists (`_cancel_order`, `schwab.py:709`); it is currently
   driven from an OMS order row, and these legs have none, so it needs a **cancel-by-broker-id**
   entry point. *(Proven reachable: an ad-hoc qty-safe script hit the same endpoint successfully
   on 08-04.)*
3. **Re-read the broker to confirm zero live sell legs**, then and only then release the
   stand-down.
4. **On failure: do NOT release.** Leave the stand-down in place and log loudly. A position whose
   legs we could not cancel is better left with the broker owning it than handed to a ladder that
   cannot sell.

⛔ No protection is lost by cancelling: the legs cannot fill in EH anyway. The software ladder
becomes the *only* owner — which is already the transition's stated intent.

### D2 — the retry bound cannot see a structural block
**Proposal: make `held-but-blocked` a first-class state.**
- If a close is rejected **and** the reason matches the oversell class, do **not** reset the
  accumulator on a HELD read — accumulate, and stand the retry down at the existing bound.
- Belt-and-braces: bound the HELD retry by **elapsed time** as well as count, so any future
  structural block is capped even if the reason string changes.

⚠️ **Standing down the retry must never be read as "the position is unprotected".** The existing
stand-down comment is explicit — *protection is untouched; only the retry stops* — but in this
incident there **was** no protection (the legs had expired). **D1 is what restores that invariant;
D2 only stops the hammering.** Ship D1 first.

⛔ **Do not "fix" this by widening the reject tolerance or slowing the ladder.** That is a
threshold band-aid on a path where price moves 30–50% in seconds. [[feedback_root_cause_over_bandaid]]

---

## 5. ACCEPTANCE CRITERIA — inverted badge

| # | criterion | evidence required (observed, not inferred) |
|---|---|---|
| **A1** | At the transition, the broker reports **zero live sell legs** before the stand-down is released | one timeline for one held-through-close position: cancel → **broker re-read confirms zero** → release |
| **A2** | A position held through 16:00 **can actually be sold at 16:00:05** | a successful EH exit, or a deliberate qty-1 manual sell that is accepted |
| **A3** | **Zero oversold rejects** in the 16:00–16:30 window on a day with a held position | count directly; 07-28 (66) and 08-04 (426) are the known-bad tapes to replay against |
| **A4** | If cancellation FAILS, the stand-down is **NOT** released | prove by **deliberate mutation** — force the cancel to fail and confirm the bracket keeps ownership |
| **A5** | D2 caps a structural block | inject a persistent oversell reject on a HELD position ⇒ retries stop at the bound instead of resetting |
| **A6** | **No change** to the flat / UNKNOWN paths #608 fixed | #608's own tests stay green; NCRA's 145-retry case must still be bounded |

⭐ **A4 and A6 are the two most likely to be skipped and the two most likely to bite** — A4 because
failure paths are boring, A6 because this edits the exact function #608 hardened.

---

## 6. ROLLOUT
Attended · flag-gated, default **off** · deploy **after the close** · PR + Validate · explicit GO
before merge+restart · OMS-only (`stop strategy → restart oms → start strategy`) with the
pre/post-restart bar-gap checklist. ⛔ **Does not require a v2 restart — do not take one**
(Bug 2: `cw_entries_this_flip` is unpersisted and re-issues the entry cap on every armed segment).

## 7. OPEN QUESTIONS
1. **Cancel at 16:00, or never emit a lapsing bracket at all?** An alternative is to give RTH
   brackets a duration that dies cleanly at the close. Cancelling is the smaller change; the
   duration route removes the class.
2. **Should the Webull leg cancel on the same trigger?** It jammed **313** times today, worse than
   Schwab, and it is the leg already known to be deaf to the flip exit.
3. **D2's reason-matching is broker-specific.** Match a normalised class, not the literal Schwab
   string, or Webull will silently not benefit.

## Related
`v2-premarket-exit-protection-design.md` *(pending in PR #646)* ·
[`premarket-eod-exit-design.md`](premarket-eod-exit-design.md) ·
[`oco-bracket-design.md`](oco-bracket-design.md)

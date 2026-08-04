# v2 PRE-MARKET EXIT PROTECTION — design note

**Status: DESIGN ONLY. Nothing is built. No live change until the operator approves this note.**
Author: agent session 2026-08-04. Closes open thread 11. Doubles as the **P0a validation vehicle**.

> ✅ **Probe P ran 2026-08-04 and SETTLED the gating question — preview-only, nothing placed, zero
> money at risk.** Schwab rejects a STOP leg in the extended-hours session with
> *"This order type is not available for this session."* — single leg, entry bracket, and
> exit-only OCO pair alike, with `session=NORMAL` controls accepting. **The two-part shape below is
> confirmed.** Full matrix in §0.

> **Scope discipline.** This note covers the **pre-market/EH exit protection path only**.
> Fix 1 (entry composition cap / SOBR) and Fix 2 (exit poll from open rows) are **out of scope**
> and are not touched by anything proposed here.

---

## 0. THE QUESTION THAT DECIDES THE SHAPE — answered

**Why is `[V2-OCO-EMIT]` skipped outside RTH: broker restriction, or self-imposed?**

**Answer: it is SELF-IMPOSED IN OUR CODE. We have never once asked Schwab for an
extended-hours bracket.** Two independent pins:

1. **The guard returns before any payload is built.** `oms/service.py:5176`
   — `if not _is_regular_market_session(): ... return`. `_is_regular_market_session()`
   (`oms/service.py:198`) is pure wall-clock: `_extended_hours_session(now) is None`. No broker
   call, no capability probe, no cached rejection. *[pinned]*
2. **The bracket payload hardcodes `session: "NORMAL"` — in three places.** The parent
   (`broker_adapters/schwab.py:1147`) and **both** OCO exit legs (`:1077`, in
   `_bracket_exit_leg`). Contrast the single-leg path at `:1028`, which reads
   `request.metadata.get("session", "NORMAL")` and *does* support `AM`/`PM`. So the bracket
   builder is structurally incapable of expressing an EH session even if the guard were lifted.
   *[pinned]*

**Corroborating: no broker rejection of an EH bracket exists anywhere in our logs** — because no
such order was ever sent. Grep across all retained `oms.log*` returns zero
extended-hours/session rejections from Schwab; the only `extended_hours_trading` hits are Webull
ORB payloads. `[V2-OCO-EMIT] ... SKIPPED` fires often (91× on 07-30, 2× already today), and every
one of those is **our** clock, not Schwab's answer. *[pinned]*

### ⛔ But do NOT read that as "so just emit it pre-market"

The original comment's reasoning is about the **entry** leg — *"a MARKET+STOP OTOCO placed
PRE-market would queue to 09:30 (missing the pre-market entry) or firm-reject."* That reasoning is
**sound for an entry bracket** and is not what this note proposes to change.

The load-bearing unknown is narrower and sits on the **protective leg**:

> `_bracket_exit_leg(..., order_type="STOP")` builds `{"orderType": "STOP", "stopPrice": …}`.
> **Extended-hours sessions are limit-only at every US broker we have touched, including our own
> EH design** (#390 routes EH exits as `EH-LIMIT single-leg, no OCO`; the EH managed exit is
> built as LIMIT + `session=AM|PM`, `oms/service.py:3160-3175`). A `STOP` leg in `session=AM`
> is very likely a firm reject. *[inferred — never measured against Schwab]*

And a limit-only OCO **cannot express a protective stop**: a sell LIMIT below market executes
immediately, it does not wait for adverse movement. So if the STOP leg is refused in AM/PM, then
**native pre-market downside protection is structurally impossible at Schwab**, and the software
ladder must own the pre-market exit. That is the case the two-part fix below assumes.

### ✅ PROBE P — SETTLED BY MEASUREMENT, 2026-08-04 07:19–07:21 ET

**Run against the live account via `POST /previewOrder` — PREVIEW ONLY, nothing was placed.**
No live qty-1 order was needed: `preview_bracket_order` already existed
(`broker_adapters/schwab.py:1183`, the endpoint that broker-validated the OTOCO shape on
2026-07-21), so the question was answerable at **zero money at risk**. Harness:
`scripts/schwab_eh_session_probe.py` — the only endpoints it references are `/previewOrder` and
`/accountNumbers`; there is no POST to `/orders` in the file by construction.

**Every EH case is paired with the identical shape in `session=NORMAL` as a control**, because a
bare reject is not an answer — it could equally mean "not shortable", "bad tick", or "harness
broken".

| # | shape | session | result |
|---|---|---|---|
| C1 | single BUY STOP | NORMAL | ✅ **ACCEPTED** |
| **P1** | single BUY STOP | **AM** | 🔴 **REJECT — "This order type is not available for this session."** |
| C2 | single SELL LIMIT | AM | reject on *position only* (we hold no AAPL) — **no session message** |
| P2 | single SELL STOP | AM | position reject **+ "not available for this session"** |
| C3 | TRIGGER→OCO bracket | NORMAL | ✅ **ACCEPTED** |
| **P3** | TRIGGER→OCO bracket | **AM** | 🔴 **REJECT — "This order type is not available for this session."** |
| C4b | exit-only OCO pair | NORMAL | reject on *position only* — **no session message** |
| **P4b** | exit-only OCO pair | **AM** | position reject **+ "not available for this session"** |

**Verdict — the broker's own words, `originalSeverity: REJECT`:**

> **"This order type is not available for this session."**

1. **A STOP order type is refused in the AM session, in every shape** — single leg (P1, a BUY, so
   no position/shortability confound at all), entry bracket (P3), and exit-only OCO pair (P4b).
   *[pinned — measured]*
2. **LIMIT is accepted in AM.** C2 and C4b reject *only* on the oversold/position check with **no
   session message**; their AM twins add it. The sole difference between C4b and P4b is the
   session field. That is a clean differential. *[pinned — measured]*
3. Both NORMAL controls that could be clean were **ACCEPTED**, so the harness is sound and the
   rejects above are real answers, not artefacts.

⇒ **The `_is_regular_market_session()` skip was self-imposed and never verified — but it is
CORRECT.** Schwab independently refuses the construct. Two separate facts, both now established:
we were guessing, and the guess happened to be right.

⇒ **Native pre-market downside protection is structurally impossible at Schwab.** A limit cannot
express a protective stop (a sell LIMIT below market executes immediately rather than waiting for
adverse movement), and STOP is refused. **The software ladder MUST own the pre-market exit.**

⇒ **The two-part shape below is CONFIRMED, and Part 1's trigger is confirmed too:** an exit-only
OCO is refused in AM (P4b), so the bracket genuinely cannot be armed before the RTH edge. The
design's "emit at the instant RTH opens" is not a convenience — it is the earliest moment the
broker will accept it.

---

## 1. THE PROBLEM BEING CLOSED

A pre-market/EH entry gets **no native OCO** → the **software exit ladder owns the exit** → that
ladder is the 30s reprice-cancel churn path.

**KUST, 2026-07-31, real money.** Pre-market entry, no bracket. A sell LIMIT 1.74 placed 13:26:20
UTC was cancelled and re-placed **nine times in six minutes** against a captured bid tape that was
**≥ the limit at every single tick** (1.76/1.77/1.76/1.75/1.74/1.78…). It ended at the −5% hard
stop: **−5.17% on a signal that was RIGHT**, while the Webull leg — same bid-sourced 1.74, placed
once, never cancelled — filled in **34 ms at +1.76%**. *(`oms/service.py:6209-6217`)*

**NVVE proves necessary ≠ sufficient.** Getting everything under a bracket was the stated intent,
but **11 cancelled sells landed on an OCO-bracketed entry** (NVVE 07-23; KUST 07-22 = 6,
FIEE 07-27 = 6). When a bracket resolves or stands down,
`[OMS-OCO-STAND-DOWN-CLEARED] … OCO gone; ladder deferred` (`oms/service.py:3400`) hands the exit
**back to the bare timer ladder** — KUST's failure mode, now on a bracketed entry.

---

## 2. FINDING THAT CHANGES THE SCOPE — Part 2 is mostly *prove*, not *build*

**P0a's marketable-hold has NO session gating.** `_managed_exit_refresh_exempt`
(`oms/service.py:6201-6247`) requires exactly:

```
flag oms_hold_marketable_managed_exit (default True)
payload.oms_v2_managed_exit == "true"
payload.order_type == "LIMIT"
limit_price > 0 ; bid > 0 ; limit_price <= bid
```

Nothing in that predicate is RTH-only. And the EH exit satisfies every clause **by construction**:

- the managed exit is stamped `oms_v2_managed_exit: "true"` at its single build site
  (`oms/service.py:3158`) — always, RTH or EH;
- in extended hours that same builder switches `order_type` to **LIMIT** with `session=AM|PM`
  priced off the live bid (`:3163-3175`), because a MARKET order cannot fill in EH;
- **P0b** then caps the limit at the bid (`_cap_exit_limit_to_bid`, `:6249`), so `limit <= bid`
  holds *at placement* — which is precisely P0a's engage condition.

⇒ **The pre-market exit should ALREADY inherit the P0a hold today.** It has simply never been
observed doing it. *[pinned to code; UNVERIFIED at runtime — this is the gap the acceptance run
closes]*

⛔ **This is exactly the class of claim that has burned us before** — the vol floor was
"configured" for weeks while guarding dead code, and `positions: []` was read as evidence when it
was a literal in a constructor. **Configured ≠ enforced. The acceptance run must prove the hold
FIRES on the EH path, from the log line, on real tape.** Until then Part 2 is unproven, not done.

**So the genuine build is smaller than it looked:**

| part | status |
|---|---|
| Pre-market exit uses the P0a hold, not the bare timer | **likely already true — must be PROVEN** |
| Bracket emitted at the open for a position still held from a pre-market entry | **genuinely missing — must be BUILT** |
| Stand-down-clear never falls back to the bare ladder | **genuinely missing — must be BUILT** |

---

## 3. THE DESIGN

### Part 1 — BRACKET AT THE OPEN (the real hole)

`_apply_v2_oco_bracket_entry` is wired to the **entry intent** only: it is invoked on a
`side=buy, intent_type=open` event and decorates that order's metadata
(`oms/service.py:5143-5165`). A position entered at 07:30 and still held at 09:30 is therefore
**never bracketed, for its entire life** — there is no code path that revisits it.

**Proposal.** A session-transition sweep, evaluated on the existing OMS periodic loop:

- **Trigger:** first loop iteration where `_is_regular_market_session()` is true and the previous
  iteration's value was false — the RTH edge. No new timer; reuse the loop that already runs.
- **Selection:** every open managed v2 row where the entry filled in the EH session and the
  broker reports **no live OCO pair** (reuse the existing broker-truth walk
  `childOrderStrategies` ≥2 WORKING SELL legs, `broker_adapters/schwab.py:129-185` — the same
  source of truth the stand-down already uses; do **not** invent a second notion of "bracketed").
- **Action:** emit an **exit-only OCO** (target LIMIT + protective STOP) for the held quantity,
  using the same `_cw_target_pct` / `_cw_stop_pct` geometry the ladder would have used, so the
  bracket **relocates** the exit rather than changing it.
- **Cancel-then-arm ordering:** the software exit must be stood down **only after** the broker
  confirms the OCO legs are live — never before. An unprotected gap between cancelling the
  software exit and the broker accepting the bracket is the naked-position shape this whole
  structure exists to eliminate.
- **Failure is safe:** if the OCO is refused, **keep the software exit exactly as-is** and log
  loudly. A failed upgrade must leave the position no worse protected than before.

⚠️ **The exit-only bracket is a different payload from today's entry bracket** — no TRIGGER
parent, just the OCO pair against an existing position. `_build_bracket_payload` builds
TRIGGER→OCO and cannot express this. This is new adapter surface and needs its own STEP-1
qty-1 proof before it goes anywhere near the managed path.

### Part 2 — PRE-MARKET EXIT INHERITS THE P0a HOLD

Per §2 this is believed already true. **Deliverable is proof, plus whatever the proof exposes.**

- Confirm on live EH tape that a marketable EH exit is **held**, not cancel/replaced.
- Confirm the hold **releases** correctly when the bid falls below the limit (the exemption is
  *not* "never reprice" — a stale exit that never adjusts is the same bug facing the other way).
- If the proof shows the hold does **not** engage in EH, the cause is one of the predicate clauses
  above and the fix is targeted at that clause — not a new mechanism.

### Part 3 (FIRST-CLASS REQUIREMENT, NOT A FOOTNOTE) — THE STAND-DOWN-CLEAR RULE

> **Whenever a bracket resolves or stands down, the exit MUST either re-arm a bracket or inherit
> the P0a marketable-hold. It must NEVER fall back to the bare timer ladder.**

This is the load-bearing constraint. NVVE (11 cancelled sells on a bracketed entry) is the
evidence the path is real, not theoretical. **Any design that does not state which of the two it
does on stand-down-clear is incomplete and must be rejected at review.**

⚠️ **P0a alone does not satisfy this.** The hold engages only while `limit <= bid`. A bracket that
stands down while the exit is **not** marketable still lands on the plain ladder. So the rule needs
an explicit decision at `[OMS-OCO-STAND-DOWN-CLEARED]` (`oms/service.py:3400`):

| condition at stand-down-clear | required behaviour |
|---|---|
| position still held, RTH, no live OCO | **re-arm a bracket** (Part 1's exit-only path) |
| position still held, EH (no bracket possible) | **P0a-held software exit**, explicitly |
| exit marketable | P0a hold — already covered |
| exit NOT marketable | **must still not churn** — this is the uncovered case and needs a stated rule |

---

## 4. P0a VALIDATION, BUILT IN

P0a has been deployed-not-validated since 07-31 because the condition never occurred organically:
FCUV exited through a native bracket (not the software path), and 08-03's one EH exit filled in
**41 ms** — correctly scored `fastfill_inconclusive`, neither pass nor fail.

**A position whose bracket stands down while its exit is marketable runs straight through the P0a
hold.** That is the test we could not trigger. The acceptance run below is designed to produce
exactly that condition, so **P0a closes as a side effect of validating Part 3** — the two are not
separate workstreams.

---

## 5. ACCEPTANCE CRITERIA — inverted-badge discipline

⛔ **Every criterion is stated so that a broken implementation FAILS it.** A criterion that a
no-op would also satisfy is not a criterion. Green means the listed evidence was *observed*, not
that nothing was observed.

| # | criterion | evidence required (observed, not inferred) |
|---|---|---|
| **A1** | A pre-market entry is protected **at all times** — software P0a-hold before the open, native OCO from the open onward | one continuous timeline for one position: EH exit present → RTH edge → OCO legs confirmed live at the broker → software exit stood down **after** that confirmation. **Zero unprotected gap.** |
| **A2** | **No bare-timer fallback at any stand-down** | across the run, zero cancel/replace of a *marketable* managed exit. The KUST signature (repeated cancels while bid ≥ limit) must be **absent from the tape**, verified against the captured bid series, not from the absence of an alert |
| **A3** | **Stand-down-clear is PROVEN, not assumed** — force a bracket stand-down on a marketable exit | `[OMS-OCO-STAND-DOWN-CLEARED]` followed by the P0a hold engaging (or a re-armed bracket), and the position exiting cleanly. **This is the P0a validation.** |
| **A4** | The P0a hold **releases** when the exit stops being marketable | one observation of bid falling below limit → refresh resumes → exit repriced. Without this, A2 could be satisfied by an exit that never adjusts |
| **A5** | **Byte-identical on the RTH path** | the existing in-hours OCO flow is unperturbed: characterize before, re-prove identical after. A pre-market fix must not touch the path that already works |
| **A6** | The failure path is safe | with the bracket emit forced to fail, the position keeps its software exit and the failure is logged. Prove by **deliberate mutation**, not by absence of failure |

⭐ **A5 + A6 are the two most likely to be skipped and the two most likely to bite.** Per the
standing rule: a green suite is not evidence until a deliberate break turns it red.

---

## 6. ROLLOUT

- **Attended.** Operator present. No unattended deploy of this path.
- **Kill-switched.** Every new behaviour behind its own flag, default **off**, with the kill
  command written down *before* the deploy — not derived afterwards.
  ⚠️ Note the P0a precedent: `MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT` runs on the **code default
  `True`** and is **absent from the env file**, so its kill is an **APPEND**, not a flip. Do not
  repeat that — write the flag into the env file explicitly at deploy time.
- **Deploy after the close.** Never mid-session on the live entry/exit path.
- **PR + Validate mandatory**, no direct push. Explicit operator GO before the merge+restart.
- **Restart choreography** (OMS is touched): account-flat pre-flight →
  `stop strategy → restart oms → start strategy`. Pre/post-restart **bar-gap checklist applies** —
  a restart leaves a bar hole and ATR spans it.

---

## 7. OPEN QUESTIONS FOR THE OPERATOR

1. ~~Probe P first?~~ ✅ **DONE 2026-08-04** — preview-only, no order placed, answer in §0.
   The two-part shape is confirmed; no re-scope needed.
2. **A3 needs a bracket stand-down forced on a marketable exit.** Cleanest trigger is a qty-1
   attended position where the bracket is cancelled by hand while the exit is marketable.
   ⛔ Reminder: a hand-cancel at the broker does **not** stop the Webull fan-out leg — it needs
   `global_manual_stop_symbols` as well.
3. **Sequencing vs #366.** Handoff has this as *P0a validated → (#366 if the quiet window is only
   deploy-sized) → this build*. This note lets P0a validation ride along with Part 3 rather than
   blocking on it — confirm that is the intended order.
4. **EH target leg — now a live question, because LIMIT *is* accepted in AM (C2/C4b).** A native
   EH *target* is placeable; a native EH *stop* is not. Do we want one? It would put the upside at
   the broker while the downside stays on software — **splitting the exit across two owners**,
   which is arguably worse than uniform software ownership and adds a second stand-down surface.
   **Recommendation: no.** Keep the pre-market exit wholly on the P0a-held ladder until 09:30.

---

## 8. WHAT THIS NOTE DELIBERATELY DOES NOT DO

- Does not touch **Fix 1** (entry composition cap / SOBR) or **Fix 2** (exit poll from open rows).
- Does not change **entry** behaviour in any session.
- Does not change the RTH OCO flow (A5 forbids it).
- Does not propose a threshold or tolerance band-aid anywhere.

## Related

[`session-handoff.md`](session-handoff.md) open thread 11 + THE STAND-DOWN-CLEAR CONSTRAINT ·
[`oco-bracket-design.md`](oco-bracket-design.md) ·
[`v2-eh-exit-routing-fix-design.md`](v2-eh-exit-routing-fix-design.md) (#390, the EH-LIMIT exit) ·
[`premarket-eod-exit-design.md`](premarket-eod-exit-design.md)

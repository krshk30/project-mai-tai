# #646 PRE-MARKET EXIT PROTECTION — ROLLOUT RUNBOOK

Companion to [`v2-premarket-exit-protection-design.md`](v2-premarket-exit-protection-design.md)
(the design) and PR #647 (the build). **Attended only. Deploy after the close. Nothing here is a
standing authorisation — each gate needs the operator present.**

> **Status at time of writing (2026-08-04 09:47 ET):** Gate 0 ready (CI green). Gate 0.5 **NOT
> satisfied** — nothing from #647 is deployed. Gate 1 **blocked on opportunity**, not on work.

---

## WHY THE ORDER IS FORCED (not a preference)

1. **Part 3 reuses Part 1's `_emit_v2_rth_edge_bracket` wholesale.** The re-arm trigger sits on top
   of the same emit, so Part 1's emit must be proven live before Part 3 goes on.
2. **The sweep only ever PLACES; it never stands the software exit down.**
   `_refresh_native_oco_armed_state` activates the stand-down only once the BROKER confirms both
   legs working. So enabling the sweep is **additive protection, not a handoff**: the P0a-held
   software exit keeps the position until the bracket is provably live, and a failed placement
   leaves it untouched.

⇒ **The risk being gated is NOT "unprotected gap" — it is OVERSELL / DOUBLE-BRACKET**, and that is
a thing you can watch for directly in the tape.

---

## GATE 0 — merge #647 dark

✅ **CI green** (`validate` SUCCESS, mergeable CLEAN, 1823 tests, ruff clean).

Merging is **inert in production**: every new *behaviour* is behind a flag defaulting off.

- [ ] `gh pr merge 647 --squash`

⛔ **One thing in #647 is NOT flag-gated: Part 2's logging.** It is log-only and unconditional, so
it takes effect the moment the code is deployed. That is deliberate — see Gate 0.5.

---

## GATE 0.5 — DEPLOY, so Part 2 is actually live

⛔⭐ **THIS IS A DEPLOY, NOT A CONFIRMATION.** Ground-truthed 2026-08-04 09:47 ET: the box is on
`71c6c2c` (#645) and `grep -c "OMS-P0A-HOLD"` on the deployed source returns **0**. Nothing from
#647 is on the box.

**Why it must come before every gate below.** Part 2 is the LENS. Without
`[OMS-P0A-HOLD]` / `[OMS-P0A-HOLD-RELEASED]` in prod, Part 1's deferral line — *"a software exit is
working and has the shares reserved"* — cannot be cross-read against the P0a hold that CAUSED the
reservation. You would be inferring health from an absence again, which is the exact false-clean
failure this build exists to end.

- [ ] VPS `git pull --ff-only` to the merge commit
- [ ] **PRE-RESTART bar-gap checklist** (mandatory — a restart leaves a bar hole and ATR spans it,
      which put resting orders ~8% off on 07-30)
- [ ] Account-flat pre-flight
- [ ] Choreography (OMS is touched): **`stop strategy → restart oms → start strategy`**
- [ ] **POST-RESTART bar-gap checklist**
- [ ] Confirm the lens is live: `grep -c "OMS-P0A-HOLD" src/…/oms/service.py` on the box > 0

**Expected after this gate, with zero flags flipped:** no behaviour change at all, plus
`[OMS-P0A-HOLD]` lines appearing whenever a marketable managed exit is held.

---

## GATE 1 — STEP-1 shape proof (preview only, NO flag)

**The proof Probe P could not give.** With the account flat, Probe P's `session=NORMAL` control
rejected on the oversold/position check — that settled the SESSION and left the SHAPE unproven.

```python
# adapter method shipped for exactly this
await adapter.preview_exit_only_oco(request)
```

**GO signal:** `previewOrder` returns accepted with **no REJECT severity** for the exact exit-only
OCO shape.

⛔ **TWO ways to get another non-result — both must be avoided:**
1. **Reserved shares.** If a working software exit already reserves the position, the broker
   rejects on oversell and you have proven nothing. Preview against **free** shares, or shrink qty
   to the unreserved count.
2. **⭐ Shares that are not OURS.** Ground-truthed 2026-08-04 09:47 ET: the *only* Schwab position
   is **CYN 5000 — the operator's MANUAL holding, in `PROTECTED_SYMBOLS`**. Previewing a sell
   against it breaks the OMS scoping invariant (the OMS acts only on positions it placed) even
   though preview is read-only. **Do not use CYN.**

⇒ **Gate 1 is OPPORTUNISTIC.** It needs v2 to be holding a Schwab long **during RTH** with
unreserved shares. v2 was flat on Schwab from 08:14 (AAOG exited). Wait for a qualifying position;
do not manufacture one.

- [ ] v2 holds a Schwab long, RTH, shares unreserved
- [ ] `preview_exit_only_oco` returns accepted, zero REJECT severity
- [ ] Raw response body captured into this file / #646

⛔ **Until Gate 1 is green, no placement flag goes on.**

---

## GATE 2 — `oms_v2_rth_edge_bracket_enabled=true` (Part 1)

**This is where open thread 11 closes live.**

**Watch the tape for:**

| line | meaning |
|---|---|
| `[OMS-V2-RTH-EDGE-BRACKET] … ARMED` on a genuinely pre-market-held position at 09:30 | ✅ the intended fire — item 11's exact condition (AAOG 2026-08-04) finally getting broker-side protection |
| `… deferred — a software exit is working` | ✅ the oversell guard doing its job; **expected and good** whenever P0a is holding |
| `… GAVE UP after N attempts` | ⚠️ position stays on the software ladder for the session. Not naked — but no broker-side protection |

🔴 **FAIL LINE — pull the flag immediately:** a second set of sells against reserved shares, any
oversold rejection, any E5 shape.

🔴 **A5 CHECK:** **no `ARMED` line for an RTH-entered symbol.** The sweep is scoped to
`entry_time < edge` and must not touch the entry-path flow that already works.

**Rollback:** flag off. Because the sweep only ever places, this returns to software-only
behaviour for anything NEW.

⚠️ **NOT "zero cleanup" — be precise.** A bracket already armed **stays live at the broker** after
the flag goes off, and the stand-down keeps the software ladder deferred while it remains armed.
That is protective, not dangerous, and `session=NORMAL` + `duration=DAY` means it expires at the
16:00 close, which `_v2_eod_oco_transition` already handles. But do not expect the flag flip to
retract a live bracket.

---

## GATE 3 — `oms_v2_stand_down_clear_rearm_enabled=true` (Part 3)

**Only after Gate 2 is proven.** Independent kill switch, so Part 1 stays up if Part 3 is pulled.

**New risk: the resolving-fill oversell window.** The load-bearing guard is
**90s resolution grace + still in `_managed_v2_symbols`**.

**Watch for:**
- `[OMS-V2-STAND-DOWN-REARM] … ARMED` **only** on the NVVE shape (stood down while still held)
- 🔴 **never** inside the 90s grace
- 🔴 **never** against a position going flat

---

## ⛔ WHAT THIS ROLLOUT DELIBERATELY DOES **NOT** CLOSE

Stated explicitly so neither is silently assumed covered.

1. **The not-marketable-at-stand-down case.** Even with Parts 1 and 3 fully on, a stand-down while
   the exit is NOT marketable still lands on the plain refresh ladder and can churn. P0a's hold
   engages only while `limit <= bid`. Closing it needs the owed **reprice-to-bid** pricing
   decision. **This deploy does not touch it.**

2. **P0a validation.** Turning #646 on does **not** validate P0a. Item 11 (does protection exist
   at all) and P0a (does the exit avoid churn when it does hold) are **separate questions**.
   Evidence so far — two consecutive fastfills, **41 ms** (08-03 FCUV-class) and **25 ms**
   (08-04 AAOG) — suggests the hold may never organically engage, because EH exits are marketable
   limits priced off the bid and fill before a refresh tick can threaten them. That still points
   at the **A3 forced stand-down on a marketable exit** as the only reliable route, and it is
   untouched by this rollout.

---

## KILL SWITCHES — write them down BEFORE the deploy

| flag | default | kill |
|---|---|---|
| `MAI_TAI_OMS_V2_RTH_EDGE_BRACKET_ENABLED` | off | set `false` (or drop) + restart |
| `MAI_TAI_OMS_V2_STAND_DOWN_CLEAR_REARM_ENABLED` | off | set `false` (or drop) + restart |
| `MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT` (P0a) | **code default `true`** | ⚠️ **an APPEND, not a flip** — the flag is ABSENT from the env file, so killing it means adding `=false` |

⛔ Write the two new flags into the env file **explicitly at deploy time**, even when enabling
them, so their kill is a flip and never an append. That is the P0a lesson.

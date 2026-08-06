# P0a — INSTRUMENT THE NEGATIVE — design + build note

**Log-only. No behaviour change. No flag.** Deploy attended, after the close, OMS-only.

> ⛔⭐ **THIS IS NOT BEHIND GATE 1.** P0a's validation was parked behind the #647 rollout for over a
> week against a runbook that **already stated their independence** — *"Turning #646 on does not
> validate P0a. Item 11 and P0a are separate questions."* The board re-coupled them under a heading.
> **Nobody was misled by evidence; they were misled by a label.**
> [[project_mai_tai_pr659_is_source_of_truth]]

---

## 1. THE PROBLEM — zero is ambiguous, and it has been zero since deploy

`[OMS-P0A-HOLD]` has emitted **0 lines** since #647 landed on the box (08-05 21:06 ET). Part 2 made
the hold observable; it did **not** make its *absence* readable. Zero is consistent with at least
five different worlds:

| world | is P0a healthy? |
|---|---|
| no managed exit has existed at all | ✅ yes — nothing to hold |
| managed exits existed but none were LIMIT | ✅ yes |
| they were LIMIT but there was no usable bid | ⚠️ fail-open, by design |
| they were marketable but the flag is off | 🔴 **no — misconfigured** |
| the branch never runs | 🔴 **no — broken** |

⭐ **We cannot currently distinguish "nothing qualified" from "it does not fire", and that is most of
why P0a is still deployed-not-validated since 07-31.** It is also, precisely, a watch that fails to
a false clean. [[feedback_a_watch_that_fails_to_a_false_clean]]

---

## 2. THE CHANGE — three pieces, all diagnostic

### P1 · `_p0a_decline_reason(order, bid) -> str | None`
Returns **why** `_managed_exit_refresh_exempt` said no; `None` when it said yes. Reason codes:
`flag_off` · `not_managed_exit` · `not_limit` · `no_limit_price` · `no_bid` · `not_marketable`.

⛔ **DIAGNOSTIC ONLY — it must never gate.** `_managed_exit_refresh_exempt` stays the single
authority for the hold decision.

### P2 · the census counter
Every evaluation increments one bucket — `held`, or the decline reason.

### P3 · `[OMS-P0A-CENSUS]` rollup, every 300 s
```
[OMS-P0A-CENSUS] window=300s evaluated=847 held=0 declined: no_bid=12 not_marketable=835
```

⭐⭐ **It emits even when `evaluated=0`.** That is the whole point: a census that only speaks when it
has something to say **rebuilds the silence it exists to cure**. `evaluated=0` is a RESULT and must
reach the tape. The call site sits in `sync_broker_state`, deliberately **outside** any
"did we evaluate anything" condition and outside the per-account loop.

⭐ **Why a rollup, not a line per decline:** this is on the ~15 s order sync, so per-evaluation
logging would emit per working order per tick — the trade-coach retry-storm shape (45 % CPU while
nominally disabled). The edge-triggered `HOLD` / `HOLD-RELEASED` lines are unchanged; the census
carries the volume.

---

## 3. ⛔ THE RISK THIS CHANGE ITSELF CREATES, AND HOW IT IS PINNED
`_p0a_decline_reason` **duplicates the predicate's structure**, so the two can drift — two sources of
truth for one question, which is the bug class that has cost the most this week.
[[feedback_authoritative_for_a_is_not_for_b]]

**Pinned by `test_p0a_decline_reason_matches_predicate`:**
```python
assert (svc._p0a_decline_reason(order, bid=bid) is None) is svc._managed_exit_refresh_exempt(order, bid=bid)
```
across a 9-case matrix. **Edit either function without the other and it goes red.** ⛔ Do not delete
that test; it is the only thing making the duplication acceptable.

---

## 4. VALIDATION — mutation-proved, not merely green

| test | mutation applied | result |
|---|---|---|
| pairing pin | `not_marketable` → `None` (diagnostic drifts from predicate) | 🔴 **2 failed** |
| `evaluated=0` emits | early-`return` when the window is empty | 🔴 **2 failed** |
| restored | — | ✅ **24 passed** |

`ruff` clean. **Green was not accepted as evidence until a deliberate break turned it red.**
[[feedback_mutate_the_code_pin_the_threshold]]

---

## 5. ACCEPTANCE AFTER DEPLOY — what to read, and what each reading MEANS

| reading | verdict |
|---|---|
| `evaluated=0` on every window, all session | the ladder is not evaluating working managed exits at all → **investigate the caller, not P0a** |
| `evaluated>0`, `held=0`, `not_marketable` dominant | ✅ P0a is fine and **the hold genuinely never engages organically** — confirms the fastfill theory (41 ms / 25 ms) and promotes the **A3 forced stand-down** as the only route |
| `evaluated>0`, `held>0` | ⭐ **P0a engages** — pair with `[OMS-P0A-HOLD]` edges and measure hold durations |
| `flag_off>0` | 🔴 misconfiguration — `MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT` is absent from the env file, so its kill is an **append**, not a flip |
| no census line at all | 🔴 **the change did not deploy** — this is the acceptance test, and it is the one that is easy to skip |

⛔ **A census line every 5 minutes is the proof of life. Its absence is a deploy failure, never a
quiet market.**

---

## 6. WHAT THIS DOES NOT DO
1. **It does not validate P0a.** It makes P0a *validatable*. If the census shows `held=0` all
   session, P0a still needs the **A3 forced stand-down** — which is a **deliberate act to schedule**,
   not an opportunity to wait for. Treating A3 as opportunistic is what has kept P0a unvalidated
   since 07-31.
2. **It does not touch slice C.** The census may inform it — a `no_bid` or `not_marketable` census
   during a pre-market hold is worth cross-reading against the storms — but slice C's surviving
   candidate needs the **account/position** broker read, which is a different instrument.
3. **No behaviour change whatsoever.** Ship 2 must stay green throughout.

---

## 7. ROLLOUT
⛔ **TWO DEPLOYS TONIGHT, NOT ONE — DO NOT MERGE THE CHOREOGRAPHY.**
CELZ and this change share the same restart sequence, and bundling them would recreate this week's
own lesson inside the deploy: **if anything is wrong afterwards you will not know which change owns
it.** Each has a specific, losable acceptance signal — CELZ's is *"CELZ appears in `watchlist
updated`"*; this one's is *"a census line appears"*.

```
1.  CELZ   -> pre-flight -> env edit -> choreography -> VERIFY (CELZ in watchlist updated)
2.  P0a    -> pre-flight -> deploy   -> choreography -> VERIFY (census line within 5 min)
```
Pre-flight before **each** restart, per the standing rule: broker-truth flat · working orders 0 ·
⭐ **armed-segment check that ASSERTS on its output** · outage < 2 min or it is a bar hole.
OMS-only choreography (`stop strategy → restart oms → start strategy`). ⛔ **No v2 restart for this
change** — it touches `oms/service.py` only.

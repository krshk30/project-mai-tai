# #388-for-v2 — cap the CW-v2 segment on FILL, not EMIT (DESIGN-FIRST, NOT BUILT)

**Status:** REVIEW-READY. No code. Live real-money entry path (v2/Schwab).
**Date:** 2026-07-17 · **Rollout:** attended, fleet-flat, flag-gated. Not today.
**Sibling:** this is ORB's #388 (fixed 2026-06-30) applied to v2. [[feedback_has_the_other_bot_solved_this]]

---

## 0. The blocking question, answered first (it changes the shape)

**"Does v2 consume the order-events stream?" → NO.** `schwab_1m_v2_bot.py` reads `strategy-state`,
`strategy-state-isolated`, `heartbeats` — never `order-events`. (ORB's #388 does not use the redis
stream either; it DB-reconciles `broker_order_events` — `orb_app.py:159`.)

**But this is NOT a new-consumer problem.** v2 already has a fill-feedback channel:
`_position_poll_loop` (5s) → `_fetch_open_positions` → `strategy.update_position(symbol, qty)`
(`schwab_1m_v2.py:494`), which sets `state.position_qty`.

**⚠️ The catch that decides the fix — the channel is DELIBERATELY OPTIMISTIC.** `_fetch_open_positions`
(`bot.py:761`) returns **`virtual_positions(qty>0) ∪ in-flight trade_intents(open)`**, `max()` across
sources — *"In-flight intents … also count as 'in position' … preventing duplicate opens."* So
`position_qty` goes 0→N on the in-flight **intent (emit)**, not on the fill. The fill-only signal —
`virtual_positions(qty>0)` alone — is computed inside `_fetch_open_positions` but merged away.
⇒ **The fill channel exists; it just isn't separable at the strategy today.** That is the whole fix.

## 1. The bug (verified, millisecond-pinned)

`cw_entries_this_flip` is the per-BUY-flip entry cap (`_cw_v2_max_entries_per_flip`: 1 reclaim-off / 2
reclaim-on). It is **incremented on EMIT** at `schwab_1m_v2.py:1289`, inside `_cw_v2_quote`, and reset
**only** at the 04:00 session anchor (824) or a fresh BUY flip (1198). **Nothing decrements it on a
reject.** With `max=1`, one emit permanently disables the segment.

**A rejected emit consumes the cap forever:**
- **07-16 09:07 TGHL, 10:01 ATPC** — both in-window, both Schwab open-block rejected, both caps burned.
  The confirmed-microcap universe is ~100% Schwab-open-rejected (07-09 4/4, 07-10 3/3, 07-16 2/2), so
  on a reject day v2 takes zero trades AND burns every segment it touches.
- **After-hours window-block** (24 on 07-16): `[V2-CW] … ENTER n=1` at `.513` →
  `[V2-ENTRY-WINDOW-BLOCK] dropped` at `.514`. The cap incremented (1289) before the window gate
  (`bot.py:1496`) dropped the draft. (Benign — the 04:00 anchor clears it before the window reopens —
  but it is the same increment-on-emit root.)

**⭐ THE DISEASE: intent recorded as OUTCOME.** `[V2-CW] ENTER` says ENTER, means EMIT;
`cw_entries_this_flip` counts emits and caps on them. Fourth site of this family (with
`[OMS-V2-MANAGED-EXIT]` stamping the round-trip and ORB's fixed `traded`-on-emit). **A marker is not a
fill.** [[feedback_has_the_other_bot_solved_this]]

## 2. What is ALREADY correct (do not rebuild it)

The in-flight double-emit guard already exists and already self-heals on a reject:
- `cw_v2_emit_claimed` set on emit (1287), checked at 1250 — blocks a SECOND emit while one is in flight.
- **Released on a reject via timeout** (1178–1181): on a new bar, if the claim is set and `position_qty
  == 0` and the emit is older than `_atr_rearm_timeout_secs`, the claim clears.
- Released on a close for reclaim (514).

⇒ **The claim is NOT the bug.** After a reject the claim ages out and re-opens emits — but the cap check
at 1249 (`cw_entries_this_flip >= 1`) immediately re-blocks them. **The cap is the sole permanent burn.**

## 3. The fix

**Move the cap increment from EMIT to FILL, keyed on the NON-optimistic signal.**

1. **`_fetch_open_positions`** — return the fill-only quantity alongside the optimistic union (it already
   computes both components; today it `max()`-merges them). E.g. return `(union_qty, filled_qty)` where
   `filled_qty` is `virtual_positions(qty>0)` ONLY.
2. **`update_position(symbol, qty, filled_qty)`** — keep the existing optimistic `qty` for the
   close-detection / cooldown logic (unchanged: it must stay optimistic to prevent duplicate opens).
   Add: on a `filled_qty` **0→N transition**, `state.cw_entries_this_flip += 1`.
3. **`_cw_v2_quote:1289`** — **remove** the emit-time increment. Emit sets only `cw_v2_emit_claimed`
   (1287, unchanged) — the in-flight guard that already prevents the double-emit the cap was doing
   double-duty for.

**Net behavior:**
| event | `cw_v2_emit_claimed` | `cw_entries_this_flip` | segment retryable? |
|---|---|---|---|
| emit | True (1287) | unchanged | no (claim blocks) |
| **reject** | ages out (1178) | **unchanged** | **YES — the fix** |
| fill (filled 0→N) | held until close | **+1** (new) | no (cap) |
| close | released (514) | held | reclaim if < max |
| next BUY flip | — | reset 0 (1198) | fresh segment |

**Bonus — the after-hours window-block ghost also resolves:** a window-blocked draft never fills, so the
cap never increments. Two bugs, one fix.

## 4. #388's TRAP is the load-bearing constraint

ORB's #388 lesson: **the release must be fill-gated, or you reintroduce double-entry.** Here the two
concerns are already separated and MUST stay so:
- `cw_v2_emit_claimed` = the in-flight guard (emit → fill/timeout window). It is what prevents a second
  emit before the first fills. **Do NOT weaken it.** Without it, moving the cap to fill would let N emits
  fire into the 5s poll gap before any fill registers ⇒ the exact double-entry #388 warns of.
- `cw_entries_this_flip` = the per-segment cap, now fill-counted.

**The subtlety vs ORB:** ORB's #388 could fill-gate inline (single process). v2's emit (strategy
process) and fill (OMS process) are **decoupled by a 5s poll**, so the in-flight guard MUST be a
timeout/poll, not a `finally`. That is why `cw_v2_emit_claimed` uses the 1178 timeout — correct by
necessity, not laziness.

## 5. ⚠️ Interaction with #486 (per-submit claim) — state it, don't discover it

Both designs touch claim/release semantics on v2's entry-and-exit paths. **They do NOT share state and
do NOT collide, but the reviewer must see why:**

| | #486 per-submit claim | this (#388-for-v2) |
|---|---|---|
| process | OMS | strategy (schwab_1m_v2 bot) |
| path | EXIT (managed-exit / flatten submit) | ENTRY (emit → fill) |
| state | `_submit_in_flight` (OMS) | `cw_v2_emit_claimed` + `cw_entries_this_flip` (strategy) |
| release | `finally` (single process, single await) | timeout/poll (cross-process, 5s) |

⭐ **The distinction to record: `finally` is available to #486 because emit-and-outcome are one await in
one process; it is NOT available here because emit-and-fill are two processes.** Conflating them at
implementation would be a bug — do not "unify" the two claims. Same PATTERN (claim-before-async-action,
release-on-outcome), two different mechanisms, correctly.

## 6. Open review points (flagged, not resolved)

- **`cw_entry_n` metadata semantics change** (`schwab_1m_v2.py:1308`). Today it is set from
  `cw_entries_this_flip` AFTER the emit-increment, so the first entry logs `cw_entry_n=1`. With the fix,
  at emit the cap is still 0 ⇒ it would log `0`. **No downstream consumer** (grep of `src/` is empty —
  log/forensics only), so it is safe to change — but for log continuity, emit could log
  `cw_entries_this_flip + 1` explicitly. Reviewer's call.
- **Reclaim accounting** (reclaim-on, `max=2`): 1st fill caps 0→1, close releases the claim, reclaim
  emit, 2nd fill caps 1→2, cap blocks a 3rd. Preserved — but verify the reclaim-gap check at 1274
  (`cw_entries_this_flip >= 1`) still reads correctly when the cap is fill-timed (it should: by the time
  a reclaim can fire, the first entry has filled AND closed, so cap=1).
- **Partial fills:** `filled_qty` 0→N where N < ordered qty. Define the transition on `>0`, not on
  `== ordered` — a partial is still an entry that consumed the segment. Confirm `virtual_positions`
  reflects partials.
- **The 5s poll latency** means the cap increments up to ~5s after the fill. During that window
  `cw_v2_emit_claimed` (still set, not yet released) holds the line. Verify the claim cannot age out
  (1178 timeout) BEFORE the fill poll lands — i.e. `_atr_rearm_timeout_secs` must exceed the poll
  interval by a safe margin. **If the timeout < poll interval, a fast reject-then-refill race could
  double-enter.** This is the one genuinely new race the fix introduces; size both constants before build.

## 7. Test plan (per [[feedback_mutate_the_code_pin_the_threshold]])

- **Reject does not burn the segment** (the anchor): emit → reject (no `virtual_positions` row) → assert
  `cw_entries_this_flip == 0` and a later qualifying quote CAN emit again. **Assert it FAILS with the
  fix reverted** (increment back at 1289) — that binds the test to the bug.
- **Fill consumes exactly one** (`filled_qty` 0→N increments once, not per poll while held).
- **Double-emit still blocked in the emit→fill window** (the #388 trap): two qualifying quotes before a
  fill ⇒ one emit. Mutation: delete the `cw_v2_emit_claimed` check ⇒ this test must go red.
- **Timeout vs poll ordering** (§6 last bullet): claim timeout > poll interval, asserted as a config
  guard (a threshold that pins the VALUE, not a fixture derived from it).
- **Window-blocked emit does not burn the cap.**
- **Flag off ⇒ byte-identical**, asserted.

## 8. Provenance

Found by asking [[feedback_has_the_other_bot_solved_this]] on the 07-16 cap-burn observation
(IQST/DXST/ASTN showed `capped:True` with zero intents today). ORB solved this class on 06-30 (#388);
v2 never inherited it. **The blocking question ("does v2 consume order-events?") was answered FIRST
because its answer determined whether this was a routing change or a new subsystem — it is the former,
because the position-poll channel already exists; it was just optimistic.**

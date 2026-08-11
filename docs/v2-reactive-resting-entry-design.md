# v2 reactive entry — REST at the known level instead of chasing the touch

**BUILD-READY. 2026-08-10. Build tomorrow, deploy tomorrow night.**

## 1. The case — execution only

The reactive path **chases a price it already knew**. `cw_segment_high` is computed at every bar
close. The bot then waits for a quote to print above it and sends a **MARKET** order *after* the
print. It pays the spread and the drift, on every entry.

Measured, same broker, same universe, same 21-day window:

| Schwab entry path | order type | n | SD | worst adverse |
|---|---|---|---|---|
| **resting** | broker STOP_LIMIT | 67 | **25.6 bps** | **60.2 bps** |
| **reactive** | MARKET | 71 | **57.0 bps** | **351.7 bps** |

**Goal: both Schwab entry slots rest at a known price. Neither chases.**

⛔⭐ **SCOPE: EXECUTION ONLY. NO OUTCOME ANALYSIS.** Whether the trades this changes would have won
or lost is STRATEGY and is parked. **Acceptance is a price comparison and nothing else** (§8).

### 1a. ⛔ #674's price ceiling is a STOPGAP on this path, not the fix
`#674` makes the RTH reactive entry a band-capped marketable LIMIT. **It caps the damage; it does
not stop the chasing.** The trigger and timing are unchanged — the bot still waits for the print and
buys after it. #674 removes the unbounded tail (the ≥200 bps events); **this** change removes the
drift.

⇒ **#674 is SUPERSEDED on this path once this lands.** ⛔ Do not leave it in place by default: when
this deploys, **decide deliberately** whether the cap stays as belt-and-braces over a resting fill or
is removed. Recorded as an explicit decision, not an omission.

## 2. The one behavioural difference — rule 7, ≤1.3%

Rule 7 requires the **whole forming bar** to have stayed above the flip level:
`if fl <= 0.0 or px <= fl or state.cw_bar_low_so_far <= fl: return None`.
`cw_bar_low_so_far` is the running min quote price of the **currently forming bar**. A broker stop
triggers on price alone and **cannot carry intrabar state**.

**Frequency: 65 of 5,129 rule-6-passing bar-evaluations = 1.3%, UPPER BOUND** (the reconstruction
walks every print; v2 evaluates on 5-second polls, so it over-rejects by construction). A rested
reactive order would fill on ~1.3% of cases the current rule declines. **That is the entire fidelity
difference.** Stated as a number, not a verdict. `[V2-CW-RULE7-BLOCK]` (#674) makes it measured from
tomorrow, so the number can be re-derived from the tape before this is judged.

## 3. ⭐⭐ THE BLOCKER, AND WHY IT DISSOLVES

`_resting_entry_already_open` refuses a second OPEN order tagged `resting_entry` per (account,
symbol) — *"so the v2 resting flip-entry can never place a second live buy order."*

**The first design assumed this meant building a second, concurrent resting slot.** It does not:

> ⭐ **The strategy ALREADY makes the two entry types mutually exclusive** —
> `schwab_1m_v2.py:1574`: `if not self._reactive_entry_enabled or state.resting_active: return None`.
> The reactive path stands down whenever a first-entry resting order is live, and has since it
> shipped.

⇒ **There is never a moment when both want a resting order.** So this change needs **ONE resting
order per symbol — the existing one — with its LEVEL sourced from whichever entry type armed it.**

**No second slot. No second latch. No second broker order. The OMS guard is never reached, and its
protection is retained** (both entry types keep the `resting_entry` tag, so a restart-dedup still
catches a duplicate).

## 4. ⛔⭐⭐ THE ORPHAN SURFACE — THE ACCEPTANCE CRITERION, NOT A CAVEAT

`_resting_entry_already_open` exists because of **#580 / EGG-POLA**: a live buy order at the broker
that nothing repriced and nothing cancelled — **twice**, both requiring hand-cancels. The mechanism
is precise and worth stating exactly:

> **The strategy's in-memory latch is the only thing that knows the order exists. Clear the latch
> while the broker order is live, and NO path can reprice or cancel it again.**
> EGG/POLA: `position_qty` counted in-flight open intents, a resting buy-stop's intent stays
> `submitted` for its entire life, so the gate latched qty=2 for an UNFILLED order, cleared
> `resting_active` **without cancelling**, and blocked every future reprice.

### The three sites that write `resting_active = False`
| # | site | cancels first? | why it is safe |
|---|---|---|---|
| 1 | `_queue_resting_cancel` (:1834) | **YES** — queues a broker cancel when `resting_is_broker_order` | the only intended path |
| 2 | `_apply_session_anchor_reset` (:1011) | **no** | relies on the 16:00 window-close cancel having already fired — **an assumption, not an invariant** |
| 3 | the `position_qty_held` gate (:1913) | **no** | **the EGG/POLA site itself**; now gated on broker-confirmed HELD (the #580 fix) so the OTOCO owns the position |

### ⇒ How this change cannot reopen that surface
1. **It adds ZERO new clear-without-cancel sites.** Sites 2 and 3 are the entire orphan surface and
   this change does not touch them. **This is the acceptance criterion: a diff that adds a fourth
   site fails review regardless of its tests.**
2. **It adds no new latch.** The reactive path writes the *existing* `resting_active` /
   `resting_level` / `resting_is_broker_order` fields via the *existing*
   `_queue_resting_place` / `_queue_resting_cancel`. Every existing reprice and cancel path keeps
   managing the order unchanged — there is no state only the new code knows about.
3. **The only new field is inert.** `resting_slot: str = "first" | "reclaim"` selects which LEVEL to
   reprice against. It **never gates a cancel**. If it were corrupted the worst outcome is repricing
   to the wrong level — the order stays managed, never orphaned.
4. **Mandatory test:** for each of the three sites, assert that after it runs either
   `resting_is_broker_order` is False **or** a cancel draft was queued. Pinned for BOTH slot values,
   so a `reclaim`-slot order can never take a clear-without-cancel path a `first`-slot order would
   not.

## 5. The change

`resting_slot` is set at placement and read only when choosing the reprice level.

- **Arm (reclaim):** at bar close, when the reactive path is armed and eligible (all existing gates
  unchanged) **and `resting_active` is False**, `_queue_resting_place(state, cw_segment_high)` with
  `resting_slot="reclaim"`.
- **Reprice:** STABLE-REST cadence, unchanged. The level source is `cw_segment_high` for `reclaim`
  and the ATR trail for `first`. Following a moving level by re-placing is exactly what the
  first-entry path already does.
- **Cancel:** `_queue_resting_cancel`, unchanged, all reasons.
- **On a new cross:** cancel the outstanding reactive resting order **before** arming the new one
  (`reason=new_segment`). **Never carry an order across segments** — a level computed for a dead
  segment is the live-money version of a dangling ARM.
- **Slot claim on FILL, not placement** — a resting order that never fills has cost nothing and must
  forfeit nothing. Claiming at placement would let a place-then-cancel spend the reclaim slot on a
  trade that never existed: strictly worse than today, which is the one thing a change like this
  must not introduce.
- **Rule 7 is not evaluated** for a rested reactive entry. §2.

⛔ **The reactive MARKET path is not deleted.** It remains the fallback whenever a resting order
cannot be placed — including the stop-above-ask case below. Removing it would convert a pricing
change into a missed-entry change.

## 6. ⛔ The reject this must not inherit

A buy stop must sit **above** the ask; placing at an already-crossed level firm-rejects
`The stop price must be above the current ask…`. **Dated:** 49 on 2026-07-23 (pre-guard — `#527`
landed the same day), 4 on 2026-07-30, **zero in the 11 days since**. A working guard, not a missing
one.

**Reuse the `#527` guard verbatim** (`if ask > 0.0 and trail <= ask: return`). If the level is
already crossed at placement, resting there is invalid by construction — do not place.

⛔⭐ **The residual risk is HIGHER on this path than where it was measured.** `cw_segment_high` sits
**at the recent high by definition**; the first-entry trail sits *below* the market by construction.
So the reactive level is likelier to be at or through the ask at placement, and likelier to meet the
guard's **fail-open** case (absent/stale quote) exactly when price is moving fast. **4 leaks in 11
days on the LESS exposed path is the BASELINE, not the expectation.** Watch this reject class after
enabling; a rise is expected in direction, unknown in size.

⛔ **Fail-open stays unchanged.** Making it fail-closed is a separate change with its own evidence —
a rider on an entry-path change is how an unrelated behaviour ships unnoticed.

### 6a. METHOD NOTE — what the recency rule bought
The first draft treated "39 stop-above-ask rejects" as a live defect to engineer around.
Date-ranging it first showed a working guard with a residual of 4, then zero. That turned a second
guard into a reuse. **Never size a defect without its date range.**

## 7. Reused, not rebuilt
| need | mechanism | status |
|---|---|---|
| slot separation | `cw_reclaim_taken` / `cw_resting_taken`; composition cap (#644) | reuse |
| cancel on flip-down | `_queue_resting_cancel` + `resting_is_broker_order` at placement (#666) | reuse |
| stop-above-ask | `#527` | reuse (§6) |
| reprice on a moving level | STABLE-REST | reuse |
| liquidity floor at arm | `_liquidity_floor_ok` — arm only, never a reprice/cancel | reuse |

⚠️ The floor's re-check pulls resting orders off the book ~70×/day. **Measured 2026-08-10: only 4 of
269 off-book windows had price cross a level the segment still wanted (~0.6/day).** Real, small, and
**not** to be changed in the same deploy as this — one change per path at a time.

## 8. Acceptance — a price comparison, nothing else
**Reactive entries show the RESTING path's dispersion, not the reactive path's.** Target SD in the
~25 bps region, no entry beyond ~60 bps. ⛔ No outcome measures.

**Plus the two structural gates:**
1. **Zero new clear-without-cancel sites** (§4). A diff adding one fails review regardless of tests.
2. **`[V2-RESTING-PLACE]` carries the slot**, so `first` and `reclaim` are separable on the tape from
   the first cross — acceptance is a quoted line, not a statistical inference hours later.

**Standards:** design-first (this doc), mutation-proved both directions, fixture matched to
production config, full suite with the baseline quoted.
⛔ **If it is not cleanly proven by the window, it does not go — said at the time, not after.**

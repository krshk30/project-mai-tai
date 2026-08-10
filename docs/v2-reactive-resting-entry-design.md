# v2 reactive entry — REST at the known level instead of chasing the touch

**Design-first. Flag-gated, default OFF. 2026-08-10.**

## 1. The case — execution only

The reactive path **chases a price it already knew**. `cw_segment_high` is computed at every bar
close. The bot then waits for a quote to print above it and sends a **MARKET** order *after* the
print. It pays the spread and the move, on every entry.

Measured, same broker, same universe, same 21-day window:

| Schwab entry path | order type | n | SD | worst adverse |
|---|---|---|---|---|
| **resting** | broker STOP_LIMIT | 67 | **25.6 bps** | **60.2 bps** |
| **reactive** | MARKET | 71 | **57.0 bps** | **351.7 bps** |

The resting path **never exceeded 60 bps**. The reactive path owns Schwab's only **≥200 bps**
entries. Same signal universe, same window — **the variable is the order type.**

**Goal: both Schwab entry slots rest at a known price. Neither chases.**

⛔⭐ **SCOPE: EXECUTION ONLY. NO OUTCOME ANALYSIS.** Whether the trades this changes would have won
or lost is STRATEGY and is parked. **Acceptance is a price comparison and nothing else** (§7).

## 2. The one behavioural difference — rule 7, ≤1.3%

Rule 7 (`schwab_1m_v2.py`, the intrabar entry) requires **the whole forming bar** to have stayed
above the flip level:

```python
if fl <= 0.0 or px <= fl or state.cw_bar_low_so_far <= fl:
    return None   # rule 7
```

`cw_bar_low_so_far` is the running min quote price of the **currently forming bar**. A broker stop
triggers on price alone and **cannot carry intrabar state**, so a resting order cannot express it.

**Frequency: 65 of 5,129 rule-6-passing bar-evaluations = 1.3%, UPPER BOUND.** Reconstructed from 7
days of `[V2-CW-STATE-PROBE]` against the Schwab trade tape. The reconstruction uses **every print**
while v2 evaluates on **5-second quote polls**, so its running-min dips below anything v2 saw and it
**over-rejects by construction** ⇒ the true rate is strictly lower.

⇒ **A resting reactive order would fill on ~1.3% of cases the current rule declines. That is the
entire fidelity difference.** Stated as a number, not a verdict.

⛔ **The 1.3% is a RECONSTRUCTION, not a measurement** — a rule-7 rejection is a bare `return None`
and logs nothing. **Instrumenting it ships FIRST** (§6), so the number becomes measured within days.

## 3. Already built — reused, not rebuilt

| need | existing mechanism | status |
|---|---|---|
| slot separation | `cw_reclaim_taken` / `cw_resting_taken`, distinct per-cross latches; composition cap already separates resting from reclaim (#644) | reuse |
| cancel on flip-down | `_queue_resting_cancel(state, reason=...)`, with `resting_is_broker_order` recorded at placement | reuse (#666 pattern) |
| **stop-above-ask guard** | `#527`, `schwab_1m_v2.py` — `if ask > 0 and trail <= ask: return` | **reuse — see §4** |
| reprice on a moving level | STABLE-REST: re-place only on a meaningful move | reuse |
| liquidity floor at arm | `_liquidity_floor_ok` gates the ARM only, never a reprice/cancel | reuse |

## 4. ⛔ THE REJECT THE REACTIVE PATH MUST NOT INHERIT

A buy stop must sit **above** the ask. Placing at an already-crossed level firm-rejects:
`The stop price must be above the current ask for buy stop orders…`

**Dated (per the recency rule):** 49 on 2026-07-23 (**pre-guard** — `#527` landed the same day),
**4 on 2026-07-30**, and **ZERO in the 11 days since**. So the guard works and is **not airtight**.

**Why the 4 leaked:** the guard is **fail-open** — `if ask > 0.0 and trail <= ask` reads
`state.last_quote`, so an absent or stale quote skips the check and lets a crossed level through
("let the broker be the backstop").

⇒ **The reactive resting order reuses this guard verbatim.** **If the level is already crossed at
placement time, resting there is invalid by construction — do not place.**

⛔⭐ **BUT SAY PLAINLY: THE RESIDUAL RISK IS HIGHER ON THIS PATH THAN WHERE IT WAS MEASURED.**
`cw_segment_high` sits **at the recent high by definition**; the resting path's trail sits *below*
the market by construction. So the reactive level is far likelier to be at or through the ask at
placement, and it is likelier to meet the fail-open case (stale/absent quote) at exactly the moment
price is moving fast. **4 leaks in 11 days on the LESS exposed path is the BASELINE, not the
expectation.** Watch this reject class after enabling; a rise is expected in direction, and its size
is unknown.

⛔ **Fail-open stays unchanged.** Making it fail-closed is a separate change with its own evidence —
a rider on a flag-off entry-path change is exactly how an unrelated behaviour ships unnoticed.

### 4a. ⭐ METHOD NOTE — this section is what the recency rule bought
The first draft of this design treated "39 stop-above-ask rejects" as a live defect to engineer
around. **Date-ranging it first** showed 49 pre-guard, 4 residual, **zero in 11 days** — a working
guard, not a missing one. That turned a second guard into a reuse. **Never size a defect without its
date range** ([[feedback_an_absence_is_evidence_only_against_a_known_denominator]]) — applied the
same afternoon it was written, and it changed the build.

## 5. The change

At each bar close, when the reactive path is armed and eligible (all existing gates unchanged):
- **place** a buy `STOP_LIMIT` at `cw_segment_high`, limit = level × (1 + band), instead of waiting
  for the intrabar touch and sending a MARKET;
- **re-place** when `cw_segment_high` advances (STABLE-REST cadence — the level moves, and following
  a moving level by re-placing is exactly what `rth_resting` already does);
- **cancel** on flip-down / window close via `_queue_resting_cancel`;
### 5a. ⭐ SLOT CLAIM — on **FILL**, not on placement (a behaviour decision, not an implementation detail)

Today reactive sets `cw_reclaim_taken = True` at the emit, because for a MARKET order **emit IS the
fill**. Resting separates them, so the claim instant becomes a real choice.

**Decision: claim on FILL.** A resting order that never fills has cost nothing, so it must forfeit
nothing. Claiming at placement would let a cross that places-then-cancels **spend its reclaim slot on
a trade that never existed** — strictly worse than today's behaviour, and introducing a
strictly-worse path is the one thing a flag-off change must not do.

⛔ **THE CONSEQUENCE, NAMED — it is not free.** Claim-on-fill means **an unfilled reactive order can
still be working when the next cross arrives.** That is part of THIS design, not a follow-up:

> **On a new cross, CANCEL the outstanding reactive resting order before arming the new one**
> (`_queue_resting_cancel`, `reason=new_segment`). **Never carry an order across segments.**

A level computed for a dead segment is precisely the stale-ARM class
([[project_mai_tai_v2_silent_disarm_and_log_rotation]]) — an order resting at a previous cross's
level is a live-money version of the dangling ARM. The existing session/flip reset already clears
`resting_active`; this makes the broker-side cancel explicit rather than implied, and reuses the
`resting_is_broker_order` record-at-placement discipline (#666) to decide whether a broker cancel is
actually owed.

⛔ Rule 7 is **not** evaluated for a rested reactive entry — it cannot be. That is the §2 difference,
and it is the only one.

## 6. Build order

1. **Instrument the rule-7 rejection.** One log line on today's bare `return None`. Turns 1.3% from
   reconstruction into measurement. **Fourth missing-negative found this week** — same shape as
   P0a's bare `pass`.
2. **This change**, flag-gated default OFF.

## 7. Acceptance — a price comparison, nothing else

After enabling: **reactive entries show the RESTING path's dispersion, not the reactive path's.**
Target: SD in the ~25 bps region, no entry beyond ~60 bps. ⛔ **No outcome measures.**

**Deploy discipline:** ships **flag-OFF and verified inert** in the deploy window; **enabled the
following morning, attended**. Deploy and behaviour change are separate events, so a clean deploy
with wrong behaviour tells you which one moved — `#662`'s pattern.

⛔ **If it is not cleanly proven by the window, it does not go.** Say so at the time rather than
compressing the testing to make it.

# schwab_1m_v2 — consume order-terminal events (fill AND reject), not position-poll inference (TICKET — follow-up)

**Owner:** open · **Priority:** medium · **Filed:** 2026-07-09 · **Widened:** 2026-07-09 · **Depends on:**
the re-arm fix (PR #404) deployed first.

## The one missing capability (two symptoms)

The strategy is **poll-only on positions** (`update_position`, 5s) and does **not** consume order-terminal
events. It **infers** order outcomes from position state. That single blind spot causes **both** open
gaps in the re-arm fix:

1. **The 27 blind-timeout rejects.** 27 of 29 no-fills were terminal **rejects** — knowable at the broker
   the instant they happen, and **the OMS already receives them.** Instead the re-arm waits ~12 blind
   seconds for information already in hand; a real flip in that window is blocked.
2. **The fast-scratch blind spot (measured RARE, 2/26).** A fill that **opens and fully closes within one
   5s poll interval** is invisible to the poll (`position_qty` reads 0→0), so the guard re-arms as if
   never filled — violating the one-entry-per-segment invariant on that rare path. Data: of 26 live ATR
   fills (06-24..07-08), **2** had a < 5s lifetime (KIDZ 2s, LHAI 4s); the other 24 lived ≥5s and are
   always poll-caught. Phase-dependent → ~0.8 expected actual misses across the sample. (The backtest
   fills immediately, so **D3/D5 is not confounded** — this is a live-only divergence.)

**Both are the same missing capability: the strategy can't see order outcomes, so it guesses from
position state.** One change fixes both.

## The proposal — consume order-terminal events

Give the strategy an explicit, scoped signal for **its own** order lifecycle:
- **reject / cancel** → release the PROVISIONAL claim **now** (don't wait the 12s timeout) → symptom 1.
- **fill** (even of an already-closed position) → mark **CLAIMED** regardless of what the position poll
  later sees → symptom 2 (a transient open+close no longer looks like "never filled").
- Keep the **12s timeout as the backstop** for the genuinely-unknown / lost-signal case.

## Why it's a SEPARATE ticket

- **Adds coupling.** Today the strategy is deliberately decoupled from order state. Wiring an
  order-terminal signal in (OMS → strategy) is a new dependency with its own failure modes (missed
  signal, ordering vs the poll, provenance scoping) — designed on its own, not bolted onto the bugfix.
- The blind timeout is **correct and sufficient** for correctness (it bounds the leak); the fast-scratch
  residual is **rare and accepted** for now (documented in the LIVE impl plan). This ticket is the
  **latency + completeness** upgrade, not a correctness gate for the re-arm ship.

## Design questions to answer first

1. **Transport.** How does the OMS surface a terminal fill/reject the strategy can consume — a Redis key
   per (symbol, intent), an event stream, a `broker_order_events` / `trade_intents` terminal-status the
   position poll already reads? Cheapest may be extending the existing poll to also read intent
   terminal-status (fill/reject/cancel), avoiding a new stream. NB `broker_order_events`
   (event_type ∈ accepted/filled/partially_filled/rejected/cancelled, linked by
   `client_order_id` = `schwab_1m_v2-{symbol}-{open|close}-{hash}`) already records every outcome.
2. **Scoping.** Signal scoped to THIS strategy's emit (provenance / virtual_position ledger) — respect
   the OMS scoping invariant; never react to another actor's outcome.
3. **Ordering / races.** reject-then-poll vs poll-then-reject; can't release before the emit is recorded;
   can't double-release; a fill signal must win over a stale flat poll.
4. **Keep the backstop.** The 12s timeout stays as the floor for lost signals.
5. **Backtest faithfulness.** `v2_sim` already fills immediately (has neither gap). Extend its
   `reject_bar_idxs` hook to model reject-signal release timing if we want the backtest to mirror the new
   live latency.

## When to pick up

After PR #404 is deployed and D3/D5 re-run. Correctness (blind timeout) first; this closes the latency
gap (rejects) and the rare fast-scratch gap (fills) together.

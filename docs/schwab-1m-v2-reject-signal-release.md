# schwab_1m_v2 re-arm — release on the OMS reject signal, not the blind timeout (TICKET — follow-up, NOT this PR)

**Owner:** open · **Priority:** medium · **Filed:** 2026-07-09 · **Depends on:** the re-arm fix (PR #404)
being deployed first.

## The gap

The re-arm fix releases a PROVISIONAL (emit-sent) claim on a **blind wall-clock timeout** (~12s, §4 of
`schwab-1m-v2-atr-flip-rearm-fix-design.md`). That exists because the strategy is **poll-only on
positions** (`update_position`, 5s) and does **not** consume order-terminal (reject/cancel) events — so
it cannot see *why* a position hasn't appeared, only that it hasn't.

But look at the miss data: **27 of the 29 no-fills were terminal REJECTS** (DB 06-24..07-08). A reject is
**terminal and knowable at the broker the instant it happens**, and **the OMS already receives it.** So
for the *dominant* miss class we currently wait ~12 blind seconds for information the OMS holds
immediately. During those 12s the segment stays PROVISIONAL and a real flip in that window is blocked.

## The proposal

Release the PROVISIONAL claim on an **explicit reject signal** from the OMS (event/flag the strategy can
read), and keep the **12s timeout as the backstop** for the genuinely-unknown case (the 2 cancels, drops,
or any signal that never arrives). Two-tier: *reject signal → release now; else timeout → release at 12s.*

Effect: the restricted-name reject class re-arms in ~1 poll instead of ~2–3, so a real flip landing
within the current 12s window is no longer blocked. This tightens exactly the class that dominates the
live misses (§2.0 of the design doc).

## Why it's a SEPARATE ticket

- **Adds coupling.** Today the strategy is deliberately decoupled from order state (poll-only). Wiring a
  reject signal in (OMS → strategy, via Redis/event/DB flag) is a new dependency with its own failure
  modes (missed signal, ordering vs the poll, provenance scoping) — it must be designed on its own, not
  bolted onto the bugfix. One change at a time.
- The blind timeout is **correct and sufficient** on its own (it bounds the leak); this is an
  **optimization** of latency for the dominant class, not a correctness fix.

## Design questions to answer first

1. **Signal transport.** How does the OMS surface a terminal reject the strategy can consume — a Redis
   key per (symbol, intent), an event stream, a `trade_intents` status the position poll already reads?
   Cheapest may be to extend the existing poll to also read intent terminal-status, avoiding a new stream.
2. **Scoping.** The signal must be scoped to *this strategy's* emit (provenance / virtual_position
   ledger) — respect the OMS scoping invariant; never react to another actor's reject.
3. **Ordering / races.** Reject-then-poll vs poll-then-reject; ensure the release can't fire before the
   emit is recorded, and can't double-release.
4. **Keep the backstop.** The 12s timeout stays as the floor for cancels / lost signals — the reject
   signal only *shortens* the wait when present.
5. **Backtest faithfulness.** `v2_sim` would model reject-signal release (it already has `reject_bar_idxs`
   as the no-fill hook — extend it to release at the reject bar's poll rather than at timeout).

## When to pick up

After PR #404 is deployed and D3/D5 re-run on the corrected entry. Sequence stays: correctness (blind
timeout) first, latency optimization (reject signal) second.

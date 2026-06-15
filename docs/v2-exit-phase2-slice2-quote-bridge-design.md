# Track 2 Phase 2 — Slice 2: gateway quote-consumer bridge — DESIGN + BUILD

**Status:** design + build, for review. **No deploy.** Second of 4 Phase-2 slices.
**Builds on:** Slice 1 (`oms_managed_positions`, #305). **Overall design:** #299 (Approach B).

**The problem it closes (from the §Q4-followup probe):** the OMS's quote cache is
gateway-sourced, and the gateway subscribes to the momentum bots' *retained* `active_symbols`
(`strategy_engine_app.py:market_data_symbols()` — which **excludes v2**). v2's watchlist comes
from the *broader* scanner pool, so they overlap **in practice** (Fri 17/17, zero gap) but **not
by guarantee** — a v2-traded symbol no momentum bot retained would leave the OMS **price-blind**
for that position. Unacceptable for a risk path (slice 3). This slice makes coverage a guarantee.

---

## The bridge

v2 **registers its watchlist as a gateway subscription CONSUMER.** The gateway already unions
desired symbols across consumers (`gateway.py:apply_subscription_event` — keyed by `consumer_name`,
`mode="replace"`, set-union). v2 publishes a `MarketDataSubscriptionEvent` with
`consumer_name=SERVICE_NAME ("schwab-1m-v2")`, `mode="replace"`, `symbols=sorted(watchlist)` to the
`market-data-subscriptions` stream — **mirroring verbatim** the strategy-engine's
`_sync_market_data_subscriptions`. The gateway then streams v2's symbols → the OMS (already
consuming gateway quotes) covers them **with a guarantee**. No duplicate feed; v2 stays **entry-only**
(it declares *interest*, it does not emit quotes or market data).

**Implementation** (`services/schwab_1m_v2_bot.py`):
- New `async _sync_gateway_subscription()` — gated, debounced (publishes only when the symbol set
  changes, tracked via `self._last_gateway_symbols`).
- Called from the async scanner loop after the watchlist is (re)computed: once after the cold-start
  **seed**, and after each **tail pass** (`_apply_strategy_state_event` is sync, so the publish runs
  in its async callers; the debounce makes repeated calls no-ops).

## Gating / inert-when-OFF (the load-bearing property)

Gated on the **single Phase-2 flag** `oms_v2_exit_management_enabled` (default OFF). When OFF,
`_sync_gateway_subscription` **returns immediately — publishes nothing, registers no consumer, streams
no extra symbols.** Identical to today (the OMS doesn't use v2 quotes until slice 3). When the operator
flips the flag ON at slice-3 activation, v2 begins registering its symbols → the gateway streams them
→ the OMS cache covers them — one flag turning on the whole v2-exit system coherently.

*Withdraw note:* a clean ON→OFF withdrawal (publishing an empty `replace` to drop v2's symbols from
the gateway union) is a **slice-3 activation/deactivation-lifecycle** concern, not needed for the
dormant default (which never registers). Flagged, deferred — settings aren't hot-reloaded, so a
flag-flip requires a v2 restart anyway, at which point OFF simply never re-registers.

## Tests (`tests/unit/test_v2_gateway_subscription.py`, 4 — harness w/ fake async redis)

- **dormant-when-OFF:** flag OFF → zero publishes, state untouched (the inert proof).
- **publishes-when-ON:** correct stream + `consumer_name="schwab-1m-v2"` + `mode="replace"` +
  `symbols` sorted; `maxlen=250`, `approximate=True` (mirrors the engine).
- **debounced:** same watchlist twice → one publish.
- **republishes-on-change:** watchlist grows → second publish with the new set.
40 existing v2-bot tests still pass; lint clean.

## Not in slice 2

- No quote→Position update wiring (slice 3, co-located with the eval — per the slice-1 review).
- No exits / sells (slice 3, gated additionally on the paper-isolation re-proof).
- No ON→OFF withdraw lifecycle (slice 3 activation work).

## Honest boundaries

- Inert by default; its only effect when ON is that the gateway streams v2's symbols (which the OMS
  already consumes) — no behavior change to v2's entries, bars, or the momentum bots.
- This guarantees **coverage**, not **fill realism** — quotes are bid/ask; the per-quote risk legs
  (slice 3) and their idealized-sim caveat are unchanged. Deploy gate remains at slice 3.

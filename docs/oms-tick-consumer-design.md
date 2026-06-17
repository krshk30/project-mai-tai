# OMS tick-by-tick consumer — intrabar exit latency fix

**Status:** implemented (Phase 1). **Branch:** `fix/oms-tick-consumer-intrabar`.
**Trigger:** 2026-06-17 live LNAI ATR-Flip trade — the +2% scale fired ~70s late and
filled at 4.345 instead of ~4.45.

## Problem (evidence-pinned)

LNAI entry 14:33:04 ET @ 4.34 (qty 10). The market-data feed received bids **above the +2%
level (4.4268)** for a ~14s window — `bid 4.43→4.45→4.46→4.44→4.45` between 18:36:28–42 UTC
(trades printed 4.48). Yet the `SCALE_PCT2` order was submitted at **18:37:42** and filled
**4.345** (+0.1%), after LNAI round-tripped the whole spike inside one minute.

### Root cause (two compounding defects)

1. **Quote-consumption lag in the OMS, worst exactly during the burst.** The OMS main loop
   read the shared `market-data` + `strategy-intents` streams in ONE `xread`, then ran
   `sync_broker_state()` **inline every 5s** (`oms_broker_sync_interval_seconds=5`) — a batch
   of sequential broker REST calls across all accounts. While that ran, the loop stopped
   reading quotes, so ticks piled up in Redis and were drained 50-at-a-time afterward. During
   the high-volume spike the lag ballooned to ~70s (the same position's later floor-breach
   close lagged far less — the lag tracked tick volume, the signature of a single loop falling
   behind during a tick storm).
2. **The 5s staleness guard was blind to it.** `_handle_quote_tick_event` stamped
   `received_at = utcnow()` at *processing* time, not the producer's event time. A 70s-
   backlogged quote was seen as "age ≈ 0ms" and sailed through `oms_v2_exit_quote_max_age_ms`
   (5000). The `+2%` scale decision (`Position.get_scale_action` keys off the **current** bid)
   therefore fired against a stale 4.45 while the market order executed into the 4.34 market.

The exit engine itself was already quote-driven with an intrabar check — the failure was the
feed/consumption path, not the ladder logic.

## Design (Phase 1 — the tick-by-tick guarantee)

`src/project_mai_tai/oms/service.py`:

1. **Dedicated tick consumer task** (`_run_tick_consumer`) reads the `market-data` stream on
   its OWN asyncio task, launched by `run()`. The control loop (`_run_control_loop`) keeps
   intents + periodic broker-sync + heartbeat on the `strategy-intents` stream. A slow
   broker-sync REST can no longer starve quote evaluation — the structural guarantee.
   Stream offsets split: `_intent_offsets` / `_market_offsets`.
2. **Last-quote-wins coalescing** (`_coalesce_ticks`, pure/static): each read burst collapses
   to the freshest quote per symbol before evaluation, so a tick storm cannot build a serial
   backlog — the ladder always decides on the current price within ms. Trades are NOT
   coalesced (preserved in arrival order) to keep armed-hard-stop fidelity.
3. **Event-time staleness**: `received_at` is stamped from `event.produced_at` (market-data's
   publish time; same host as the OMS → no clock skew), so the 5s guard measures TRUE price
   age and rejects a backlogged quote instead of acting on it.

## Tradeoffs / edge cases

- **Coalescing drops intermediate quotes.** Intended: the profit/floor ladder only cares
  about the current price; acting on stale intermediates only adds latency. Hard stops also
  run on every (un-coalesced) trade tick, so downside protection is unaffected.
- **Guard now rejects backlogged quotes (could *miss* a scale under residual lag).** Correct
  trade-off: a missed late scale is caught by the floor; acting on a 70s-stale price is worse.
  Phase 2 (resting-limit brackets) removes the dependence on the OMS reacting at all.
- **`received_at` semantics also feed the armed-hard-stop guard** (`_evaluate_hard_stop_
  market_event`) — now event-time there too, a consistent correctness improvement (rejects
  stale quotes for stop decisions). Behavior-identical when quotes are fresh (the normal case).
- **Two tasks share `self` state** (`_latest_quotes_by_symbol`, `_managed_v2_symbols`, sessions).
  Safe under asyncio's cooperative single-thread model; each DB op uses its own session.
- **Shutdown**: `run()` cancels the tick task in `finally` and awaits the `CancelledError`.

## Overlapping state-mutation audit

`_stream_offsets` (OMS) was referenced only in `run()` — split cleanly. The identically-named
field in `strategy_engine_app.py` is a different class, untouched. `_handle_stream_message`
keeps its quote/trade branches (now unused from the control loop) so existing direct-call tests
still pass — no behavior change.

## Tests (`tests/unit/test_v2_managed_exit.py`)

- `test_handle_quote_uses_event_time_for_staleness` — 70s-old event rejected; fresh acts (the
  exact LNAI bug).
- `test_coalesce_last_quote_wins` / `_keeps_one_freshest_quote_per_symbol` — freshest quote
  per symbol survives.
- `test_coalesce_preserves_all_trades_in_order` — trades not coalesced.
- `test_coalesce_ignores_unknown_and_symbolless` — robustness.
- All 7 pre-existing exit-ladder tests + OMS roundtrip integration remain green (57 passed,
  1 xfailed).

## Rollback

Revert the PR; the change is contained to `oms/service.py`. No schema/migration, no settings
change, no API change.

## Phase 2 (follow-up, design-first)

Pre-staged resting-limit bracket orders at entry (scale/floor/stop as broker-resident orders)
so fills happen at exchange speed independent of OMS reaction — belt-and-suspenders over the
ms-latency this PR delivers.

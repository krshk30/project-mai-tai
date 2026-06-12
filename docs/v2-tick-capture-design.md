# Design — Schwab tick-by-tick capture for replay (schwab_1m_v2)

**Status:** design + implementation landed behind a default-OFF flag (ships dormant).
**Deploy gate:** attended, after-close, flag flipped during an RTH window — NOT before the
08:00 UTC roll. **No strategy/OMS behavior change.**

## Goal & non-goals

Durable, append-only Schwab tick storage so an ambiguous 1-minute candle (one that touched both
`+target%` and `-stop%`) can be replayed to the **actual first hit**. This is **data capture +
replay evidence only**. Entry/exit rules, bar-build, and OMS are untouched.

Non-goals: changing how v2 decides; feeding ticks into the strategy; reviving the shared
`schwab_streamer.py`; tick-built bars.

## Key decision — how to source LEVELONE without a second streamer session

Schwab permits **one streamer WS session per OAuth token**. v2's `schwab_v2_streamer.py` already
holds that single session (CHART_EQUITY only). A `data` push can carry **multiple service
subscriptions on the same session**, so:

- **CHOSEN — Option A: add `LEVELONE_EQUITIES` as a second subscription on v2's existing session.**
  One session, no new collision, no new OAuth consumer. The CHART_EQUITY bar path is left
  **byte-for-byte unchanged**; LEVELONE is a parallel, additive, flag-gated branch.
- Rejected — revive `schwab_streamer.py` (off-limits shared file; it opens a *second* session →
  collides with v2's). Rejected — a new isolated LEVELONE service (same single-session collision).

Why LEVELONE and not TIMESALE: prior evidence shows `TIMESALE_EQUITY` is unavailable/unreliable in
this Schwab setup; `LEVELONE_EQUITIES` is the proven live source. Field map (from the existing
`schwab_streamer.py`, verified against live capture): subscribe fields `0,1,2,3,4,5,8,9,35`.
- **trade** fields: `3`=last_price, `9`=last_size, `8`=cumulative_volume, `35`=trade_time_ms.
- **quote** fields: `1`=bid, `2`=ask, `4`=bid_size, `5`=ask_size.
A single LEVELONE content record may update any subset → it can yield a trade tick, a quote tick,
or both.

## Overlapping-state-path audit (streamer design-first discipline)

The only new state in `schwab_v2_streamer.py`:
- `_tick_capture` (bool, derived from flag + `on_tick` present). Read-only after init.
- The subscription delta (`_requested_symbols`, `_apply_subscription_delta`, `_send_subscription`)
  is **reused unchanged for symbol membership**; `_send_subscription` now emits the CHART_EQUITY
  request **and** (only when `_tick_capture`) a LEVELONE request in the same frame. The
  `_requested_symbols` set still drives both services (same watchlist) — no second tracker, no new
  race. UNSUBS covers both services symmetrically.
- `_handle_message` keeps the CHART_EQUITY branch verbatim; a new branch handles
  `service == LEVELONE_EQUITIES` and routes extracted ticks to `on_tick`. CHART_EQUITY dedupe
  (`_last_bar_ts_ms`) is untouched; LEVELONE has **no** dedupe in the streamer (append-only capture
  wants every update; dedup is the DB unique constraint's job).
- `on_tick` exceptions are caught and logged (like `on_chart_bar`) so a writer failure can never
  kill the receive loop or the bar feed.

Flag OFF (default): no LEVELONE SUBS is ever sent, the LEVELONE branch is unreachable, `on_tick` is
`None` — **zero behavior change, identical to today.**

## Write path — batched observer (never blocks the streamer)

LEVELONE is high-frequency. A per-tick synchronous DB insert would stall the receive loop (the
persist-lag failure class). So:

- `on_tick` appends to an in-memory buffer (`SchwabV2TickWriter`).
- A background flush task drains the buffer every `flush_interval_secs` (default 2s) or when it
  reaches `flush_batch_size` (default 500), whichever first.
- Flush does a single batched `INSERT ... ON CONFLICT DO NOTHING` per table inside
  `asyncio.to_thread` (off the event loop), so the streamer never awaits the DB.
- Bounded buffer (`max_buffer`, default 50_000): on overflow, drop oldest + count `dropped` in the
  heartbeat (capture is best-effort evidence, not execution-critical — never apply backpressure to
  the feed).

The writer is a **pure tee**: it shares nothing with `strategy.on_bar` / entry. It cannot move a
signal.

## Tables (append-only, normalized)

`market_trade_ticks` and `market_quote_ticks` per the requirement. Notable columns: `raw jsonb`
(the exact LEVELONE content), `raw_hash text` (sha1 of canonical raw), and the dedupe key.

- **Dedupe / unique:** `(provider, service, symbol, event_ts, raw_hash)`. Partial field updates at
  the same `event_ts` carry different raw → different hash → both kept (real distinct updates);
  exact re-sends collapse.
- **Query index for replay:** `(symbol, event_ts)` on trade ticks (the replay walks this).
- `event_ts`: trade = field 35 (trade_time_ms); quote = the data item's top-level `timestamp` (no
  separate quote-time field is subscribed), falling back to `received_at` if absent.

Migration: `sql/migrations/versions/20260611_0007_market_ticks.py` (Alembic, chains off `..._0006`).
ORM models added to `db/models.py`.

## Retention / index plan (tables must not grow uncontrolled)

- **Index discipline:** only the two indexes above. No per-column sprawl.
- **Time-based retention:** `scripts/prune_market_ticks.py --keep-days N` deletes
  `received_at < now() - N days` in batches (default keep 14 days). Intended as a daily cron:
  `0 9 * * * … prune_market_ticks.py --keep-days 14`. Documented in the script + this doc.
- **Volume estimate:** ~10–30 active penny symbols × LEVELONE updates; at ~a few hundred
  rows/symbol/min over a ~6.5h RTH ≈ low-single-digit millions/day worst case. 14-day retention with
  the narrow index set is comfortably within the VPS Postgres. If it grows, the next step is
  **native range partitioning by `received_at::date`** (drop old partitions instead of DELETE) —
  noted as the scale follow-up, not built now.

## Replay — `scripts/replay_exit_from_ticks.py`

Inputs: `--strategy --symbol --entry-ts --entry-price --qty --target-pct --stop-pct --window-mins`.
Walks `market_trade_ticks` for the symbol in `[entry_ts, entry_ts+window]` ascending by `event_ts`;
returns the **first** tick where `price >= entry*(1+target)` (TARGET) or `price <= entry*(1-stop)`
(STOP), with the exact `event_ts` and P&L. If there are **zero trade ticks** in the window, returns
**`UNRESOLVED_NO_TICKS`** — never a guessed answer. (Quote ticks are captured too but the replay is
trade-print-based, matching how a stop/target actually triggers.)

## Acceptance criteria → how this meets them

| Criterion | Mechanism |
|---|---|
| Active symbols write trade/quote rows continuously during RTH | LEVELONE SUBS on the live session + batched writer |
| DB tick count increases while bot receives ticks | append-only inserts; heartbeat exposes `ticks_written` / `dropped` |
| Replay of a both-hit candle returns the actual first hit | `replay_exit_from_ticks.py`, first-tick walk |
| No strategy / OMS change | tee-only; flag OFF = identical behavior; entry/exit/OMS files untouched |
| Retention/index plan | narrow indexes + `prune_market_ticks.py` + partition follow-up |

## Flags (settings.py)

- `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TICK_CAPTURE_ENABLED` (default `false`) — master switch.
- `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TICK_FLUSH_INTERVAL_SECS` (default `2.0`)
- `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TICK_FLUSH_BATCH_SIZE` (default `500`)
- `MAI_TAI_STRATEGY_SCHWAB_1M_V2_TICK_MAX_BUFFER` (default `50000`)

## Activation runbook (tomorrow, attended, after-close → verify RTH)

1. Apply migration (`alembic upgrade head`) — additive, creates two empty tables.
2. Deploy code (flag still OFF) — dormant, behavior identical. Confirm v2 healthy.
3. After-close, flip `…TICK_CAPTURE_ENABLED=true`, restart v2 only. Watch the existing v2 log: the
   LEVELONE SUBS should ack (code 0) on the same session; CHART_EQUITY bars must keep flowing
   unchanged (the collision/regression check). Heartbeat `ticks_written` climbs.
4. RTH verify: `SELECT count(*) FROM market_trade_ticks WHERE symbol=$1 AND event_ts > now()-'5 min'`
   increases for an active symbol; run `replay_exit_from_ticks.py` on a known both-hit candle.
5. Rollback: flag OFF + restart v2 → REST/CHART_EQUITY path identical to today.

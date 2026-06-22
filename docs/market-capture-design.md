# Central market-data capture (global, bot-agnostic)

**Goal.** Persist the *raw* Polygon/Massive trade prints + L1 quotes that flow
through the `mai_tai:market-data` gateway stream into a durable, central store
that ANY bot can query for backtesting. Bars (polygon_30s 30s, ORB 1m) aggregate
the intra-bar tick sequence away; the tick-confirmation / intrabar-hold-confirm
work needs the raw prints. The Redis stream is a ~8-minute window — uncaptured
ticks are lost forever. This is the irreplaceable-data capture.

**Why a standalone consumer (not a bot tee).** The dead Schwab TIMESALE capture
(#335) was bolted to v2. This lives at the shared-infra level: a separate
service that only *reads* the gateway stream and *writes* central tables. It is
NOT part of any bot; today's and tomorrow's bots all query the same store.

## Architecture
- `services/market_capture_app.py` — a flag-gated (`market_capture_enabled`,
  default OFF), READ-ONLY consumer. `xread`s `mai_tai:market-data`, dispatches by
  `event_type`, and writes parsed rows. Touches NOTHING in the trading path:
  no gateway change, no bot change, no order flow.
- **Off-loop batched writes (#350 pattern):** events buffer and flush via
  `asyncio.to_thread` (batch size / flush interval configurable) so the consumer
  loop never stalls at 50–150+ inserts/sec.
- **Timestamp normalization (ORB-bug lesson):** `market_data/tick_time.py`
  `normalize_ts_ns` — the live WS feed's `timestamp_ns` is *milliseconds*; REST
  historical is *nanoseconds*. Normalized to true ns before storage so a 1970
  timestamp can never be persisted. (Canonical home; ORB + strategy-engine
  reimplement the same ladder — future dedupe candidate.)
- **Tables (`db/models.py`, migration `20260622_0009`):** per-type, not one
  event-typed table — justified: trades are ~40–60× the volume of quotes, so
  per-type gives independent retention/indexing + clean typed columns (no sparse
  nullables) + book/L2 addable as its own table later. Append-only, **no `raw`
  blob** (parsed columns only ≈ half the size), **no unique-dedupe constraint**
  (the live payload has no trade id/sequence; restart-replay dupes are rare and
  de-duped at backtest time) → cheap high-volume inserts.

## Event types
Captured today: `trade_tick` (price/size/exchange/conditions/cumulative_volume)
and `quote_tick` (bid/ask/sizes; `event_ts` from `produced_at`, since quotes
carry no payload timestamp). **No book/L2 flows on the stream today** — add an
`elif` branch + a `market_capture_book` table when it does (no schema rewrite).

## Volume / retention (mandatory from day one)
~1–2M trade rows/day typical, 3–6M heavy (~40–60× the Schwab LEVELONE table);
~0.5–2 GB/day. Pruned by `scripts/prune_market_ticks.py --tables
market_capture_trades,market_capture_quotes --keep-days 14` via
`project-mai-tai-prune-capture.timer` (daily 09:30 UTC). Disk: 99 GB free →
steady-state ~7–30 GB. The decider needs ~10–14 days, so retention is tight.

## Backfill (separate capability — entitlement CONFIRMED)
Massive REST `list_trades` (`/v3/trades`) returns historical tick trades for
both large- and small-caps on our key (verified 2026-06-22). So recent days can
be backfilled for immediate backtesting; this forward-capture is the continuous,
all-symbol, cheaper-than-repeated-REST complement.

## Deploy
Console script `mai-tai-market-capture`; unit
`ops/systemd/project-mai-tai-market-capture.service`. Enable with
`MAI_TAI_MARKET_CAPTURE_ENABLED=true` in the env file + start the service.
Additive/isolated — no other service restarts.

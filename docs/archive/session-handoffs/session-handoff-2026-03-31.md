# Session Handoff - 2026-03-31

This file is the primary handoff for a new agent picking up `project-mai-tai`
after the March 31, 2026 live trading, parity, and VPS recovery session.

## April 1 Continuation Note

There is now a follow-on handoff for the next session:

- [session-handoff-2026-04-01.md](C:/Users/kkvkr/OneDrive/Documents/GitHub/project-mai-tai/docs/session-handoff-2026-04-01.md)

Important April 1 fixes added after this March 31 handoff:

- stale prior-session `Momentum Confirmed` rows were blocked from reviving into
  the current scanner session
- scanner state now rolls cleanly at the `4:00 AM ET` session boundary
- legacy shadow divergence now compares legacy confirmed names against new
  confirmed names instead of the new watchlist
- scanner alert restart behavior now restores visible alert tape as well as
  hidden warmup buffers
- Redis `snapshot-batches` retention was increased because the old retention
  was too short to support 5 to 10 minute scanner warmup reconstruction after
  restart
- strategy indicator VWAP was briefly re-anchored from `9:30 AM ET` to
  `4:00 AM ET` on April 1 during premarket debugging, but a later TradingView
  parity review showed the desk reference is regular-session anchored, so the
  final live config was reverted back to `9:30 AM ET`
- a later April 1 follow-up then added dual VWAP handling:
  - regular-session `vwap` stays anchored at `9:30 AM ET` for TradingView
    parity
  - premarket bot entry logic uses a separate `extended_vwap` anchored at
    `4:00 AM ET` before the regular session opens
- live confirmed/watchlist selection was later relaxed so already-confirmed
  names like `RENX` and `BCG` are no longer hidden just because they fall
  below the old static `rank_min_score=50` cutoff
- the confirmed scanner was later split back into:
  - full session `all_confirmed`
  - score-qualified `top_confirmed/watchlist` for bots
- scanner dashboard HTML was later corrected to display the full confirmed
  universe instead of only the bot-fed subset, with a heartbeat-based live
  indicator fallback when subscription stream detail lags
- exact legacy score parity is still not solved; current Mai Tai score remains
  a relative peer-ranking formula rather than a known imported legacy formula
- afternoon strategy instability on April 1 was later traced to memory
  retention in scanner history/runtime caches; a live memory-compaction and
  state-pruning fix was deployed, and the 30s decision tape resumed advancing
  without immediate restart churn

## Current Outcome

At end of session, the live stack was recovered and healthy again:

- `/health` = `healthy`
- database connected
- Redis connected
- no pending intents
- no open `virtual_positions`
- no open `account_positions`
- no open reconciliation findings
- scanner `status = active`
- live watchlist = `BFRG`, `ELAB`, `KIDZ`, `MASK`
- `active_subscription_symbols = 4`

The main work completed during this session fell into four groups:

1. strategy/runtime state convergence fixes
2. TradingView parity investigation and `30s` bar/VWAP fixes
3. scanner/candidate/watchlist cleanup behavior
4. VPS/Redis recovery and hardening

## Additional Important Fixes Completed Earlier In The Session

These were also completed during the March 31 session and should not be
overlooked by a new agent.

### Order Routing / Execution Plumbing

- premarket / after-hours orders were changed to use:
  - `limit + day + extended_hours=true`
  instead of falling through to default market-order behavior
- paper timeout was changed from `20s` back to `10s`
- pre/post-market pricing was aligned to legacy behavior:
  - buy at live `ask`
  - sell at live `bid`
  - rounded to 2 decimals

### Bot / UI Day-Scoped Behavior

- bot pages stopped showing yesterday's rolling recent orders/fills in the
  normal "today" bot view
- runtime `daily_pnl` and `closed_today` were given ET day-rollover reset
  behavior so values do not bleed across midnight

### Scanner Feed / Candidate Cleanup

- confirmed names were pruned when live `change_pct` fell below `20%`
- final confirmed/top feed also enforced `min_change_pct=20.0` so faded names
  could not leak back into live watchlists
- control-plane live scanner fallback was hardened so transient Redis stream
  issues would not make the scanner look falsely `idle`

### Config / Parity Alignment Work

The following values were aligned during the session:

- momentum min volume = `100k`
- squeeze 5m = `5%`
- squeeze 10m = `10%`
- alert cooldown = `5m`
- top gainers RVOL = `5`
- confirmed max float = `50M`
- `stoch_exit_level = 20`

The following remained intentional user choices:

- `confirmed_min_volume = 500k`
- `30s confirm_bars = 1`

### Trading Hours

- code was updated to support trading through `8:00 PM ET`
- stale `18:00 ET` messages later in the day were runtime/deploy mismatch
  symptoms, not the intended config

## Most Important Conclusions

### 1. Execution-State Drift Was Internal

The broker was often clean while Mai Tai still showed stale local runtime
positions. This was not primarily an Alpaca problem.

Confirmed bugs fixed during this session:

- cumulative broker `filled_quantity` was being applied more than once in
  strategy runtime
- sync-discovered OMS fills/status changes were not always being published back
  onto `order-events`
- strategy runtime did not treat `no strategy position available to sell` as a
  ghost-position cleanup signal
- duplicate/overlapping exit retries could re-spam closes after state was
  already stale

Practical result:

- OMS, Alpaca, reconciler, and bot runtime state now converge much more
  reliably than they did earlier in the day

### 2. Bar Parity Was A Real Root Cause

The user correctly identified that Mai Tai was entering earlier than
TradingView because live bar construction drifted from chart bars.

What was confirmed:

- TradingView CSV comparisons for `ELAB` and `BFRG` showed that Mai Tai's
  EMA/MACD/signal/histogram calculations were already very close when using the
  same OHLCV bars
- the real remaining mismatch was bar formation, not indicator formula math

Bar-parity changes applied:

- removed odd-lot filtering from `30s` bar construction
- removed synthetic flat-fill bars from indicator inputs
- aligned VWAP session reset with TradingView session anchoring

Relevant files:

- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/bar_builder.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/indicators.py`
- `/Users/velkris/src/project-mai-tai/scripts/compare_tv_csv.py`

Important verification:

- the VPS checkout was later checked directly and confirmed to include
  `4f6d4be` - `Align 30s bar construction with TV session bars`

### 3. Scanner Warmup Had To Move Away From Heavy Redis History

Scanner warmup originally depended too much on Redis `snapshot-batches`.

Final direction implemented:

- persist lightweight scanner warmup state in dashboard snapshots / Postgres
- reduce `redis_snapshot_batch_stream_maxlen`
- treat Redis as transient cache/event bus, not a durable history store

Primary commit:

- `47ec003` - `Persist scanner warmup state outside Redis`

### 4. Redis OOM Was The Main VPS Incident

Redis was repeatedly OOM-killed while loading its persisted cache snapshot.

Observed symptoms:

- `redis-server.service` repeatedly died with `status=9/KILL`
- journal showed the OOM killer
- Redis was stuck in `Status: "Redis is loading..."`

What recovered the box:

- stop app services
- move `/var/lib/redis/dump.rdb` aside instead of deleting it blindly
- restart Redis clean
- restart control-plane and strategy after Redis recovered

What was applied afterward:

- live Redis configuration set to:
  - `maxmemory 512mb`
  - `maxmemory-policy allkeys-lru`
  - `save ""`
  - `appendonly no`
- Redis memory returned to a small baseline

Related repo changes:

- `/Users/velkris/src/project-mai-tai/ops/bootstrap/02_prepare_host.sh`
- `/Users/velkris/src/project-mai-tai/ops/bootstrap/README.md`
- `/Users/velkris/src/project-mai-tai/docs/live-market-restart-runbook.md`

## Candidate / Scanner Behavior Changes

Behavior changes implemented or verified during this session:

- bot pages now scope "today" activity to ET day instead of mixing yesterday
  into the same normal day view
- runtime `daily_pnl` / `closed_today` reset on ET day rollover
- confirmed names are pruned when live `change_pct` falls below `20%`
- final confirmed/watchlist feed also enforces the same `20%` minimum so faded
  names cannot leak into live bot feeds
- live confirmed names again feed all bot watchlists after restart

## Strategy Selection / Legacy Drift Context

By the end of the session, the main remaining parity concern was no longer
basic indicator math. The bigger open questions for the next agent are:

- live `30s` entry timing versus TradingView
- strategy-selection drift versus legacy shadow
- whether `30s confirm_bars = 1` is helping enough to justify the earlier
  entries

## Config / Strategy Alignment Changes

Values intentionally aligned during the session:

- momentum min volume = `100k`
- squeeze 5m = `5%`
- squeeze 10m = `10%`
- alert cooldown = `5m`
- top gainers RVOL = `5`
- confirmed max float = `50M`
- `stoch_exit_level = 20`

Still intentional user choices:

- `confirmed_min_volume = 500k`
- `30s confirm_bars = 1`

Trading hours:

- code was updated to support trading through `8:00 PM ET`
- any later stale `18:00 ET` messaging was a runtime/deploy mismatch, not the
  intended config

## VPS / Access Notes

Important operational discovery from this session:

- the VPS public IP had changed from the previously assumed
  `145.223.75.4`
- actual working VPS public IP is:
  - `104.236.43.107`

The current Windows machine now has a working SSH host alias:

```bash
ssh mai-tai-vps
```

The local SSH config points to:

- user: `trader`
- host: `104.236.43.107`
- identity: `C:\Users\kkvkr\.ssh\mai_tai_vps`

This IP change explains why direct SSH troubleshooting looked inconsistent late
in the session.

## Branch / Merge State

Additional handoff-related changes made from the Codex worktree were merged to
`main` at the end of the session:

- `675a3fb` - `Harden Redis bootstrap defaults`
- `f80d608` - `Add TradingView CSV comparison helper`

So `origin/main` now includes:

- Redis bootstrap hardening
- Redis OOM recovery documentation
- TradingView CSV comparison helper script

## Best Next Steps For A New Agent

1. Use the live bar-parity tooling against fresh TradingView exports during the
   next active session.
2. Keep validating `30s` entry timing against TradingView because the user is
   primarily concerned with buy timing parity.
3. Continue `30s` trading-quality review using the actual entry timestamps
   already collected during the March 31 session.
4. Keep Redis treated as a bounded cache/event bus; do not reintroduce large
   persistence/retention assumptions casually.
5. If deployment/restart work is needed again, use the now-working
   `ssh mai-tai-vps` path and remember that the VPS public IP is
   `104.236.43.107`.

## April 1 Continuity Note

Additional live changes landed on April 1, 2026 ET after this handoff:

- scanner/dashboard behavior was split back into:
  - full retained confirmed universe
  - score-qualified bot/watchlist subset
- the scanner page live indicator now falls back to heartbeat/fresh snapshot
  activity when subscription-stream detail lags
- `macd_30s` VWAP guardrails were widened to:
  - precondition `1.00%`
  - anti-chase `1.50%`
  - hard block `8.00%`
- `macd_30s` now also has a narrow structure-aware near-high stall block plus a
  stoch-health guard that delays stoch-based exits while momentum is still
  improving
- a later April 1 replay-backed update then loosened `macd_30s` further:
  - soft VWAP precondition disabled
  - soft anti-chase VWAP disabled
  - hard VWAP widened to `25%`
  - EMA9 precondition widened to `1.00%`

See the newer handoff for details:

- `docs/session-handoff-2026-04-01.md`

# Live Market Restart Runbook

Use this runbook during an active trading session.

Current state as of March 29, 2026:
- broker-side orders and positions survive service restarts
- Postgres-backed orders, fills, `virtual_positions`, and `account_positions` survive service restarts
- `strategy-engine`, `oms-risk`, and `market-data-gateway` do not fully replay downtime state on restart
- `ops/systemd/restart_all.sh` is for off-hours use only

This means live-market restarts must be coordinated.

## Service Risk Levels

Lower-risk restarts during market hours:
- `project-mai-tai-control.service`
- `project-mai-tai-reconciler.service`

Higher-risk restarts during market hours:
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-market-data.service`

Golden rules:
- prefer restarting only the service you changed
- prefer restarting while flat
- do not use `ops/systemd/restart_all.sh` during an active session
- stop `project-mai-tai-strategy.service` before restarting `project-mai-tai-oms.service`
- stop `project-mai-tai-strategy.service` before restarting `project-mai-tai-market-data.service`

## Useful Checks

Dashboard/API checks:
- `https://project-mai-tai.live/api/overview`
- `https://project-mai-tai.live/api/scanner`
- `https://project-mai-tai.live/api/bots`
- `https://project-mai-tai.live/api/orders`
- `https://project-mai-tai.live/api/positions`
- `https://project-mai-tai.live/api/reconciliation`

Service checks:
```bash
sudo systemctl status \
  project-mai-tai-market-data.service \
  project-mai-tai-strategy.service \
  project-mai-tai-oms.service \
  project-mai-tai-reconciler.service \
  project-mai-tai-control.service \
  --no-pager
```

Tail logs:
```bash
sudo tail -n 80 /var/log/project-mai-tai/strategy.log
sudo tail -n 80 /var/log/project-mai-tai/oms.log
sudo tail -n 80 /var/log/project-mai-tai/market-data.log
sudo tail -n 80 /var/log/project-mai-tai/reconciler.log
sudo tail -n 80 /var/log/project-mai-tai/control.log
```

Follow one service live:
```bash
sudo journalctl -u project-mai-tai-strategy.service -n 100 --no-pager -f
```

Convenience scripts:
- `bash ops/systemd/restart_control_live.sh`
- `bash ops/systemd/restart_reconciler_live.sh`
- `bash ops/systemd/restart_strategy_live.sh`
- `bash ops/systemd/restart_oms_live.sh`
- `bash ops/systemd/restart_market_data_live.sh`

Optional hold behavior:
- `bash ops/systemd/restart_oms_live.sh --hold-strategy`
- `bash ops/systemd/restart_market_data_live.sh --hold-strategy`

## Preflight Before Any Trading-Critical Restart

Do these checks first:
1. Open `/api/orders`, `/api/positions`, and `/api/reconciliation`.
2. Confirm whether any strategy has open positions, pending opens, pending closes, or recent fills still settling.
3. If there are open positions, assume the restart is risky and prefer waiting until flat.
4. If there are `pending`, `submitted`, or `accepted` intents, do not restart yet.
5. If there are manual operator actions in progress, finish them first.

Safe-to-proceed signal:
- no pending or in-flight strategy intents
- no order you are actively waiting to fill or cancel
- operator understands the current broker/account positions

## Control Plane Restart

Use when:
- only dashboard code changed
- API/UI is stale or broken

Command:
```bash
sudo systemctl restart project-mai-tai-control.service
```

Post-checks:
- `/api/overview` loads
- `/api/orders` and `/api/positions` still render
- no trading services changed state unexpectedly

Expected impact:
- UI/API blip only
- trading pipeline keeps running

## Reconciler Restart

Use when:
- only reconciliation logic changed
- reconciliation worker is stuck

Command:
```bash
sudo systemctl restart project-mai-tai-reconciler.service
```

Post-checks:
- `/api/reconciliation` updates again
- no new critical findings appear unexpectedly

Expected impact:
- detection gap only while the service restarts
- trading pipeline keeps running

## Strategy Restart

Use when:
- only `strategy-engine` code changed
- market data and OMS are otherwise healthy

Important limitation:
- runtime bot memory does not fully rehydrate from OMS/DB on restart
- dashboard may still show broker and virtual positions even if runtime bot positions reset

Procedure:
1. Open `/api/bots`, `/api/orders`, and `/api/positions`.
2. Confirm there are no pending opens, pending closes, or active order acknowledgements still in flight.
3. If not flat, pause and decide whether the restart is worth the risk.
4. Restart the strategy service:

```bash
sudo systemctl restart project-mai-tai-strategy.service
```

Post-checks:
1. `/api/overview` shows `project-mai-tai-strategy.service` healthy.
2. `/api/scanner` returns live data again.
3. `/api/bots` shows watchlists rebuilding.
4. `/api/positions` still shows broker/account positions and virtual positions.
5. `/api/reconciliation` does not show new critical drift.

If runtime positions disappear but account positions remain:
- treat the bot as not fully recovered
- do not assume the strategy remembers the position lifecycle
- watch for reconciliation drift and do not make a second trading-critical change casually

## OMS Restart

Use when:
- only OMS or broker adapter code changed
- you need to rotate broker credentials or env values

Important limitation:
- `oms-risk` reads new strategy intents after restart and does not safely queue intents emitted while it is down
- this means `strategy-engine` must be stopped first

Procedure:
1. Stop new bot intents:

```bash
sudo systemctl stop project-mai-tai-strategy.service
```

2. Wait for OMS to drain:
- refresh `/api/orders`
- refresh `/api/positions`
- confirm no `pending`, `submitted`, or `accepted` intents remain
- confirm no cancel/replace/fill workflow is still in progress

3. Restart OMS:

```bash
sudo systemctl restart project-mai-tai-oms.service
```

4. Wait for OMS to become healthy and repopulate broker-account positions.

5. Start strategy again:

```bash
sudo systemctl start project-mai-tai-strategy.service
```

Post-checks:
1. `/api/overview` shows `project-mai-tai-oms.service` healthy.
2. `/api/positions` shows `account_positions` repopulated.
3. `/api/orders` shows no unexpected new rejected or duplicate orders.
4. `/api/reconciliation` stays clean or only shows pre-existing issues.
5. `/api/bots` shows strategy service healthy after it is started again.

Operational note:
- do not try to queue bot orders during OMS downtime
- let the bots recalculate after restart instead of trying to send stale pre-restart intent decisions

## Market Data Restart

Use when:
- only market-data code changed
- trade/quote stream is stale
- subscriptions look broken

Important limitation:
- the gateway restarts from new subscription events only
- dynamic subscriptions are safest when `strategy-engine` is restarted after the gateway

Procedure:
1. Stop strategy first so no new trade decisions are generated during the market-data interruption:

```bash
sudo systemctl stop project-mai-tai-strategy.service
```

2. Confirm OMS is quiet:
- no new intents arriving
- no in-flight order workflow you are waiting on

3. Restart market data:

```bash
sudo systemctl restart project-mai-tai-market-data.service
```

4. Wait until market data is healthy again.

5. Start strategy again so subscriptions and watchlists rebuild from a clean point:

```bash
sudo systemctl start project-mai-tai-strategy.service
```

Post-checks:
1. `/api/overview` shows `project-mai-tai-market-data.service` healthy.
2. `/api/scanner` shows live scanner data rather than only restored data.
3. `/api/scanner` or `/api/overview` shows active subscription symbols rebuilding.
4. `/api/bots` shows watchlists repopulating.
5. `/api/reconciliation` remains clean.

If scanner remains restored/idle after the restart:
- inspect `market-data.log` and `strategy.log`
- verify the strategy service was restarted after market data
- do not assume subscriptions are healthy until live rows return

## Full Stack Restart

Do not use this during market hours:
```bash
ops/systemd/restart_all.sh
```

Use full-stack restart only when:
- market is closed
- the account is flat
- no manual operator workflow is in progress

## Abort Conditions

Stop the restart procedure and reassess if any of these are true:
- open position exists and the bot/runtime state already looks inconsistent
- a broker cancel is pending
- a fill just arrived and positions are still updating
- reconciliation turns critical after a partial restart
- strategy runtime comes back empty while broker/account positions remain open

## Immediate Recovery After a Bad Restart

If a restart causes uncertainty:
1. Keep `project-mai-tai-strategy.service` stopped.
2. Leave `project-mai-tai-oms.service` running so broker/account sync continues.
3. Review `/api/orders`, `/api/positions`, and `/api/reconciliation`.
4. Inspect `strategy.log`, `oms.log`, and `market-data.log`.
5. Do not resume strategy until open positions and account positions are understood.

## Follow-Up Improvement Gap

This runbook exists because restart recovery is not fully automated yet.

The main missing capabilities are:
- strategy runtime rehydration from persisted open positions and pending order state
- safe intent buffering or replay across OMS restarts
- safer subscription re-seeding after isolated market-data restarts
- a tested restart-with-open-positions validation pass

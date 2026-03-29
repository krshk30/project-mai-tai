# Session Handoff - 2026-03-29

This file is the primary handoff for a new agent picking up `project-mai-tai` after the March 28-29, 2026 build and deployment session.

## Current Outcome

`project-mai-tai` is no longer just a scaffold. It is a running parallel platform on the VPS with:

- a live control plane at `https://project-mai-tai.live`
- FastAPI control plane, market-data gateway, strategy engine, OMS/risk, and reconciler services
- Postgres and Redis-backed runtime state
- Alpaca paper execution enabled
- four active strategies seeded:
  - `macd_30s`
  - `macd_1m`
  - `tos`
  - `runner`
- legacy app still running separately and intentionally untouched

Operational runtime on the VPS was last deployed from commit:

- `79edfbe` - `Tighten catalyst-driven confirmation logic`

This handoff document may be committed after that deployment commit and is intended as durable context, not a runtime code change.

## Repos And Paths

- New repo: `/Users/velkris/src/project-mai-tai`
- Legacy repo: `/Users/velkris/src/momentum-stock-trader`
- GitHub repo: `git@github.com:krshk30/project-mai-tai.git`

## Live Environment

- Domain: `project-mai-tai.live`
- VPS IP: `104.236.43.107`
- VPS SSH user: `trader`
- Control plane internal bind: `127.0.0.1:8100`
- Public edge: `Nginx + HTTPS + basic auth`

Important: do not write secrets into repo docs or commit them.

Secrets and live runtime configuration are on the VPS in:

- `/etc/project-mai-tai/project-mai-tai.env`

Do not echo or paste values from that file into Git, docs, or chat.

## What Was Built This Session

### 1. New Production-Oriented Repo

Created a separate repo instead of refactoring the legacy app in place:

- `project-mai-tai`

The new platform preserves strategy logic but replaces the runtime shell with:

- durable state in Postgres
- Redis Streams internal event bus
- service separation
- restart-safe execution flow

### 2. Core Runtime Services

Implemented and wired:

- `control-plane`
- `market-data-gateway`
- `strategy-engine`
- `oms-risk`
- `reconciler`

Primary package:

- `/Users/velkris/src/project-mai-tai/src/project_mai_tai`

### 3. Preserved Strategy Logic

Ported and kept deterministic strategy logic for:

- scanner alerts
- momentum confirmed
- 30s MACD
- 1m MACD
- TOS bot
- Runner
- bar building
- indicators
- entry logic
- exit logic
- position tracking

News Bot was intentionally excluded from the new platform.

### 4. Market Data And Warmup

Implemented:

- Massive/Polygon snapshot polling
- Massive WebSocket streaming
- historical bar warmup for `30s`, `1m`, and `5m`

The new runtime keeps scanning snapshots every `30s` whether or not confirmed names exist.

### 5. OMS And Execution

Built:

- broker abstraction
- simulated adapter first
- Alpaca paper adapter for real paper execution
- durable intent/order/fill/position persistence
- shared-account attribution support

Paper-account layout now in use:

- `macd_30s` -> dedicated Alpaca paper account
- `macd_1m` -> dedicated Alpaca paper account
- `tos` + `runner` -> shared Alpaca paper account

This shared-account model is intentional because Schwab live will use one shared account later.

### 6. Control Plane And UI

Control plane now includes:

- compact main operator view
- scanner workspace page
- dedicated bot pages for `30s`, `1m`, `tos`, and `runner`
- shadow comparison against legacy
- reconciliation and incident views
- TOS parity section

Layout direction requested by user and now implemented:

- main page favors trading highlights
- system health is compact and not page-dominating
- scanner page has left-side overview and multi-panel scanner layout
- bot pages follow the same workstation-style pattern

### 7. ET Time Display

User-facing timestamps were updated to ET across the UI and JSON responses.

Internal storage remains UTC where appropriate.

### 8. Morning Flowchart

Created and saved:

- `/Users/velkris/src/project-mai-tai/docs/morning-runtime-flow.mmd`
- `/Users/velkris/src/project-mai-tai/docs/morning-runtime-flow.pdf`

The flowchart was corrected to make clear that snapshot scanning continues every `30s` even when no confirmed names exist.

### 9. TOS 1m Parity Layer

Added a TOS parity panel for:

- `macd_1m`
- `tos`

Important:

- this does not fetch indicator values from thinkorswim
- it shows Mai Tai’s closed `1m` values in a TOS-aligned configuration for manual comparison

### 10. Active Market Verification Tracker

Saved active-session checks here:

- `/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md`

This is the working checklist for the next live paper session.

### 11. News/Catalyst Logic Rebuild

This is the most recent strategy-safety change and likely the most important context for the next agent.

Status:

- implemented
- tested
- pushed
- deployed

Key files:

- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/catalyst.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/momentum_confirmed.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/services/control_plane.py`
- `/Users/velkris/src/project-mai-tai/tests/unit/test_catalyst_engine.py`

What changed:

- news window begins at previous market close, `4:00 PM ET`
- weekend handling is included
  - Monday morning looks back to Friday `4:00 PM ET`
- generic price-action roundups do not count as real catalysts
- `PATH_A_NEWS` now requires `path_a_eligible`
- scanner UI now shows:
  - confidence
  - article count
  - real-catalyst article count
  - freshness
  - reason
  - roundup/informational/PATH A state

Design intent:

- news should help entry timing only when it is a fresh, real catalyst
- news should not be trusted just because a headline sounds positive

## Current Live Status

Last explicitly verified:

- control plane health endpoint was healthy
- all four core services relevant to this work were active
- strategy engine restarted cleanly after catalyst deployment
- OMS adapter is `alpaca_paper`
- scanner API was reachable
- no confirmed names were present at that moment

Because the market was inactive at the time of the last check, a blank confirmed table was expected.

## What The User Explicitly Wants Preserved

- keep the legacy app running separately until comfortable with cutover
- preserve strategy and core functionality, not rewrite them away
- use GitHub as the source of deployment truth
- keep UI closer to legacy for scanner/bot workflows
- keep timestamps in ET
- use recent news starting from previous market close
- make news safer and less misleading

## Important Files For A New Agent

Read these first:

- `/Users/velkris/src/project-mai-tai/README.md`
- `/Users/velkris/src/project-mai-tai/docs/architecture.md`
- `/Users/velkris/src/project-mai-tai/docs/implementation-roadmap.md`
- `/Users/velkris/src/project-mai-tai/docs/vps-deployment.md`
- `/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/services/control_plane.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/services/strategy_engine_app.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/catalyst.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/momentum_confirmed.py`
- `/Users/velkris/src/project-mai-tai/src/project_mai_tai/strategy_core/runner.py`

## Safe Local Verification Commands

Run from:

- `/Users/velkris/src/project-mai-tai`

Commands:

- `.venv/bin/python -m ruff check src tests`
- `.venv/bin/python -m pytest tests/unit`
- `.venv/bin/python -m compileall src/project_mai_tai`

The local dev environment is already set up with Python `3.12` and `.venv`.

## Git And Deploy Workflow

Preferred flow:

1. commit locally in `project-mai-tai`
2. push to GitHub `main`
3. deploy on VPS by pulling from GitHub
4. restart only the services you changed when possible

Typical VPS deploy command shape:

```bash
ssh -tt trader@104.236.43.107 'cd /home/trader/project-mai-tai && git pull --ff-only && sudo systemctl restart <service-names>'
```

Relevant services:

- `project-mai-tai-control.service`
- `project-mai-tai-market-data.service`
- `project-mai-tai-strategy.service`
- `project-mai-tai-oms.service`
- `project-mai-tai-reconciler.service`

Full target:

- `project-mai-tai.target`

Health check:

```bash
ssh -tt trader@104.236.43.107 'curl -fsS http://127.0.0.1:8100/health'
```

## Things A New Agent Should Not Do Casually

- do not disturb the legacy app on port `8000`
- do not reboot the VPS casually before a paper session
- do not commit secrets
- do not remove Alpaca paper wiring unless the user explicitly asks
- do not import legacy data unless the user explicitly asks

The user explicitly said they do not need legacy data imported into Mai Tai.

## Open Work / Next Best Steps

### Highest Priority

Use the next active market session to work through:

- `/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md`

Especially:

- first live scanner -> watchlist -> subscription cycle
- first live `intent -> accepted -> filled` flow
- shared-account attribution for `tos` and `runner`
- WebSocket stability under active subscriptions
- reconciliation sanity during live paper flow
- end-of-day account comparison

### Good Follow-Up Improvements

- add tolerance coloring for TOS parity values
- add broker account summary blocks to bot pages for closer legacy parity
- continue dashboard usability polish if the user requests it
- prepare Schwab live adapter work only after Alpaca paper behavior is stable

## If A New Agent Needs To Explain The News Logic Simply

Use this summary:

- Runner is not buying on news alone.
- News now only helps earlier entry when it qualifies a strict `PATH_A_NEWS`.
- A headline that merely describes price action no longer counts as bullish catalyst news.
- News is now tied to the current session window starting at the previous `4:00 PM ET`.

## Session Summary In One Line

This session turned `project-mai-tai` from a production-oriented rebuild into a running, paper-trading, VPS-deployed parallel platform with legacy-style dashboards, ET-facing operator UX, TOS parity support, and a much safer catalyst/news path for tomorrow’s paper session.

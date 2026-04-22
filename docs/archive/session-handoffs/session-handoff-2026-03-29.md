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

## Continuation Update - Later 2026-03-29

This section captures the follow-up work completed after the original handoff.

### Access State

Access is now working from the current Windows machine for both:

- VPS SSH
- GitHub SSH

Working VPS target:

- `trader@104.236.43.107`

Local repo is now configured to use the GitHub SSH remote:

- `git@github.com:krshk30/project-mai-tai.git`

### Branch And Commit State

The follow-up work was committed and pushed on:

- branch: `codex/trading-followups`
- commit: `ccc365c` - `Add broker follow-ups and live restart runbooks`

This branch includes:

- scanner blacklist work
- Alpaca cancel-intent work
- Schwab adapter/readiness work
- restart helper scripts and live restart docs
- associated test coverage

### New Operational Docs And Scripts

Added:

- `/Users/velkris/src/project-mai-tai/docs/live-market-restart-runbook.md`
- `/Users/velkris/src/project-mai-tai/docs/schwab-onboarding.md`
- `/Users/velkris/src/project-mai-tai/ops/systemd/live_helpers.sh`
- `/Users/velkris/src/project-mai-tai/ops/systemd/restart_control_live.sh`
- `/Users/velkris/src/project-mai-tai/ops/systemd/restart_reconciler_live.sh`
- `/Users/velkris/src/project-mai-tai/ops/systemd/restart_strategy_live.sh`
- `/Users/velkris/src/project-mai-tai/ops/systemd/restart_oms_live.sh`
- `/Users/velkris/src/project-mai-tai/ops/systemd/restart_market_data_live.sh`

These scripts are intended to be used instead of ad hoc restart sequences during live operations.

### Real VPS Restart Verification Was Performed

Off-hours verification was run on the real VPS while the account was flat.

Verified successfully:

- control-plane restart helper
- reconciler restart helper
- coordinated `strategy -> oms -> strategy`
- coordinated `strategy -> market-data -> strategy`

Observed during verification:

- services restarted successfully under `systemd`
- final settled health returned `healthy`
- immediately after coordinated restarts, `/health` could briefly show `degraded`
- that short degraded window was caused by heartbeat timing/staleness, not by a persistent service failure

Important detail:

- after market-data restart, control-plane briefly still showed the old market-data heartbeat as `stopping`
- direct Redis inspection confirmed fresh healthy market-data heartbeats were already being published again
- a later settled `/health` check returned fully healthy

Practical meaning for the next agent:

- do not panic if `/health` is briefly degraded right after a coordinated restart
- wait for fresh heartbeats before concluding the restart failed

### Flat-State Verification Context

At the time of the VPS restart verification:

- `pending_intents = 0`
- `open_virtual_positions = 0`
- `open_account_positions = 0`
- reconciliation findings = `0`

This proves off-hours restart behavior, but it does **not** replace the still-needed active-session validation with real open positions.

### Important VPS Checkout Note

Before GitHub SSH/push access was fully set up from this machine, the restart helper files were copied directly into the VPS checkout to enable testing.

That means the VPS checkout may contain local modified or untracked files corresponding to the same work now pushed on `codex/trading-followups`.

Before any future deploy or branch switch on the VPS, the next agent should first run:

```bash
cd /home/trader/project-mai-tai
git status --short
git fetch origin
```

Do not assume the VPS checkout is a perfectly clean fast-forward candidate until that is checked.

## What The User Explicitly Wants Preserved

- keep the legacy app running separately until comfortable with cutover
- preserve strategy and core functionality, not rewrite them away
- use GitHub as the source of deployment truth
- keep UI closer to legacy for scanner/bot workflows
- keep timestamps in ET
- use recent news starting from previous market close
- make news safer and less misleading

## User-Requested Validation Themes

These are not random nice-to-haves. The user explicitly asked about these repeatedly during the session, so the next agent should treat them as standing expectations.

### 1. Legacy UI Parity

The user asked the agent to review the legacy screens directly and make sure Mai Tai had matching operational surfaces for:

- scanner dashboard
- `30s` bot page
- `1m` bot page
- `tos` bot page
- `runner` bot page

What was learned:

- early Mai Tai versions were missing several legacy-style surfaces
- missing items included multiple scanners, alerts, trade-log style panels, and dedicated bot screens
- the user wants Mai Tai to feel familiar and usable as a primary operator interface, not just technically correct

What was done:

- scanner workspace page added
- dedicated bot pages added
- top-level control plane shortened and made more trading-focused
- scanner page redesigned toward the legacy workstation layout
- bot pages redesigned to follow that style

What still matters for the next agent:

- continue comparing Mai Tai screens directly against legacy if parity questions come up
- prefer matching user workflow and operator visibility over abstract UI cleanliness

### 2. Scanner Page Expectations

The user explicitly wanted the scanner page to look and feel closer to the legacy multi-panel workstation, especially:

- left-side overview rail
- multiple scanners and alerts on one dedicated page
- more dense trading-oriented layout
- momentum confirmed table with the same important columns

What was learned:

- structural parity matters more to the user than a generic dashboard aesthetic
- the user notices missing columns and missing scanner categories quickly

What was done:

- scanner workspace restructured with left rail and multi-panel deck
- added `Momentum Confirmed`, `5 Pillars`, `Top Gainers`, `Momentum Alerts`, and `Top Gainer Changes`
- added missing confirmed columns including catalyst/news-related fields
- added restored non-empty confirmed snapshot behavior so the table does not go blank after restart/off-hours

What still matters for the next agent:

- if the user says “legacy has X on scanner and Mai Tai does not,” verify directly against legacy before answering
- treat scanner UX parity as a product requirement, not a cosmetic preference

### 3. Bot Page Expectations

The user wanted the same style improvement applied to all bots.

What was learned:

- the user uses bot pages as primary operational views
- compact left-side overview plus multi-panel workspace is preferred
- misleading execution status labels are not acceptable

What was done:

- all bot pages now share the same workstation-style layout
- fixed misleading legacy-shadow execution labels so Mai Tai’s own bot wiring is shown correctly

What still matters for the next agent:

- if bot page parity is questioned again, compare against legacy screen by screen
- broker account summary blocks are still a good next parity improvement

### 4. Timezone And Labeling Expectations

The user explicitly asked that Mai Tai stop showing `UTC` and present ET consistently.

What was learned:

- user-facing time labels must be ET everywhere
- wording and labels matter a lot for trust
- the main control-plane header needed to be simpler and more operator-friendly

What was done:

- ET display normalization across UI and APIs
- simplified control-plane wording
- compact system dock moved to the top

What still matters for the next agent:

- if adding new UI or API fields, default user-facing timestamps to ET
- avoid reintroducing verbose or academic UI wording

### 5. “Healthy” Must Be Explainable

The user explicitly pushed back on vague health labels like:

- `Platform HEALTHY`
- long environment/provider strings without basis

What was learned:

- health must be grounded in visible checks
- compact status summaries are preferred over long prose

What was done:

- top health card simplified
- health view tied to service health, database, Redis, and incident state

What still matters for the next agent:

- any future health/status text should answer “healthy based on what?”

### 6. Flowchart And Runtime Understanding

The user asked for a PDF flowchart of the daily startup/runtime loop and then questioned whether scanning stops when no confirmed names exist.

What was learned:

- the user wants operational flow documentation, not just code
- the flowchart must make continuous snapshot scanning explicit

What was done:

- morning flowchart and PDF created
- corrected to show full-market snapshot scanning continues every `30s`

What still matters for the next agent:

- if runtime behavior changes, update the flowchart too

### 7. News Logic Trustworthiness

The user explicitly said they were not confident in the bullish/bearish news logic and observed that “bullish” names had not behaved bullish.

What was learned:

- the user understands news is only part of the flow, especially for runner
- the user still wants that component tightened because it affects confirmation and earlier-entry thresholds
- the user specifically asked to use recent news starting from previous market close, e.g. “yesterday 4 PM onwards”

What was done:

- stricter catalyst model
- previous close `4:00 PM ET` session window
- weekend handling
- no price-action-only bullish classification
- strict `PATH_A_NEWS` gating
- scanner UI now shows why a catalyst is trusted or not

What still matters for the next agent:

- if the user questions news again, verify current runtime behavior in Mai Tai, not just static code
- the next active session should confirm that catalyst data is being populated as expected for live names

### 8. TOS Comparison Expectations

The user wants live trading to happen in thinkorswim and asked whether comparing Mai Tai calculations to TOS is the right idea.

What was learned:

- `1m` parity is the practical comparison target
- `30s` exact TOS parity is not realistic because TOS does not expose `30s` aggregation in the relevant way

What was done:

- added a TOS parity panel for `1m`-based runtimes

What still matters for the next agent:

- parity panel currently shows Mai Tai’s own values in TOS-aligned settings
- it does not ingest TOS indicator values automatically
- tolerance highlighting is still a good next improvement

### 9. Active-Market Validation Matters More Than Quiet-Time Validation

The user repeatedly wanted validation of real functionality, not just code structure.

What was learned:

- many important checks can only be proven during an active trading session
- a quiet weekend dashboard being green is necessary but not sufficient

What was done:

- active-session todo tracker created

What still matters for the next agent:

- if the next session is during market hours, prioritize live functional validation before cosmetic work

## Validation Requests Already Asked By The User

The next agent should assume the user cares about all of the following, because they already asked for them in this session:

- compare Mai Tai screens directly against legacy
- verify missing scanners/alerts/trade-log-style sections and add them
- verify momentum confirmed table columns against legacy
- verify ET display consistency
- verify runtime health claims are meaningful
- verify morning runtime flow and scanning loop behavior
- verify TOS comparison approach
- review and tighten news logic
- carry forward all active-session validations into repo docs so they are not lost

If any of those topics come up again, the next agent should not start from zero.

## Known Learnings From The Session

- User preference strongly favors operator familiarity over abstract redesign.
- Compact top-level status is better than long explanatory dashboard prose.
- Empty weekend/off-hours tables can still be correct, but the UI should explain why.
- Restored scanner snapshots reduce operator anxiety when live runtime is quiet.
- Shared-account attribution is a structural requirement because Schwab live will use one real account.
- News should be an input to timing, not a vague trust signal.
- “Healthy” without basis undermines confidence.
- GitHub is the source of deployment truth; do not rely on local-only deploy habits.

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

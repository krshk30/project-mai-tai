# Chat Summary - 2026-03-29

This is a concise summary of the March 28-29, 2026 working session for `project-mai-tai`.

For the full operational handoff, see:

- [session-handoff-2026-03-29.md](/Users/velkris/src/project-mai-tai/docs/session-handoff-2026-03-29.md)
- [active-market-verification-todo.md](/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md)

## Goal

Build a production-oriented parallel replacement for the legacy `momentum-stock-trader` platform without modifying the legacy repo, and run both platforms side by side on the same VPS until cutover confidence is high.

## Major Decisions

- Start a completely new repo: `project-mai-tai`
- Keep the stack Python-first
- Use:
  - FastAPI control plane
  - Redis Streams for internal event flow
  - Postgres for durable state
  - `systemd` services on the VPS
  - Nginx + HTTPS + basic auth for the public dashboard
- Preserve strategy behavior from the legacy platform instead of rewriting it
- Exclude News Bot from the new platform
- Use Alpaca paper first
- Plan for Charles Schwab live later
- Support shared-account attribution from day one because Schwab live will use one shared real account

## Infrastructure And Deployment

- New repo created at `/Users/velkris/src/project-mai-tai`
- GitHub repo created and pushed: [krshk30/project-mai-tai](https://github.com/krshk30/project-mai-tai)
- Domain configured: [project-mai-tai.live](https://project-mai-tai.live)
- VPS stack brought up with:
  - Postgres
  - Redis
  - Nginx
  - systemd units for all Mai Tai services
- GitHub is now the deployment source of truth

## Runtime Built

The new platform now includes:

- `control-plane`
- `market-data-gateway`
- `strategy-engine`
- `oms-risk`
- `reconciler`

And preserves strategy/runtime logic for:

- scanner pipeline
- 30s MACD bot
- 1m MACD bot
- TOS bot
- Runner bot
- bar building
- indicators
- entries/exits
- position tracking

## Execution State

Mai Tai is running on the VPS in parallel with the legacy app.

Current operating mode:

- Alpaca paper execution enabled
- `macd_30s` -> dedicated paper account
- `macd_1m` -> dedicated paper account
- `tos` + `runner` -> shared paper account

The legacy app was left untouched.

## Dashboard And UX Work

The control plane and bot/scanner pages were iteratively improved based on direct user feedback.

Key UI changes:

- Main control plane simplified and made more trading-focused
- System health compacted into a small top dock
- Scanner page redesigned into a workstation-style layout
- Dedicated bot pages created for:
  - `30s`
  - `1m`
  - `tos`
  - `runner`
- Legacy-style sections added where missing:
  - multiple scanners
  - alerts
  - trade-log style panels
  - confirmed candidate tables

Important user preference learned:

- parity with the legacy operational workflow matters more than a generic “modern dashboard” aesthetic

## Timezone And Operator Clarity

- User-facing timestamps were changed from UTC to ET
- Health/status wording was simplified because vague labels like “HEALTHY” without explanation reduced trust
- Main page layout was shortened so trading-relevant information is visible first

## Flowchart And Operating Loop

Created:

- `/Users/velkris/src/project-mai-tai/docs/morning-runtime-flow.mmd`
- `/Users/velkris/src/project-mai-tai/docs/morning-runtime-flow.pdf`

Important clarification captured:

- Mai Tai should continue scanning full-market snapshots every `30s` even when there are no confirmed names

## TOS Parity

Added a TOS parity layer for:

- `macd_1m`
- `tos`

This does not ingest TOS values automatically. It exposes Mai Tai’s own `1m` EMA/MACD/VWAP values in a TOS-aligned format so the user can compare them manually in thinkorswim.

## News/Catalyst Review And Rebuild

The legacy news logic was reviewed because the user observed that many names tagged bullish did not behave bullish.

Main finding:

- legacy logic could over-trust weak or generic news, especially price-action roundups

Mai Tai was then tightened so that:

- news is evaluated from the previous market close window starting at `4:00 PM ET`
- weekend handling is included
- generic roundup headlines do not count as real catalysts
- `PATH_A_NEWS` requires strict eligibility, not just a bullish label
- scanner UI shows richer catalyst context:
  - confidence
  - article count
  - real-catalyst article count
  - freshness
  - reason
  - roundup/informational/PATH A state

Key intent preserved:

- news is not supposed to be a stand-alone trade trigger
- it is supposed to help with earlier entry only when the catalyst is fresh and real

## Active-Market Validation

Because much of the platform was built over a weekend, a dedicated live-session verification tracker was created:

- `/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md`

Main live checks still needed during an active trading session:

- first real `top_confirmed -> watchlist -> subscription` cycle
- first `intent -> accepted -> filled` paper order flow
- shared-account attribution check for `tos` and `runner`
- WebSocket stability during active subscriptions
- restart safety with open paper positions
- reconciliation behavior during real order flow

## Current Status At End Of Session

- Repo is pushed to GitHub
- VPS deployment is live
- Control plane is reachable at [project-mai-tai.live](https://project-mai-tai.live)
- Services are running
- OMS is configured for `alpaca_paper`
- News/catalyst tightening is deployed
- Active-session validations are documented but not all yet exercised under live market conditions

## Best Next Step

Use the next active trading session to execute the checklist in:

- [active-market-verification-todo.md](/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md)

If a new agent picks this up, they should start with:

- [session-handoff-2026-03-29.md](/Users/velkris/src/project-mai-tai/docs/session-handoff-2026-03-29.md)
- [active-market-verification-todo.md](/Users/velkris/src/project-mai-tai/docs/active-market-verification-todo.md)
- this file

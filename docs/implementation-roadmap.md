# Implementation Roadmap

## Phase 0: Bootstrap

Deliverables:
- repo scaffold
- Python project metadata
- architecture docs
- VPS deployment docs
- initial migration contract

Acceptance:
- repository exists locally and on GitHub
- legacy repo remains untouched

## Phase 1: Strategy Preservation Contract

Deliverables:
- source module mapping from legacy repo
- replay dataset plan
- parity acceptance criteria for 30s, 1m, TOS, Runner

Acceptance:
- each strategy has a documented behavior contract
- shadow comparison requirements are explicit

## Phase 2: Common Foundations

Deliverables:
- settings model
- logging conventions
- typed domain events
- Postgres schema baseline
- Alembic migrations
- Redis stream naming conventions

Acceptance:
- local services can boot against empty infrastructure
- schema can be created from scratch

## Phase 3: Market-Data Gateway

Deliverables:
- Massive/Polygon adapter
- normalized event contracts
- stream publishers for trade, quote, bar-close, and snapshot events

Acceptance:
- gateway can run without legacy interference
- event freshness and heartbeat tracking are visible

## Phase 4: Strategy Engine

Deliverables:
- strategy-core library
- legacy logic ported into pure modules
- intent emission instead of direct execution
- shadow-mode comparison hooks

Acceptance:
- strategy outputs can be replay-tested
- no broker dependency in strategy code

## Phase 5: OMS and Risk

Deliverables:
- broker abstraction
- Alpaca paper adapter
- idempotent intent-to-order flow
- order/fill persistence
- virtual positions
- restart recovery

Acceptance:
- restart does not lose open-order or open-position state
- OMS is sole writer of order/position truth

## Phase 6: Control Plane

Deliverables:
- FastAPI operator API
- server-rendered HTML dashboard
- health, incident, strategy, order, and reconciliation views

Acceptance:
- dashboard reaches parity with legacy visibility
- shadow-vs-legacy divergence is visible

## Phase 7: Reconciliation

Deliverables:
- scheduled reconcile runs
- incident recording
- broker-vs-OMS mismatch views

Acceptance:
- mismatches are detected without direct tracker mutation

## Phase 8: VPS Deployment

Deliverables:
- Postgres install bootstrap
- Redis install bootstrap
- systemd units
- log locations
- env file locations
- Nginx site config for `project-mai-tai.live`
- DNS checklist for `project-mai-tai.live` and `www.project-mai-tai.live`
- Certbot issuance steps for the VPS edge

Acceptance:
- new platform can run beside legacy on the same VPS
- no port collisions
- no shared state paths

## Phase 9: Shadow Mode

Deliverables:
- compare against both legacy API outputs and legacy artifacts
- daily divergence reports

Acceptance:
- no unexplained high-severity divergences for agreed burn-in period

## Phase 10: Alpaca Paper Rollout

Deliverables:
- account mapping:
  - 30s dedicated
  - 1m dedicated
  - TOS + Runner shared
- live paper execution through OMS

Acceptance:
- stable paper execution
- clean reconciliation
- restart-safe recovery

## Phase 11: Schwab Readiness

Deliverables:
- Schwab broker adapter
- shared live-account support
- onboarding and auth flow docs

Acceptance:
- one Schwab account can host all strategies with correct attribution

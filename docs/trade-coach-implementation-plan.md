# Trade Coach Implementation Plan

## Scope

This plan is for the first Mai Tai AI trade coach foundation.

The first implementation is intentionally limited to:

- `Schwab 30 Sec Bot` (`macd_30s`)
- `Polygon 30 Sec Bot` (`polygon_30s`)
- post-trade review first
- no live trade blocking
- no direct order submission

## Design Constraints

- Reuse the existing completed-trade reconstruction logic instead of rebuilding
  flat-to-flat pairing in a second place.
- Treat fills as the authoritative source for completed trade cycles and only
  fall back to filled orders when fills do not reconstruct the cycle.
- Keep coach reviews separated by at least:
  - `strategy_code`
  - `broker_account_name`
  - `symbol`
  - flat-to-flat cycle key
- Mirror the existing `news_ai_*` configuration shape.
- Use the OpenAI Responses API with strict structured output handling for
  persisted review records.
- Keep the service entrypoint aligned with existing `mai-tai-*` naming.
- Do not add any AI network call to the latency-sensitive live execution path in
  the first pass.

## Implementation Order

### Phase 1: Shared Episode Foundation

- [x] Extract completed-trade reconstruction from the control plane into a
  shared module.
- [x] Key cycles by `strategy_code + broker_account_name + symbol + cycle`.
- [x] Update the control plane to use the shared module.
- [x] Add tests for:
  - fills-first authority
  - filled-order fallback
  - cycle separation across strategy/account pairs

### Phase 2: Trade Coach Data Model

- [x] Add `trade_coach_*` settings to `settings.py`.
- [x] Add `ai_trade_reviews` SQLAlchemy model.
- [x] Add Alembic migration for `ai_trade_reviews`.
- [x] Add Pydantic models for:
  - completed trade cycle
  - trade episode
  - review payload

### Phase 3: Coach Service

- [x] Add `src/project_mai_tai/ai_trade_coach/` package.
- [x] Add repository layer that builds DB-backed trade episodes on top of the
  shared cycle reconstruction.
- [x] Add Responses API client with strict structured output parsing.
- [x] Add service layer that:
  - finds recent completed cycles
  - skips already-reviewed cycle keys
  - requests a coach review
  - persists the review

### Phase 4: Service Wiring

- [x] Add `src/project_mai_tai/services/trade_coach_app.py`.
- [x] Add `src/project_mai_tai/services/trade_coach.py` with `run()`.
- [x] Add `services/trade-coach/main.py`.
- [x] Add `mai-tai-trade-coach` script to `pyproject.toml`.

### Phase 5: Control Plane Surfacing

- [x] Load recent coach reviews in the control plane repository layer.
- [x] Attach per-bot recent coach reviews in `/api/bots`.
- [x] Add a minimal top-level API view for coach review visibility.

### Phase 6: Validation

- [x] Add unit tests for the shared episode module.
- [x] Add unit tests for the trade coach repository/service.
- [x] Run targeted pytest coverage on touched areas.
- [x] Run `py_compile` on touched Python modules.

## Explicit Non-Goals For This Pass

- No AI gating inside `strategy_engine_app.py`.
- No AI gating inside `oms/service.py`.
- No fine-tuning workflow.
- No multi-model ranking pipeline.
- No auto-feedback correction UI yet.

## Follow-Up After Foundation

After this foundation is stable, the next safe step is:

1. periodic post-trade reviews
2. operator visibility in the control plane
3. optional shadow live advice
4. only later, optional advisory OMS risk integration

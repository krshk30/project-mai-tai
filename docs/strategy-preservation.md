# Strategy Preservation

## Goal

Preserve the working strategy behavior from the legacy platform while removing the legacy runtime architecture.

## Legacy Source Mapping

Primary source modules:
- `src/scanner/momentum_alerts.py`
- `src/scanner/momentum_confirmed.py`
- `src/bot/bar_builder.py`
- `src/bot/indicators.py`
- `src/bot/entry.py`
- `src/bot/exit.py`
- `src/bot/position_tracker.py`
- `src/bot/tos_bot.py`
- `src/bot/runner_bot.py`

Excluded:
- `src/bot/news_bot.py`

## Preservation Rules

- keep thresholds and path logic behaviorally equivalent unless explicitly changed
- remove direct broker dependencies from strategy code
- remove filesystem persistence from strategy code
- express outputs as typed strategy events and intents
- prove parity through replay testing before paper rollout

## Comparison Strategy

Shadow-mode comparison must use both:
- legacy API outputs
- legacy persisted artifacts

Examples:
- confirmed candidates
- entry timestamps
- exit reasons
- scale events
- floor progression
- cooldown behavior

## Acceptance Criteria

Before paper rollout, each migrated strategy must show acceptable parity against legacy for:
- candidate selection
- intent generation
- position lifecycle decisions
- exit reasoning

Any intentional behavior changes must be documented explicitly.

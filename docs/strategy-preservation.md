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

## Entry And Exit Behavior

The codebase now has a durable runtime around the strategies, but the core decision logic is still preserved in `src/project_mai_tai/strategy_core/`.

Primary files:

- `entry.py`
- `exit.py`
- `position_tracker.py`
- `trading_config.py`

### Entry Logic

The MACD/TOS-style entry engine currently works in this order:

1. hard gates
   - trading hours
   - midday dead zone
   - optional EMA20 gate
   - cooldown after a recent exit
   - dedup on the same bar
   - existing-position block
2. path detection
   - `P1_MACD_CROSS`
   - `P2_VWAP_BREAKOUT`
   - `P3_MACD_SURGE`
3. optional confirmation wait
   - controlled by `confirm_bars`
4. quality score check
   - histogram growth
   - stochastic trend
   - VWAP alignment
   - volume threshold
   - MACD trend
   - EMA alignment

Important variants:

- the default MACD configuration uses confirmation bars and minimum score thresholds
- `make_1m_variant()` shortens the rhythm and reduces cooldown
- `make_tos_variant()` removes the confirmation wait, disables the EMA gate, and clears the dead-zone behavior

### Exit Logic

Exit behavior is shared between `exit.py` and `position_tracker.py`.

Position state tracks:

- current profit
- peak profit
- tier progression
- trailing floor percentage
- floor price
- completed scale levels

Current exit priority is:

1. floor breach
2. scale action
3. tier-specific bearish close
4. hard stop

Current scale levels:

- `FAST4`
- `PCT2`
- `PCT4_AFTER2`

Current close families:

- `STOCHK_TIER1`
- `MACD_BEAR_T1`
- `STOCHK_TIER2`
- `MACD_BEAR_T2`
- `MACD_BEAR_T3`
- `HARD_STOP`
- `FLOOR_BREACH`

### Runner Note

`runner.py` should be treated as a separate runtime family, not just a thin config variant of the MACD/TOS entry engine.

It uses its own watchlist ranking, timing rules, and exit handling, so parity review for Runner should be done against `runner.py` directly.

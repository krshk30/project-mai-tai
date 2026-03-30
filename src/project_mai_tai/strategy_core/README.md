# Strategy Core

This package holds the preserved trading and scanner logic that the new runtime is built around.

What lives here:

- scanner logic
  - `momentum_alerts.py`
  - `momentum_confirmed.py`
  - `five_pillars.py`
  - `top_gainers.py`
- entry/exit and indicator logic
  - `entry.py`
  - `exit.py`
  - `indicators.py`
  - `bar_builder.py`
- runner-specific behavior
  - `runner.py`
- shared models and config
  - `models.py`
  - `config.py`
  - `trading_config.py`
  - `time_utils.py`
  - `snapshot_utils.py`
  - `catalyst.py`
  - `position_tracker.py`

What does not belong here:

- broker HTTP calls
- database sessions
- Redis publishing/subscription glue
- FastAPI or HTML rendering

Practical rule:

- if the code answers "should this symbol qualify?" or "should this bot open/close/scale?", it probably belongs here
- if the code answers "how do we persist, publish, or display that decision?", it probably belongs somewhere else

The main orchestration layer that consumes this package is:

- `src/project_mai_tai/services/strategy_engine_app.py`

## Entry And Exit Quick Map

The current MACD/TOS-style entry and exit pipeline is split across:

- `trading_config.py`
  - strategy-level thresholds, confirm-bar requirements, cooldowns, stop-loss settings, and scale percentages
- `entry.py`
  - entry gating, path detection, pending confirmations, and quality scoring
- `position_tracker.py`
  - live position state, peak-profit tracking, floor calculation, and scale bookkeeping
- `exit.py`
  - floor-breach exits, scale actions, tier-specific closes, and hard-stop checks

High-level flow:

1. Entry checks hard gates such as trading hours, dead zone, cooldown, existing position, and optional EMA gate.
2. If gates pass, the engine checks one of the supported entry paths such as MACD cross, VWAP breakout, or MACD surge.
3. Depending on `confirm_bars`, the signal either fires immediately or waits for follow-through confirmation.
4. A quality score can further reject weak confirmations before a buy signal is emitted.
5. After entry, `PositionTracker` advances tiers and trailing floor behavior as profit improves.
6. Exit logic checks, in order, for floor breach, scale actions, tier-specific bearish conditions, and hard stops.

Important nuance:

- `runner.py` is not just another thin variant of `entry.py` and `exit.py`
- Runner keeps its own strategy runtime and should be treated as a separate behavior family

For the durable project-level explanation of preserved behavior, also see:

- `../../../docs/strategy-preservation.md`

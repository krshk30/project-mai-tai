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

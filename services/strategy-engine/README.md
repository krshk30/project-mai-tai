# Strategy Engine

Consumes normalized market events and turns them into scanner and bot decisions.

Responsibilities:

- scanner surfaces such as momentum alerts, momentum confirmed, five pillars, and top gainers
- watchlist management and market-data subscription requests
- bot runtimes for `macd_30s`, `macd_1m`, `tos`, and `runner`
- trade-intent emission for OMS
- strategy-state publication for the control plane

Implementation:

- wrapper: `services/strategy-engine/main.py`
- package code: `src/project_mai_tai/services/strategy_engine_app.py` and `src/project_mai_tai/strategy_core/`

This service decides what should happen next, but it does not submit broker orders itself.

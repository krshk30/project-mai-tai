# Strategy Core

Pure strategy modules ported from the legacy platform with replay-test parity requirements.

Active implementation package:
- `src/project_mai_tai/strategy_core/`

Initial ported modules:
- normalized snapshot/reference models
- bar building
- indicator math
- momentum alerts
- momentum confirmation and ranking
- entry engine
- exit engine
- position tracker

Next port targets:
- TOS-specific decision flow
- Runner-specific decision flow
- replay fixtures from legacy sessions

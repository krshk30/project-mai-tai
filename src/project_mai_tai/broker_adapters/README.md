# Broker Adapters

This package is the broker-facing edge of the platform.

Files:

- `protocols.py`
  - shared broker contract used by OMS
- `simulated.py`
  - local/dev adapter with deterministic execution reports
- `alpaca.py`
  - Alpaca paper implementation, including submit and cancel handling
- `schwab.py`
  - Schwab live implementation, including token refresh and token-store persistence

Rules:

- only `oms/` should call broker adapters
- strategy code should never import a concrete broker adapter directly
- adapter-specific request/response translation should stay here, not leak into OMS store logic

When adding another broker:

1. implement the protocol in `protocols.py`
2. keep broker HTTP/auth logic in a new adapter module
3. wire adapter selection in `src/project_mai_tai/oms/service.py`
4. document required env vars in `README.md` and `ops/env/project-mai-tai.env.example`
5. add adapter-focused unit tests under `tests/unit/`

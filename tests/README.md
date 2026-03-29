# Test Layout

The test tree is organized by the type of confidence each layer provides.

Subdirectories:

- `unit/`
  - fast tests for individual services, adapters, scanners, and helpers
- `integration/`
  - cross-service and persistence-path tests
- `replay/`
  - behavior-preservation tests that compare or replay important strategy/scanner flows

Shared files:

- `conftest.py`
  - shared fixtures and test wiring

Guidelines:

- put adapter, service, or helper regressions in `unit/`
- put multi-component flows in `integration/`
- put legacy-parity or recorded-market behavior checks in `replay/`

If a production bug crosses boundaries, prefer:

1. one narrow unit test for the failing component
2. one broader integration or replay test only if the bug depends on system interaction

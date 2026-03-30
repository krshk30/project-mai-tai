# Control Plane

FastAPI operator surface for Mai Tai.

Responsibilities:

- HTML dashboard views
- JSON APIs for health, scanner, bots, orders, fills, positions, and reconciliation
- service-heartbeat aggregation from Redis
- Postgres-backed execution and incident views
- optional shadow comparison against the legacy app
- operator actions such as scanner blacklist updates

Implementation:

- wrapper: `services/control-plane/main.py`
- package code: `src/project_mai_tai/services/control_plane.py`

This service is an operator surface, not a broker executor and not a strategy engine.

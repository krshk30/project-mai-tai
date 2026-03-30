# DB Package

This package owns the SQLAlchemy-side definition of Mai Tai's durable state.

Files:

- `base.py`
  - declarative base and metadata root
- `models.py`
  - runtime tables such as strategies, broker accounts, intents, orders, fills, positions, reconciliation findings, incidents, dashboard snapshots, and scanner blacklist entries
- `session.py`
  - engine and session-factory construction

Responsibility boundary:

- if the question is "what state do we store durably in Postgres?", start here
- if the question is "how does that state get created or updated during trading?", continue into `oms/`, `reconciliation/`, or `services/control_plane.py`

Schema history is not stored here. Migrations live in:

- `../../../sql/migrations/`

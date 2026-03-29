# SQL Layout

`sql/` contains schema-evolution assets for Postgres.

Current contents:

- `migrations/`
  - Alembic environment, migration template, and versioned revisions

Ownership boundary:

- table definitions live in `src/project_mai_tai/db/models.py`
- schema history and upgrade steps live under `sql/migrations/`

Use this directory when you need to:

- create a new Alembic revision
- inspect schema history
- understand how the runtime DB evolved over time

Main commands:

- `alembic upgrade head`
- `alembic downgrade -1`
- `alembic revision -m "describe-change"`

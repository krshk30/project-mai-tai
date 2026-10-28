"""L12 additive positive closure projection and indexed lookup; no source deletion."""
import sqlalchemy as sa
from alembic import op

revision = "20261022_l12_retirement"
down_revision = "20261023_atr_aggregate"
branch_labels = None
depends_on = None


_SHARED_INDEX_TABLES = {
    "ix_l12_dispatch_scope": "dashboard_snapshots",
    "ix_l12_order_dispatch_token": "broker_orders",
    "ix_l12_control_uncertainty": "bot_control_attempts",
}
# C1 2026-10-27 13:24: transactional build, writers stopped; both budgets180s.
# Other migrations retain their budgets. A failure still aborts installation.
_INDEX_BUDGET_SQL = "SET LOCAL statement_timeout = '180000ms'"
_INDEX_LOCK_BUDGET_SQL = "SET LOCAL lock_timeout = '180000ms'"
_OFFLINE_SAVE_SQL = (
    "SELECT set_config('l12.previous_index_statement_timeout', "
    "current_setting('statement_timeout'), true), "
    "set_config('l12.previous_index_lock_timeout', current_setting('lock_timeout'), true)"
)
_OFFLINE_RESTORE_SQL = (
    "SELECT set_config('statement_timeout', "
    "current_setting('l12.previous_index_statement_timeout'), true), "
    "set_config('lock_timeout', current_setting('l12.previous_index_lock_timeout'), true)"
)


def _create_shared_index(name, table_name, columns, **kwargs):
    if _SHARED_INDEX_TABLES.get(name) != table_name:
        raise ValueError("L12 index budget requires one exact shared index/table")
    context = op.get_context()
    if context.dialect.name != "postgresql":
        op.create_index(name, table_name, columns, **kwargs)
        return
    if context.as_sql:
        # Offline SQL remains transactional/fail-stop: an SQL error must abort
        # the transaction, restoring all SET LOCAL values before any new DDL.
        op.execute(_OFFLINE_SAVE_SQL)
        primary = None
        try:
            op.execute(_INDEX_BUDGET_SQL)
            op.execute(_INDEX_LOCK_BUDGET_SQL)
            op.create_index(name, table_name, columns, **kwargs)
        except BaseException as error:
            primary = error
            raise
        finally:
            try:
                op.execute(_OFFLINE_RESTORE_SQL)
            except BaseException as error:
                if primary is None:
                    raise
                primary.add_note("L12 offline index budget cleanup UNKNOWN: " + type(error).__name__)
        return
    connection = op.get_bind()
    previous = connection.exec_driver_sql("SHOW statement_timeout").scalar_one()
    previous_lock = connection.exec_driver_sql("SHOW lock_timeout").scalar_one()
    primary = None
    try:
        # A failed CREATE aborts its savepoint, not the connection needed to
        # restore the original budget. The outer migration still fails.
        with connection.begin_nested():
            connection.exec_driver_sql(_INDEX_BUDGET_SQL)
            connection.exec_driver_sql(_INDEX_LOCK_BUDGET_SQL)
            op.create_index(name, table_name, columns, **kwargs)
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            connection.execute(sa.text(
                "SELECT set_config('statement_timeout', :previous, true), "
                "set_config('lock_timeout', :previous_lock, true)"),
                {"previous": previous, "previous_lock": previous_lock})
        except BaseException as error:
            if primary is None:
                raise
            primary.add_note("L12 index budget cleanup UNKNOWN: " + type(error).__name__)


def upgrade():
    op.create_table("brokerdecides_dispatch_retirements",
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("dashboard_snapshots.id"), primary_key=True),
        sa.Column("proof_version", sa.Integer(), nullable=False),
        sa.Column("attempt_state", sa.Text(), nullable=False),
        sa.Column("token", sa.String(128), nullable=False),
        sa.Column("account_name", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("observed_ms", sa.BigInteger(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False))
    op.create_index("ix_brokerdecides_dispatch_retirements_token", "brokerdecides_dispatch_retirements", ["token"])
    op.create_table("brokerdecides_dispatch_evidence",
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("brokerdecides_dispatch_retirements.attempt_id"), primary_key=True),
        sa.Column("entity_kind", sa.String(24), primary_key=True),
        sa.Column("entity_id", sa.Uuid(), primary_key=True),
        sa.Column("original_state", sa.Text(), nullable=False))
    snapshots = sa.table("dashboard_snapshots", sa.column("payload", sa.JSON()))
    orders = sa.table("broker_orders", sa.column("payload", sa.JSON()))
    # Match the same SQLAlchemy JSON expressions used by the model/readers.
    _create_shared_index("ix_l12_dispatch_scope", "dashboard_snapshots", ["snapshot_type",
        snapshots.c.payload["kind"].as_string(), snapshots.c.payload["strategy_code"].as_string(),
        snapshots.c.payload["account_name"].as_string(), "created_at", "id"])
    _create_shared_index("ix_l12_order_dispatch_token", "broker_orders", [
        orders.c.payload["clearwait_dispatch_token"].as_string()])
    _create_shared_index("ix_l12_control_uncertainty", "bot_control_attempts", ["account_id", "symbol"],
        postgresql_where=sa.text("status IS NULL OR status NOT IN ('TERMINAL', 'PROVEN_NOT_SENT')"),
        sqlite_where=sa.text("status IS NULL OR status NOT IN ('TERMINAL', 'PROVEN_NOT_SENT')"))


def downgrade():
    # Rollback is feature OFF. Evidence must never disappear implicitly.
    raise RuntimeError("retain L12 closure evidence; reviewed explicit migration required")

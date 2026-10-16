"""Add the three-table live bot-control journal without enabling dispatch."""

import sqlalchemy as sa
from alembic import op

revision = "20261015_bot_control"
down_revision = "20261014_sec_country"
branch_labels = None
depends_on = None


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    active_fk = sa.ForeignKeyConstraint(
        ["active_command_id"],
        ["bot_control_commands.command_id"],
        name="fk_bot_control_state_active_command",
        ondelete="RESTRICT",
    )
    op.create_table(
        "bot_control_states",
        sa.Column("strategy_id", sa.Uuid(), sa.ForeignKey("strategies.id"), primary_key=True),
        sa.Column("execution_mode", sa.String(16), primary_key=True),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("desired_state", sa.String(8), nullable=False),
        sa.Column("active_command_id", sa.Uuid(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("execution_mode = 'live'", name="ck_bot_control_state_live"),
        sa.CheckConstraint(
            "generation >= 0 AND revision >= 0", name="ck_bot_control_state_version"
        ),
        sa.CheckConstraint("desired_state IN ('RUN', 'STOP')", name="ck_bot_control_state_desired"),
        *([active_fk] if sqlite else []),
    )
    op.create_table(
        "bot_control_commands",
        sa.Column("command_id", sa.Uuid(), primary_key=True),
        sa.Column("strategy_id", sa.Uuid(), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        *[
            sa.Column(name, sa.BigInteger(), nullable=False)
            for name in (
                "generation",
                "expected_generation",
                "revision",
            )
        ],
        sa.Column("action", sa.String(8), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("account_scope", sa.JSON(), nullable=False),
        sa.Column("account_scope_hash", sa.String(64), nullable=False),
        sa.Column("actor_reference", sa.String(128), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("phase", sa.String(40), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("inventory_census", sa.JSON(), nullable=False),
        sa.Column("executor_instance_id", sa.String(128), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)
            for name, nullable in (
                ("lease_until", True),
                ("accepted_at", False),
                ("updated_at", False),
                ("completed_at", True),
            )
        ],
        sa.ForeignKeyConstraint(
            ["strategy_id", "execution_mode"],
            ["bot_control_states.strategy_id", "bot_control_states.execution_mode"],
            name="fk_bot_control_command_scope",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "strategy_id", "execution_mode", "generation", name="uq_bot_control_command_generation"
        ),
        sa.CheckConstraint(
            "generation > 0 AND expected_generation >= 0 AND revision >= 0",
            name="ck_bot_control_command_version",
        ),
        sa.CheckConstraint("action IN ('STOP', 'ON')", name="ck_bot_control_command_action"),
    )
    if not sqlite:
        op.create_foreign_key(
            "fk_bot_control_state_active_command",
            "bot_control_states",
            "bot_control_commands",
            ["active_command_id"],
            ["command_id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "uq_bot_control_active_scope",
        "bot_control_commands",
        ["strategy_id", "execution_mode"],
        unique=True,
        postgresql_where=sa.text("active"),
        sqlite_where=sa.text("active"),
    )
    op.create_index(
        "ix_bot_control_commands_recovery", "bot_control_commands", ["active", "updated_at"]
    )
    op.create_table(
        "bot_control_attempts",
        sa.Column("attempt_id", sa.Uuid(), primary_key=True),
        sa.Column(
            "command_id",
            sa.Uuid(),
            sa.ForeignKey("bot_control_commands.command_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("strategy_id", sa.Uuid(), sa.ForeignKey("strategies.id"), nullable=False),
        sa.Column("execution_mode", sa.String(16), nullable=False),
        sa.Column("generation", sa.BigInteger(), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("broker_accounts.id"), nullable=False),
        sa.Column("symbol", sa.String(16), nullable=False),
        sa.Column("binding_kind", sa.String(24), nullable=False),
        sa.Column("binding_key", sa.String(256), nullable=False),
        *[
            sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=True)
            for name, target in (
                ("parent_intent_id", "trade_intents.id"),
                ("entry_order_id", "broker_orders.id"),
                ("managed_row_id", "oms_managed_positions.id"),
                ("target_order_id", "broker_orders.id"),
                ("existing_intent_id", "trade_intents.id"),
                ("existing_order_id", "broker_orders.id"),
            )
        ],
        sa.Column("sequence_no", sa.BigInteger(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("client_order_id", sa.String(128), nullable=True, unique=True),
        *[
            sa.Column(name, sa.Numeric(18, 8), nullable=True)
            for name in (
                "requested_quantity",
                "cumulative_entry_qty",
                "cumulative_exit_qty",
                "remaining_observed",
                "limit_price",
            )
        ],
        sa.Column("owner_evidence_revision", sa.BigInteger(), nullable=True),
        sa.Column("owner_evidence_hash", sa.String(64), nullable=True),
        sa.Column("session", sa.String(16), nullable=True),
        sa.Column("order_type", sa.String(24), nullable=True),
        sa.Column("time_in_force", sa.String(16), nullable=True),
        sa.Column("terminal_evidence", sa.JSON(), nullable=True),
        sa.Column("executor_instance_id", sa.String(128), nullable=True),
        sa.Column("lease_fence", sa.BigInteger(), nullable=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        *[
            sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)
            for name, nullable in (
                ("quote_at", True),
                ("prepared_at", False),
                ("dispatch_intent_at", True),
                ("actual_call_completed_at", True),
                ("last_authoritative_read_at", True),
                ("next_eligible_at", True),
            )
        ],
        sa.UniqueConstraint(
            "command_id",
            "account_id",
            "symbol",
            "binding_kind",
            "binding_key",
            "operation",
            "sequence_no",
            name="uq_bot_control_attempt_identity",
        ),
        sa.CheckConstraint(
            "generation > 0 AND sequence_no >= 0 AND revision >= 0",
            name="ck_bot_control_attempt_version",
        ),
        sa.CheckConstraint("execution_mode = 'live'", name="ck_bot_control_attempt_live"),
        sa.CheckConstraint(
            "operation IN ('CANCEL_ENTRY', 'CANCEL_PROTECTION', 'SUBMIT_EXIT', 'ADOPT_EXISTING_EXIT')",
            name="ck_bot_control_attempt_operation",
        ),
        sa.CheckConstraint(
            "status IN ('PREPARED', 'MAY_HAVE_SENT', 'WORKING', 'UNKNOWN', 'TERMINAL', 'PROVEN_NOT_SENT')",
            name="ck_bot_control_attempt_status",
        ),
    )
    unresolved = sa.text(
        "operation IN ('SUBMIT_EXIT', 'ADOPT_EXISTING_EXIT') AND status IN ('PREPARED', 'MAY_HAVE_SENT', 'WORKING', 'UNKNOWN')"
    )
    op.create_index(
        "uq_bot_control_unresolved_exit",
        "bot_control_attempts",
        ["strategy_id", "execution_mode", "account_id", "symbol"],
        unique=True,
        postgresql_where=unresolved,
        sqlite_where=unresolved,
    )
    op.create_index(
        "ix_bot_control_attempts_recovery", "bot_control_attempts", ["status", "next_eligible_at"]
    )


def downgrade() -> None:
    # Never erase a durable STOP or an uncertain wire identity to roll back code.
    connection = op.get_bind()
    for query in (
        "SELECT 1 FROM bot_control_states WHERE desired_state <> 'RUN' OR active_command_id IS NOT NULL LIMIT 1",
        "SELECT 1 FROM bot_control_commands LIMIT 1",
        "SELECT 1 FROM bot_control_attempts LIMIT 1",
    ):
        if connection.execute(sa.text(query)).first() is not None:
            raise RuntimeError("KILLSW1 downgrade refused: control history must be retained")
    op.drop_table("bot_control_attempts")
    if connection.dialect.name != "sqlite":
        op.drop_constraint(
            "fk_bot_control_state_active_command", "bot_control_states", type_="foreignkey"
        )
    op.drop_table("bot_control_commands")
    op.drop_table("bot_control_states")

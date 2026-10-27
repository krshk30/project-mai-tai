"""Initial, unapplied SIM-only schema snapshot; freeze once migration is applied.

Future model changes must use an explicit child migration, not alter this factory.
The FIRSTBAR model literal is Gibbs a36's explicit print-volume policy, not a
native fill claim or a replacement for the old VOLBODY model pairs.
"""

import sqlalchemy as sa


FIRSTBAR_MODEL = "MODELED_STOP_AT_OBSERVED_PRINT_VOLUME"
SIMULATION_TABLES = frozenset({"atr_simulation_inputs", "atr_simulation_consumers",
                               "atr_simulation_runs", "atr_simulation_rows"})


def simulation_metadata():
    metadata = sa.MetaData()
    sa.Table("atr_simulation_inputs", metadata,
        sa.Column("event_key", sa.String(64), primary_key=True),
        sa.Column("stream", sa.String(255), nullable=False),
        sa.Column("stream_id", sa.String(64), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("fields", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("stream", "stream_id", name="uq_atrsim_input_position"))
    sa.Table("atr_simulation_consumers", metadata,
        sa.Column("consumer_id", sa.String(128), primary_key=True),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        sa.Column("cursors", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("issues", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    sa.Table("atr_simulation_runs", metadata,
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("source_refs", sa.JSON(), nullable=False),
        sa.Column("coverage", sa.JSON(), nullable=False),
        sa.Column("result_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    rows = sa.Table("atr_simulation_rows", metadata,
        sa.Column("row_key", sa.String(64), primary_key=True),
        sa.Column("source_id", sa.String(128), sa.ForeignKey("atr_simulation_runs.source_id"), nullable=False),
        sa.Column("policy_version", sa.String(128), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("broker", sa.String(32), nullable=False),
        sa.Column("account_id", sa.String(128), nullable=False),
        sa.Column("strategy", sa.String(64), nullable=False),
        sa.Column("flip_id", sa.String(255), nullable=False),
        sa.Column("segment_id", sa.String(255), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("model", sa.String(64), nullable=False),
        sa.Column("first_fill_ts", sa.BigInteger(), nullable=True),
        sa.Column("fills", sa.JSON(), nullable=True),
        sa.Column("vwap", sa.Text(), nullable=True),
        sa.Column("filled_qty", sa.BigInteger(), nullable=True),
        sa.Column("remainder_cancel_reason", sa.Text(), nullable=True),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.CheckConstraint("mode IN ('LIVE', 'TODAY', 'VOLBODY', 'FIRSTBAR', 'FIRSTBAR_X3')", name="ck_atrsim_mode"),
        sa.CheckConstraint("model IN ('ACTUAL', 'SUPPLIED', 'QUEUE_AWARE', 'OPTIMISTIC', "
                           "'MODELED_STOP_AT_OBSERVED_PRINT_VOLUME')", name="ck_atrsim_model"),
        sa.CheckConstraint("(mode = 'LIVE' AND model = 'ACTUAL') OR (mode = 'TODAY' AND model = 'SUPPLIED') OR "
            "(mode = 'VOLBODY' AND model IN ('QUEUE_AWARE', 'OPTIMISTIC')) OR "
            "(mode IN ('FIRSTBAR', 'FIRSTBAR_X3') AND model = 'MODELED_STOP_AT_OBSERVED_PRINT_VOLUME')",
            name="ck_atrsim_mode_model"))
    sa.Index("ix_atrsim_source_day", rows.c.source_id, rows.c.day, rows.c.row_key)
    return metadata

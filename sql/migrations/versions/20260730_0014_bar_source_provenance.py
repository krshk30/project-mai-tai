"""strategy_bar_history: record each bar's PROVENANCE.

⭐⭐ WHY (2026-07-30). A v2 outage leaves a hole in the persisted series, and the REST warmup
repairs only the strategy's IN-MEMORY deque — it never writes the missing bars back. Live trading
recovers on restart; the DATABASE stays holed. Every DB-reading consumer then reads a discontinuous
series: the backtest/replay engine (this table IS its bar source), the trade recorder's
mfe/mae/n_bars and every what-if exit, and the backtest-vs-live parity study.

Measured that day: 27 gaps, 761 bars missing — an 85-minute hole on every watchlist symbol from the
outage, PLUS holes with the bot perfectly healthy (MF 13/11/31 min, CRWU 25 min, SNDG 3 min). The
feed drops bars on its own.

⛔⭐ WHY A PROVENANCE COLUMN IS THE PREREQUISITE FOR FILLING THEM. A backfilled bar is NOT
byte-identical to the live-built one — that is the bar-source defect the project already paid for
(Polygon vs Schwab bars agreed on only 54.2% of ATR flips). Filling a hole from REST without
recording where the bar came from would make the parity study silently compare two provenances
while looking perfectly clean. So: label first, fill second.

`source` values:
  'live'    — built by the strategy from the live feed. The truth for what the bot ACTUALLY SAW.
  'rest'    — fetched from Schwab REST to repair a hole. Correct prices, different provenance.

⛔ Existing rows are stamped 'live' because that is what they are: every row predating this
migration was written by the live bar path. Backfill has never run.

⛔ NON-VOLATILE server_default => Postgres 11+ adds the column WITHOUT rewriting the table. This
matters: strategy_bar_history is ~816 MB and the OMS is on the same 2-vCPU box.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260730_0014"
down_revision = "20260725_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "strategy_bar_history",
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'live'"),
        ),
    )
    # Partial index: studies filter for the EXCEPTIONS (non-live bars), which are rare. A full
    # index on a column that is ~100% one value would be dead weight on an 816 MB table.
    op.create_index(
        "ix_strategy_bar_history_source_not_live",
        "strategy_bar_history",
        ["strategy_code", "symbol", "bar_time"],
        unique=False,
        postgresql_where=sa.text("source <> 'live'"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_strategy_bar_history_source_not_live",
        table_name="strategy_bar_history",
    )
    op.drop_column("strategy_bar_history", "source")

"""Source repair controls for CTE graph compilation; no native plan evidence."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite

from project_mai_tai.brokerdecides1_retirement import (
    entity_state, table_entity_state, unresolved_jobs_query, unresolved_query,
    validated_retirement_ids,
)
from project_mai_tai.db.models import (
    BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Fill,
    OmsManagedPosition, Strategy, TradeIntent,
)


@pytest.mark.parametrize("kind,model", [
    ("order", BrokerOrder), ("intent", TradeIntent), ("event", BrokerOrderEvent),
    ("fill", Fill), ("managed", OmsManagedPosition), ("replacement", DashboardSnapshot),
    ("account", BrokerAccount), ("strategy", Strategy),
])
def test_core_preimage_has_identical_scalar_column_order_and_casts(kind, model):
    dialect = postgresql.dialect()
    scalar = str(select(entity_state(kind, model)).compile(dialect=dialect))
    core = str(select(table_entity_state(kind, model.__table__)).compile(dialect=dialect))
    assert core == scalar


@pytest.mark.parametrize("dialect", [postgresql.dialect(), sqlite.dialect()], ids=["pg", "sqlite"])
@pytest.mark.parametrize("context", ["ids", "global_selection", "jobs", "valid_count"])
def test_shared_graph_compiles_in_each_actual_consumer_context(dialect, context):
    names = ("live:schwab", "live:webull")
    if context == "ids":
        query = validated_retirement_ids()
    elif context == "global_selection":
        query = unresolved_query(names).order_by(DashboardSnapshot.created_at, DashboardSnapshot.id).limit(32)
    elif context == "jobs":
        query = unresolved_jobs_query(names).limit(4097)
    else:
        query = select(func.count()).select_from(validated_retirement_ids().subquery())
    sql = str(query.compile(dialect=dialect))
    assert "UNION ALL" in sql
    assert "LEFT OUTER JOIN" in sql
    if dialect.name == "postgresql":
        assert sql.count("AS MATERIALIZED") == 2

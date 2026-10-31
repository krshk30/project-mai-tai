"""Retained CHOW rows; omitted strategy/fills/positions/runtime remain CONTROLLED.

No provider, Redis, or native absence is inferred. This private source replay
executes the durable readers against copied rows and records exact predicates.
"""
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, BrokerOrderEvent, DashboardSnapshot, Strategy, TradeIntent

ACCOUNT_NAMES = ("live:schwab_1m_v2", "live:orb")
OPPORTUNITY = 1793374142931
OBSERVED = datetime(2026, 10, 30, 17, 15, tzinfo=UTC)
INPUTS = {
    "chow_episode_1030.json": "7f8663cde1757ef67f051bbfa799bf58cc0a93bec57905aab1ecd95748512881",
    "wbpa3_brokerdecides_1030.json": "ffaa2e9cb80cc989da4b069b89700c83c03d31f715025fcbe676da141c070b4e",
}


def decode_model(model, row):
    columns = {attr.key: attr.columns[0] for attr in inspect(model).column_attrs}
    values = {}
    for key, value in row.items():
        attr = "metadata_json" if key == "metadata" else key
        assert attr in columns
        kind = columns[attr].type.python_type
        if value is not None and kind is UUID:
            value = UUID(str(value))
        elif value is not None and kind is datetime:
            value = datetime.fromisoformat(value)
            assert value.tzinfo is not None
        elif value is not None and kind is Decimal:
            value = Decimal(str(value))
        values[attr] = deepcopy(value)
    return model(**values)


def seed_episode(tmp_path):
    source = Path(os.environ["WBPA3_SOURCE_DIRECTORY"])
    data = {}
    for name, expected in INPUTS.items():
        raw = (source / name).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == expected
        data[name] = json.loads(raw)
    raw = data["chow_episode_1030.json"]
    extra = data["wbpa3_brokerdecides_1030.json"]
    assert all(raw[k] is None for k in ("fills", "virtual_positions", "account_positions"))
    assert all(extra[k] is None for k in ("dispatch_evidence", "dispatch_retirements", "reprice_handoffs"))
    dispatch = [r for r in extra["v2_wait_dispatch"] if r["payload"]["symbol"] == "CHOW"]
    assert len(dispatch) == 5
    assert len(raw["intents"]) == 7 and len(raw["orders"]) == len(raw["order_events"]) == 4
    engine = create_engine("sqlite+pysqlite:///" + str(tmp_path / "wbpa3.sqlite"),
        connect_args={"check_same_thread": False}, poolclass=NullPool)
    factory = sessionmaker(engine, expire_on_commit=False)
    Base.metadata.create_all(engine)
    try:
        with factory.begin() as session:
            session.add_all(decode_model(BrokerAccount, r) for r in raw["accounts"])
            session.add(Strategy(id=UUID(raw["intents"][0]["strategy_id"]), code="schwab_1m_v2",
                name="CONTROLLED omitted strategy identity", execution_mode="live", is_enabled=True))
            session.flush()
            for key, model in (("intents", TradeIntent), ("orders", BrokerOrder), ("order_events", BrokerOrderEvent)):
                session.add_all(decode_model(model, r) for r in raw[key])
                session.flush()
            session.add_all(decode_model(DashboardSnapshot, r) for r in dispatch)
        return SimpleNamespace(engine=engine, factory=factory, raw=raw, extra=extra,
            dispatch=dispatch, observed=OBSERVED)
    except BaseException:
        engine.dispose()
        raise

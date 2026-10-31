"""Typed, private SQLite port of C1's immutable bounded TNMG row export.

Only an explicitly supplied, hash-pinned local export is accepted. Source rows
are deduplicated by actual primary key; no fills, prices or bindings are seeded.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import DateTime, Numeric, Uuid, create_engine, inspect, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import (
    AccountPosition, BrokerAccount, BrokerOrder, BrokerOrderEvent, Fill,
    OmsManagedPosition, Strategy, SystemIncident, TradeIntent, VirtualPosition,
)
from project_mai_tai.oms import service as oms, wbexit2, wbexit3
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.settings import Settings

EXPORT_SHA = '32a4036ddd1b9bdd38708f3f011021df6216e9895321b3e32ddfc588c0b87202'
WB2_ID = UUID('b1a620b4-6c2f-5542-b9ba-32181cff5783')
WB3_ID = UUID('cbc5ef69-e175-5e4e-bd21-0463ae1d0657')
ENTRY_ID = UUID('b591dd13-74db-49f1-916e-a0c307e5ea72')
TABLES = (BrokerAccount, Strategy, TradeIntent, BrokerOrder, BrokerOrderEvent,
          Fill, OmsManagedPosition, VirtualPosition, AccountPosition, SystemIncident)


def pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError('duplicate export key')
        result[key] = value
    return result


def load_export():
    path = Path(os.environ['WBSTALE1_EPISODE_SOURCE'])
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == EXPORT_SHA
    return json.loads(raw, object_pairs_hook=pairs,
        parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))


def row_values(model, original):
    mapper = inspect(model)
    columns = {prop.columns[0].name: prop for prop in mapper.column_attrs}
    assert set(original) == set(columns), (model.__name__, set(columns)-set(original))
    result = {}
    for name, value in original.items():
        prop = columns[name]
        dtype = prop.columns[0].type
        if value is not None:
            if isinstance(dtype, Uuid):
                value = UUID(value)
            elif isinstance(dtype, DateTime):
                value = datetime.fromisoformat(value)
            elif isinstance(dtype, Numeric):
                value = Decimal(str(value))
            else:
                value = deepcopy(value)
        result[prop.key] = value
    return result


def source_rows(export):
    groups = {
        BrokerAccount: [export['broker_account']], Strategy: [export['strategy']],
        TradeIntent: [export['entry_intent'], export['protection_intent'], *export['tnmg_intents_today']],
        BrokerOrder: [export['entry_order'], *export['tnmg_orders_today']],
        BrokerOrderEvent: export['tnmg_order_events_today'], Fill: export['tnmg_fills_today'],
        OmsManagedPosition: [export['managed_row']], VirtualPosition: export['virtual_position'],
        AccountPosition: export['account_positions'], SystemIncident: export['system_incidents'],
    }
    result = {}
    for model, rows in groups.items():
        unique = {}
        for row in rows:
            if row['id'] in unique:
                assert unique[row['id']] == row
            unique[row['id']] = row
        result[model] = list(unique.values())
    return result


def snapshot(factory):
    with factory() as db:
        return {model.__tablename__: {
            str(row.id): {prop.key: deepcopy(getattr(row, prop.key))
                         for prop in inspect(model).column_attrs}
            for row in db.scalars(select(model)).all()
        } for model in TABLES}


def seed_episode(tmp_path):
    export = load_export()
    db_path = tmp_path / 'tnmg-episode.sqlite'
    assert not db_path.exists()
    engine = create_engine('sqlite+pysqlite:///' + str(db_path), future=True)
    Base.metadata.create_all(engine, tables=[model.__table__ for model in TABLES])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory.begin() as db:
        for model, rows in source_rows(export).items():
            db.add_all([model(**row_values(model, row)) for row in rows])
    attempts = []
    class Refusing:
        def __getattr__(self, name):
            def denied(*args, **kwargs):
                attempts.append(name)
                raise AssertionError('provider/Redis operation forbidden in row-copy replay')
            return denied
    service = oms.OmsRiskService.__new__(oms.OmsRiskService)
    service.settings = Settings(_env_file=None,
        strategy_schwab_1m_v2_wbexit2_enabled=True, oms_webull_exit3_enabled=True)
    service.store = OmsStore()
    service.logger = logging.getLogger('CONTROLLED-TNMG-retained-row-copy')
    service.broker_adapter = service.redis = Refusing()
    service.session_factory = factory
    service._wbattach_exit_owners = {}
    async def run_db(unit, *, commit=False):
        with factory() as db:
            result = unit(db)
            if commit:
                db.commit()
            else:
                db.rollback()
            return result
    service._run_db = run_db
    # A replay clock AFTER retained row clocks, never a fabricated 13:36 receipt.
    stamps = [datetime.fromisoformat(export['account_positions'][0]['source_updated_at'])]
    stamps.extend(datetime.fromisoformat(row['payload']['updated_at'])
                  for row in export['system_incidents'] if 'updated_at' in row['payload'])
    now = max(stamps) + timedelta(seconds=1)
    return SimpleNamespace(service=service, factory=factory, engine=engine,
                           export=export, attempts=attempts, now=now, path=db_path)


async def hydrate(case):
    await wbexit2.hydrate_at_boot(case.service)
    await wbexit3.hydrate_at_boot(case.service)


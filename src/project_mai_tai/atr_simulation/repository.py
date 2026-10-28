"""Atomic simulation inbox/checkpoints/results, with no live model imports."""

from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
from uuid import UUID

import sqlalchemy as sa

from .models import consumers, inputs, rows, runs
from .service_contract import canonical_position_fields


def json_value(value):
    # Exact JSON primitives cannot carry dataclass/Enum conversion semantics.
    if value is None or type(value) in (str, int, float, bool):
        return value
    if is_dataclass(value):
        return json_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("nonfinite_simulation_decimal")
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    return value


def digest(value):
    return hashlib.sha256(json.dumps(json_value(value), sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class SourceConflict(ValueError):
    pass


class SimulationRepository:
    def __init__(self, engine):
        self.engine = engine

    def checkpoint(self, consumer_id):
        with self.engine.connect() as connection:
            row = connection.execute(sa.select(consumers).where(
                consumers.c.consumer_id == consumer_id)).mappings().one_or_none()
        return dict(row) if row else None

    def capture(self, consumer_id, expected_revision, events, cursors, *, issues):
        """Inbox and cursor share ONE transaction. Crash before commit replays all."""
        now = datetime.now(timezone.utc)
        with self.engine.begin() as connection:
            current = connection.execute(sa.select(consumers).where(
                consumers.c.consumer_id == consumer_id).with_for_update()).mappings().one_or_none()
            revision = current["revision"] if current else 0
            if revision != expected_revision:
                raise SourceConflict("consumer_revision_changed")
            for event in events:
                position = (event["stream"], event["stream_id"])
                key, payload_hash = digest(position), digest(event["fields"])
                prior = connection.execute(sa.select(inputs.c.payload_sha256).where(
                    inputs.c.event_key == key)).scalar_one_or_none()
                if prior is not None:
                    if prior != payload_hash:
                        raise SourceConflict("same_source_position_changed_payload")
                    continue
                connection.execute(inputs.insert().values(event_key=key, stream=position[0],
                    stream_id=position[1], payload_sha256=payload_hash,
                    fields=json_value(event["fields"]), recorded_at=now))
            values = dict(revision=revision + 1, cursors=dict(cursors),
                status="CAPTURING_ASSEMBLER_UNBOUND", issues=list(issues), updated_at=now)
            if current:
                connection.execute(consumers.update().where(
                    consumers.c.consumer_id == consumer_id).values(**values))
            else:
                connection.execute(consumers.insert().values(consumer_id=consumer_id, **values))
        return revision + 1

    def publish(self, *, source_id, input_sha256, policy_version, source_refs, coverage, result_rows):
        """Append-only sealed source scope; conflicting replay never replaces evidence."""
        if (not isinstance(source_id, str) or not 1 <= len(source_id) <= 128
                or not isinstance(policy_version, str) or not 1 <= len(policy_version) <= 128
                or not re.fullmatch(r"[0-9a-f]{64}", input_sha256) or not source_refs):
            raise ValueError("explicit_source_policy_and_input_digest_required")
        result_rows = tuple(result_rows)
        result_hash = digest(result_rows)
        proposed = dict(source_id=source_id, input_sha256=input_sha256,
            policy_version=policy_version, source_refs=json_value(source_refs),
            coverage=json_value(coverage), result_sha256=result_hash)
        with self.engine.begin() as connection:
            prior = connection.execute(sa.select(runs).where(
                runs.c.source_id == source_id)).mappings().one_or_none()
            if prior:
                if any(prior[key] != value for key, value in proposed.items()):
                    raise SourceConflict("sealed_source_replay_changed")
                return False
            connection.execute(runs.insert().values(**proposed, created_at=datetime.now(timezone.utc)))
            seen = set()
            for row in result_rows:
                identity = row["identity"]
                if any(not isinstance(identity.get(name), str) or not identity[name] for name in (
                        "trading_day", "symbol", "broker", "account_id", "strategy", "flip_id", "segment_id")):
                    raise ValueError("complete_flip_account_identity_required")
                key = digest((source_id, policy_version, identity, row["mode"], row["model"]))
                if key in seen:
                    raise SourceConflict("duplicate_flip_account_mode_model_source")
                seen.add(key)
                position = {} if row["mode"] == "LIVE" else json_value(canonical_position_fields(row))
                connection.execute(rows.insert().values(row_key=key, source_id=source_id,
                    policy_version=policy_version, day=date.fromisoformat(identity["trading_day"]),
                    symbol=identity["symbol"], broker=identity["broker"], account_id=identity["account_id"],
                    strategy=identity["strategy"], flip_id=identity["flip_id"],
                    segment_id=identity["segment_id"], mode=row["mode"], model=row["model"],
                    payload_sha256=digest(row), payload=json_value(row), **position))
        return True

    def read(self, source_id, start_day, end_day, *, limit=1000):
        if not 1 <= limit <= 10000 or end_day < start_day or (end_day - start_day).days > 6:
            raise ValueError("bounded_day_or_week_required")
        with self.engine.connect() as connection:
            run = connection.execute(sa.select(runs).where(runs.c.source_id == source_id)).mappings().one_or_none()
            result = connection.execute(sa.select(rows.c.payload).where(
                rows.c.source_id == source_id, rows.c.day >= start_day, rows.c.day <= end_day)
                .order_by(rows.c.day, rows.c.row_key).limit(limit + 1)).scalars().all()
            status = connection.execute(sa.select(consumers)).mappings().all()
        return {"run": dict(run) if run else None, "rows": result[:limit],
            "truncated": len(result) > limit, "consumers": [dict(item) for item in status]}


class RetainedInputReader:
    """Bounded SELECT-only source pages. Rows do not establish complete coverage."""

    def __init__(self, read_engine):
        self.engine = read_engine

    def _read_only(self, query):
        with self.engine.connect() as connection:
            transaction = connection.begin()
            try:
                if connection.dialect.name != "postgresql":
                    raise ValueError("explicit_readonly_postgresql_source_required")
                connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                connection.exec_driver_sql("SET LOCAL statement_timeout = '750ms'")
                connection.exec_driver_sql("SET LOCAL lock_timeout = '250ms'")
                return [dict(row) for row in connection.execute(query).mappings()]
            finally:
                transaction.rollback()

    def capture_page(self, kind, symbol, start, end, *, after=None, limit=1000):
        from datetime import timedelta

        if (kind not in {"trade", "quote"} or not 1 <= limit <= 1000
                or start.tzinfo is None or end.tzinfo is None or not start < end
                or end - start > timedelta(minutes=1)):
            raise ValueError("bounded_one_minute_source_page_required")
        common = [sa.column("id"), sa.column("provider"), sa.column("symbol"),
                  sa.column("event_ts"), sa.column("received_at")]
        fields = ("price", "size", "exchange", "conditions") if kind == "trade" else (
            "bid_price", "ask_price", "bid_size", "ask_size")
        table = sa.table("market_capture_trades" if kind == "trade" else "market_capture_quotes",
            *common, *(sa.column(name) for name in fields))
        query = sa.select(table).where(table.c.symbol == symbol,
            table.c.event_ts >= start, table.c.event_ts < end)
        if after is not None:
            stamp, identity = after
            query = query.where(sa.or_(table.c.event_ts > stamp,
                sa.and_(table.c.event_ts == stamp, table.c.id > identity)))
        result = self._read_only(query.order_by(table.c.event_ts, table.c.id).limit(limit + 1))
        page = result[:limit]
        return {"table": table.name, "rows": json_value(page), "has_more": len(result) > limit,
            "next_cursor": (page[-1]["event_ts"], page[-1]["id"]) if page else None,
            "coverage_complete": None,
            "issues": ["ROW_PAGE_NOT_COVERAGE_PROOF", "DB_ID_NOT_NATIVE_SEQUENCE",
                       "RECEIVED_AT_IS_CAPTURE_CLOCK_NOT_PROVIDER_COMPLETENESS"]}

    def live_fill_page(self, symbol, account_name, start, end, *, after=None, limit=1000):
        """Actual UUID-linked facts, NEVER the watched-flip denominator or FIFO."""
        from datetime import timedelta

        if (not symbol or not account_name or not 1 <= limit <= 1000 or start.tzinfo is None
                or end.tzinfo is None or not start < end or end - start > timedelta(minutes=1)):
            raise ValueError("bounded_named_live_fact_page_required")
        fills = sa.table("fills", *(sa.column(name, sa.Uuid()) for name in (
            "id", "order_id", "strategy_id", "broker_account_id")),
            sa.column("broker_fill_id"), sa.column("symbol"), sa.column("side"),
            sa.column("quantity"), sa.column("price"), sa.column("filled_at"))
        orders = sa.table("broker_orders", sa.column("id", sa.Uuid()), sa.column("intent_id", sa.Uuid()),
            sa.column("broker_account_id", sa.Uuid()), sa.column("strategy_id", sa.Uuid()),
            sa.column("client_order_id"), sa.column("broker_order_id"), sa.column("payload", sa.JSON()))
        intents = sa.table("trade_intents", sa.column("id", sa.Uuid()), sa.column("reason"),
            sa.column("broker_account_id", sa.Uuid()), sa.column("strategy_id", sa.Uuid()))
        accounts = sa.table("broker_accounts", sa.column("id", sa.Uuid()), sa.column("name"), sa.column("provider"))
        strategies = sa.table("strategies", sa.column("id", sa.Uuid()), sa.column("code"))
        query = sa.select(fills.c.id.label("fill_id"), fills.c.order_id,
            fills.c.broker_account_id.label("fill_account_id"), fills.c.strategy_id.label("fill_strategy_id"),
            orders.c.broker_account_id.label("order_account_id"), orders.c.strategy_id.label("order_strategy_id"),
            intents.c.broker_account_id.label("intent_account_id"), intents.c.strategy_id.label("intent_strategy_id"),
            accounts.c.id.label("physical_account_id"), strategies.c.id.label("physical_strategy_id"),
            strategies.c.code.label("strategy_code"),
            fills.c.broker_fill_id, fills.c.symbol, fills.c.side, fills.c.quantity,
            fills.c.price, fills.c.filled_at, orders.c.client_order_id, orders.c.broker_order_id,
            orders.c.intent_id, orders.c.payload.label("order_payload_raw"),
            intents.c.reason.label("intent_reason_raw"), accounts.c.name.label("account_name"),
            accounts.c.provider.label("broker")).select_from(fills.join(orders, orders.c.id == fills.c.order_id)
                .outerjoin(intents, intents.c.id == orders.c.intent_id)
                .join(accounts, accounts.c.id == fills.c.broker_account_id)
                .join(strategies, strategies.c.id == fills.c.strategy_id)).where(
                    fills.c.symbol == symbol, accounts.c.name == account_name,
                    strategies.c.code == "schwab_1m_v2", fills.c.filled_at >= start, fills.c.filled_at < end)
        if after is not None:
            stamp, identity = after
            query = query.where(sa.or_(fills.c.filled_at > stamp,
                sa.and_(fills.c.filled_at == stamp, fills.c.id > identity)))
        result = self._read_only(query.order_by(fills.c.filled_at, fills.c.id).limit(limit + 1))
        page = result[:limit]
        return {"rows": json_value(page), "has_more": len(result) > limit,
            "next_cursor": (page[-1]["filled_at"], page[-1]["fill_id"]) if page else None,
            "native_asof": "UNKNOWN", "flip_coverage_complete": None,
            "issues": ["UNALLOCATED_FACTS_NOT_EPISODE_EVIDENCE", "NO_FIFO_ALLOCATION",
                       "NO_FILLS_IS_NOT_NO_BUY", "RAW_REASON_NOT_INFERRED_NATIVE_EXIT"]}


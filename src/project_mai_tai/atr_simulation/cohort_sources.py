"""Concrete retained readers and raw Redis collection; no source-table writes."""

from datetime import datetime, timedelta
import json
from uuid import UUID

import sqlalchemy as sa

from .models import inputs
from .repository import RetainedInputReader, digest, json_value
from .worker import stream_position


STRATEGY = "schwab_1m_v2"
SOURCE_TOPICS = ("strategy-state", "strategy-state-isolated", "market-data")


class CohortSourceReader(RetainedInputReader):
    def account_identity_page(self, account_id, strategy_id):
        account_uuid, strategy_uuid = UUID(account_id), UUID(strategy_id)
        accounts = sa.table("broker_accounts", sa.column("id", sa.Uuid()),
            sa.column("name"), sa.column("provider"))
        strategies = sa.table("strategies", sa.column("id", sa.Uuid()), sa.column("code"))
        query = sa.select(accounts.c.id.label("account_id"), accounts.c.name.label("account_name"),
            accounts.c.provider.label("broker"), strategies.c.id.label("strategy_id"),
            strategies.c.code.label("strategy_code")).select_from(accounts.join(strategies, sa.true())).where(
                accounts.c.id == account_uuid, strategies.c.id == strategy_uuid,
                strategies.c.code == STRATEGY).limit(2)
        rows = self._read_only(query)
        if len(rows) != 1:
            raise ValueError("exact_actual_account_strategy_mapping_required")
        return json_value(rows[0])

    def bar_page(self, symbol, start, end, *, after=None, limit=1000):
        if (not 1 <= limit <= 1000 or start.tzinfo is None or end.tzinfo is None
                or not start < end or end - start > timedelta(hours=16)):
            raise ValueError("bounded_session_bar_page_required")
        table = sa.table("strategy_bar_history", *(sa.column(name) for name in (
            "id", "strategy_code", "symbol", "interval_secs", "bar_time", "source",
            "open_price", "high_price", "low_price", "close_price", "volume",
            "indicators", "created_at", "decision_status", "decision_reason")))
        query = sa.select(table).where(table.c.strategy_code == STRATEGY,
            table.c.symbol == symbol, table.c.interval_secs == 60,
            table.c.bar_time >= start, table.c.bar_time < end)
        if after is not None:
            query = query.where(table.c.bar_time > after)
        found = self._read_only(query.order_by(table.c.bar_time).limit(limit + 1))
        page = found[:limit]
        return {"rows": json_value(page), "has_more": len(found) > limit,
            "next_cursor": page[-1]["bar_time"] if page else None,
            "issues": ["UPSERTED_BARS_NOT_ORIGINAL_ASOF", "EMPTY_INDICATORS_NOT_FLIP_EVIDENCE"]}


def collect_retained(reader, requests, *, maximum_pages=4096):
    """Bounded requests use existing schemas, never a provider or live Fill oracle."""
    records, pages = [], 0
    seen = {}
    for request in requests:
        kind = request["kind"]
        if kind == "account_identity":
            pages += 1
            if pages > maximum_pages:
                raise ValueError("retained_page_budget_exhausted_no_partial_seal")
            row = reader.account_identity_page(request["account_id"], request["strategy_id"])
            key = "account_identity:" + row["account_id"] + ":" + row["strategy_id"]
            packet = {"key": key, "kind": kind, "row": row}
            if key in seen and digest(seen[key]) != digest(packet):
                raise ValueError("source_changed_during_retained_collection:" + key)
            if key not in seen:
                seen[key] = packet
                records.append(packet)
            continue
        symbol = request["symbol"]
        start = datetime.fromisoformat(request["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(request["end"].replace("Z", "+00:00"))
        if kind not in {"trade", "quote", "live_fill", "bar"}:
            raise ValueError("unsupported_retained_source_kind")
        cursor = None
        while True:
            pages += 1
            if pages > maximum_pages:
                raise ValueError("retained_page_budget_exhausted_no_partial_seal")
            if kind in {"trade", "quote"}:
                page = reader.capture_page(kind, symbol, start, end, after=cursor)
            elif kind == "live_fill":
                page = reader.live_fill_page(symbol, request["account_name"], start, end, after=cursor)
            else:
                page = reader.bar_page(symbol, start, end, after=cursor)
            for row in page["rows"]:
                identity = row["fill_id"] if kind == "live_fill" else row["id"]
                key = kind + ":" + str(identity)
                packet = {"key": key, "kind": kind, "row": row}
                if key in seen:
                    if digest(seen[key]) != digest(packet):
                        raise ValueError("source_changed_during_retained_collection:" + key)
                    continue
                seen[key] = packet
                records.append(packet)
            if not page["has_more"]:
                break
            next_cursor = page["next_cursor"]
            if next_cursor is None or next_cursor == cursor:
                raise ValueError("retained_pagination_not_advancing")
            cursor = next_cursor
    return tuple(records)


def persist_packets(repository, source_id, packets):
    """Idempotent raw SIM inbox, not a claim that a source window was complete."""
    consumer_id = "cohort:" + source_id
    prior = repository.checkpoint(consumer_id)
    events = [{"stream": "retained:" + source_id, "stream_id": packet["key"],
        "fields": {"data": json.dumps(packet, sort_keys=True, separators=(",", ":"))}}
        for packet in packets]
    repository.capture(consumer_id, prior["revision"] if prior else 0, events,
        {"retained:" + source_id: digest(packets)},
        issues=["RAW_RETAINED_ROWS_NOT_COVERAGE_OR_NATIVE_ASOF_PROOF"])


def captured_snapshot_packets(repository, event_keys):
    """Read named already-captured SIM inbox facts, not a fill-derived population."""
    if len(event_keys) > 4096 or len(set(event_keys)) != len(event_keys):
        raise ValueError("bounded_unique_snapshot_keys_required")
    if not event_keys:
        return ()
    with repository.engine.connect() as connection:
        found = connection.execute(sa.select(inputs).where(inputs.c.event_key.in_(event_keys))).mappings().all()
    if {row["event_key"] for row in found} != set(event_keys):
        raise ValueError("captured_snapshot_operand_missing")
    packets = []
    for row in found:
        if not any(row["stream"].endswith(":" + name) for name in SOURCE_TOPICS[:2]):
            raise ValueError("not_a_captured_membership_snapshot")
        raw = row["fields"].get("data", "")
        if len(raw.encode()) > 262144:
            raise ValueError("bounded_snapshot_required")
        event = json.loads(raw, parse_float=str)
        packets.append({"key": "snapshot:" + row["event_key"], "kind": "snapshot",
            "row": {"event": event, "stream": row["stream"], "stream_id": row["stream_id"],
                    "fields": row["fields"]}})
    return tuple(sorted(packets, key=lambda packet: packet["key"]))


def scanner_symbols(event):
    """Read actual full-snapshot candidates; never derive membership from fills."""
    payload = event["payload"]
    values = []
    if event["event_type"] == "strategy_state_snapshot":
        for field in ("top_confirmed", "all_confirmed", "watchlist"):
            values.extend(payload.get(field, []))
    elif event["event_type"] == "isolated_bot_state":
        if payload.get("strategy_code") != STRATEGY:
            return ()
        values.extend(payload.get("watchlist", []))
    else:
        return ()
    symbols = set()
    for item in values:
        value = item.get("symbol", item.get("ticker")) if isinstance(item, dict) else item
        if isinstance(value, str) and value and value == value.upper() and value.strip() == value:
            symbols.add(value)
    return tuple(sorted(symbols))


class CausalSourceCollector:
    """Actual existing isolated-state stream, not the earlier bot-state placeholder."""

    def __init__(self, redis, repository, *, prefix, consumer_id):
        if not prefix or not consumer_id:
            raise ValueError("explicit_source_namespace_required")
        self.redis, self.repository, self.consumer_id = redis, repository, consumer_id
        self.streams = tuple(prefix + ":" + name for name in SOURCE_TOPICS)

    async def run_once(self):
        prior = self.repository.checkpoint(self.consumer_id)
        cursors = dict(prior["cursors"]) if prior else dict.fromkeys(self.streams, "0-0")
        if set(cursors) != set(self.streams):
            raise ValueError("collector_source_scope_changed")
        for value in cursors.values():
            stream_position(value)
        response = await self.redis.xread(cursors, count=200, block=1000)
        events, advanced = [], dict(cursors)
        for stream, entries in response:
            if stream not in advanced:
                raise ValueError("foreign_source_stream")
            for identity, fields in entries:
                if stream_position(identity) <= stream_position(advanced[stream]):
                    raise ValueError("noncausal_source_cursor")
                if (not isinstance(fields, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                        for k, v in fields.items())
                        or sum(len(k.encode()) + len(v.encode()) for k, v in fields.items()) > 262144):
                    raise ValueError("bounded_raw_source_fields_required")
                # No global timestamp is invented by sorting these separate streams.
                events.append({"stream": stream, "stream_id": identity, "fields": fields})
                if len(events) > 600:
                    raise ValueError("bounded_source_response_required")
                advanced[stream] = identity
        self.repository.capture(self.consumer_id, prior["revision"] if prior else 0,
            events, advanced, issues=["REDIS_RETENTION_PREFIX_UNKNOWN",
                "RAW_QUOTE_TICK_HAS_NO_EXCHANGE_TIMESTAMP", "FULL_FLIP_OPERANDS_NOT_IN_BOT_SNAPSHOT"])
        return len(events)

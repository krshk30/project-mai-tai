"""Oct8 trading-condition gate; GET/READ ONLY, never refresh or dispatch.

rc 0 clear, 1 measured work, 2 incomplete/unreadable evidence. JSON stdout
is the receipt; stderr retains exceptions. Runner must retain both streams.
"""
import argparse
import asyncio
import json
import os
import sys
import time as clock
import traceback
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
UTC = timezone.utc
ET = ZoneInfo("America/New_York")
MAX_ROWS = 1024
FRESH_SECONDS = 120
TERMINAL = {"filled", "cancelled", "canceled", "rejected", "expired", "replaced", "failed"}
LIVE = {"new", "pending", "submitted", "accepted", "working", "open", "queued",
        "submitting", "partially_filled", "partial_filled", "partial filled", "partial_fill",
        "pending_cancel", "pending_replace", "pending_recall", "pending_activation",
        "pending_acknowledgement", "awaiting_condition", "awaiting_manual_review",
        "awaiting_parent_order", "awaiting_release_time"}


def need(ok, message):
    if not ok:
        raise ValueError(message)


def quantity(value):
    need(not isinstance(value, bool), "boolean quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("unreadable quantity") from exc
    need(number.is_finite(), "nonfinite quantity")
    return number


def bounded(body):
    need(len(json.dumps(body).encode()) <= 4_000_000, "broker body exceeds 4MB")
    return body


def account_bound(body, expected):
    need(isinstance(body, dict), "broker envelope absent")
    for key in ("account_id", "accountId", "accountNumber"):
        need(body.get(key) is None or str(body[key]) == expected, "foreign broker account")


def session_start(now):
    local = now.astimezone(ET)
    start = datetime.combine(local.date(), time(4), ET)
    return start - timedelta(days=1) if local < start else start


def schwab_account_number(mapping, account_hash):
    need(isinstance(mapping, list) and len(mapping) <= 64, "Schwab account map unreadable")
    for row in mapping:
        need(isinstance(row, dict) and isinstance(row.get("hashValue"), str)
             and isinstance(row.get("accountNumber"), str) and row["accountNumber"].isdigit(),
             "Schwab account map malformed")
    matches = [row["accountNumber"] for row in mapping if row["hashValue"] == account_hash]
    need(len(matches) == 1, "Schwab configured account not uniquely mapped")
    return matches[0]


def schwab_positions(body, number):
    need(isinstance(body, dict), "Schwab positions envelope absent")
    account = body.get("securitiesAccount")
    need(isinstance(account, dict) and account.get("accountNumber") == number
         and isinstance(account.get("currentBalances"), dict), "Schwab positions account unproven")
    rows = account.get("positions", [])
    need(isinstance(rows, list) and len(rows) <= MAX_ROWS, "Schwab positions incomplete")
    result, seen = [], set()
    for row in rows:
        need(isinstance(row, dict) and isinstance(row.get("instrument"), dict), "Schwab holding malformed")
        symbol = row["instrument"].get("symbol")
        need(isinstance(symbol, str) and symbol and symbol.upper() not in seen, "Schwab holding identity")
        seen.add(symbol.upper())
        need("longQuantity" in row or "shortQuantity" in row, "Schwab quantity absent")
        long, short = quantity(row.get("longQuantity", 0)), quantity(row.get("shortQuantity", 0))
        need(long >= 0 and short >= 0, "Schwab negative long/short")
        if long or short:
            result.append(dict(account=ACCOUNTS[0], symbol=symbol.upper(), quantity=str(long), short=str(short)))
    return result


def working_rows(rows, account, expected_id=None, schwab=False):
    need(isinstance(rows, list) and len(rows) <= MAX_ROWS, "orders list incomplete")
    result, identities = [], set()

    def walk(row, depth=0):
        need(isinstance(row, dict) and depth <= 8, "order tree malformed")
        if expected_id is not None:
            account_bound(row, expected_id)
        if not schwab and "items" in row:
            items = row["items"]
            parent_id = row.get("client_order_id", row.get("clientOrderId"))
            need(isinstance(items, list) and items and parent_id, "Webull combo items/identity absent")
            for index, item in enumerate(items):
                need(isinstance(item, dict), "Webull combo item malformed")
                walk(dict(item, client_order_id=str(parent_id) + ":item:" + str(index)), depth + 1)
            return
        children = row.get("childOrderStrategies", [])
        need(isinstance(children, list), "order children malformed")
        status = str(row.get("status") or row.get("order_status") or "").lower()
        # Schwab OCO wrappers have children but no status or actual order legs.
        container = schwab and children and not row.get("orderLegCollection") and not status
        if not container:
            identity = row.get("orderId") if schwab else row.get("client_order_id", row.get("clientOrderId"))
            need(identity is not None and str(identity) and str(identity) not in identities, "order identity absent/duplicate")
            identities.add(str(identity))
            need(status in LIVE | TERMINAL, "unknown broker order status: " + status)
            if status in LIVE:
                result.append(dict(account=account, id=str(identity), status=status))
        for child in children:
            walk(child, depth + 1)

    for row in rows:
        walk(row)
    return result


class SpacedClient:
    """Serial direct GETs; no adapter caches, two seconds between Webull calls."""
    def __init__(self, client, monotonic=clock.monotonic, sleep=clock.sleep):
        self.client, self.monotonic, self.sleep, self.last = client, monotonic, sleep, None

    def read(self, request):
        if self.last is not None:
            self.sleep(max(0, 2 - (self.monotonic() - self.last)))
        self.last = self.monotonic()
        response = self.client.get_response(request)
        need(200 <= int(getattr(response, "status_code", 0)) < 300, "Webull HTTP unreadable")
        body = bounded(response.json())
        need(isinstance(body, dict) and not body.get("error_code"), "Webull error envelope")
        return body


def webull_pages(reader, request_factory, account_id, kind):
    key, size = ("holdings", 50) if kind == "positions" else ("orders", 100)
    cursor, cursors, rows, identities = None, set(), [], set()
    for page in range(20):
        request = request_factory()
        request.set_account_id(account_id)
        request.set_page_size(size)
        if cursor:
            if kind == "positions":
                request.set_last_instrument_id(cursor)
            else:
                request.set_last_client_order_id(cursor)
        body = reader.read(request)
        account_bound(body, account_id)
        if kind == "positions" and "holdings" in body and "positions" in body:
            need(body["holdings"] == body["positions"], "conflicting positions lists")
        batch = body.get(key, body.get("positions") if kind == "positions" else None)
        need(isinstance(batch, list) and len(batch) <= size, "Webull page malformed")
        markers = [body[k] for k in ("has_next", "hasNext") if k in body]
        need(markers and all(type(v) is bool for v in markers) and len(set(markers)) == 1,
             "Webull pagination unknown")
        for row in batch:
            account_bound(row, account_id)
            identity = (row.get("instrument_id", row.get("instrumentId")) if kind == "positions"
                        else row.get("client_order_id", row.get("clientOrderId")))
            if kind == "positions" and identity is None:
                identity = row.get("symbol", row.get("ticker"))
            need(identity is not None and str(identity) and str(identity) not in identities,
                 "Webull duplicate/absent page identity")
            identities.add(str(identity))
        rows.extend(batch)
        need(len(rows) <= MAX_ROWS, "Webull population over evidence bound")
        if not markers[0]:
            return rows
        need(batch and page < 19, "Webull pagination incomplete")
        last = batch[-1]
        cursor = (last.get("instrument_id", last.get("instrumentId")) if kind == "positions"
                  else last.get("client_order_id", last.get("clientOrderId")))
        need(cursor and cursor not in cursors, "Webull cursor did not advance")
        cursors.add(cursor)
    raise ValueError("Webull pagination incomplete")


def webull_holdings(rows):
    result = []
    for row in rows:
        instrument = row.get("instrument")
        symbol = row.get("symbol") or row.get("ticker") or (
            instrument.get("symbol") if isinstance(instrument, dict) else None)
        raw = next((row[k] for k in ("quantity", "qty", "position", "shares") if k in row), None)
        need(isinstance(symbol, str) and symbol and raw is not None, "Webull holding incomplete")
        qty = quantity(raw)
        if qty:
            result.append(dict(account=ACCOUNTS[1], symbol=symbol.upper(), quantity=str(qty), short="0"))
    return result


def local_terminal_proof(row):
    """The reviewed no-id local-abort/submit-reject class; not status alone."""
    if row.get("broker_order_id") or row.get("has_fill") or row.get("status") not in {"rejected", "aborted"}:
        return False
    payload = row.get("intent_payload") or {}
    client_abort = (payload.get("refusal_origin") == "client_abort"
                    and isinstance(payload.get("refusal_code"), str) and bool(payload["refusal_code"]))
    return bool(row.get("intent_bound") and (client_abort or row.get("submit_reject_event")))


QUERIES = {
    "managed_rows": "SELECT broker_account_name account,symbol,current_quantity quantity FROM oms_managed_positions WHERE broker_account_name IN (:schwab,:webull) AND status='open'",
    "virtual_rows": "SELECT a.name account,v.symbol,v.quantity FROM virtual_positions v JOIN broker_accounts a ON a.id=v.broker_account_id WHERE a.name IN (:schwab,:webull) AND v.quantity<>0",
    "account_rows": "SELECT a.name account,p.symbol,p.quantity,p.updated_at,p.source_updated_at FROM account_positions p JOIN broker_accounts a ON a.id=p.broker_account_id WHERE a.name IN (:schwab,:webull) AND p.quantity<>0",
    "session_order_counts": "SELECT a.name account,b.symbol,count(*) total FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id LEFT JOIN trade_intents i ON i.id=b.intent_id WHERE a.name IN (:schwab,:webull) AND (b.submitted_at>=:start OR i.created_at>=:start OR b.updated_at>=:start OR (b.submitted_at IS NULL AND i.created_at IS NULL AND b.updated_at IS NULL)) GROUP BY 1,2",
    "session_fill_counts": "SELECT a.name account,f.symbol,count(*) total FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.name IN (:schwab,:webull) AND f.filled_at>=:start GROUP BY 1,2",
    "working_orders": "SELECT b.id,a.name account,b.symbol,b.status,b.broker_order_id,b.intent_id,i.payload intent_payload,coalesce(i.broker_account_id=b.broker_account_id AND i.strategy_id=b.strategy_id AND i.symbol=b.symbol AND i.side=b.side AND i.quantity=b.quantity AND i.intent_type='open',false) intent_bound,EXISTS(SELECT 1 FROM fills f WHERE f.order_id=b.id) has_fill,EXISTS(SELECT 1 FROM broker_order_events e WHERE e.order_id=b.id AND e.event_type='rejected' AND e.event_source='broker') submit_reject_event FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id LEFT JOIN trade_intents i ON i.id=b.intent_id WHERE a.name IN (:schwab,:webull) AND (b.status IS NULL OR lower(b.status) NOT IN ('cancelled','canceled','filled','expired','replaced','rejected') OR (lower(b.status)='rejected' AND b.broker_order_id IS NULL AND (b.submitted_at>=:start OR i.created_at>=:start OR b.updated_at>=:start)))",
    "inflight_intents": "SELECT t.id,a.name account,t.symbol,t.status FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN (:schwab,:webull) AND (t.status IS NULL OR lower(t.status) NOT IN ('completed','rejected','cancelled','canceled','filled','expired'))",
}


def sql_snapshot(config, now):
    from sqlalchemy import event, text
    from project_mai_tai.db.session import build_engine

    engine = build_engine(config.database_url, connect_timeout_s=5, statement_timeout_ms=5000,
                          lock_timeout_ms=500, pool_timeout_s=5)
    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    params = dict(schwab=ACCOUNTS[0], webull=ACCOUNTS[1], start=session_start(now).astimezone(UTC))
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            need(connection.execute(text("SHOW transaction_read_only")).scalar_one() == "on", "SQL not read-only")
            names = list(connection.execute(text("SELECT name FROM broker_accounts WHERE name IN (:schwab,:webull)"), params).scalars())
            need(sorted(names) == sorted(ACCOUNTS), "SQL account mapping incomplete/duplicate")
            result = {"observed_at": connection.execute(text("SELECT now()")).scalar_one().isoformat(),
                      "session_start": params["start"].isoformat(), "complete": True}
            for label, query in QUERIES.items():
                rows = [dict(row) for row in connection.execute(text(query + " LIMIT 1025"), params).mappings()]
                need(len(rows) <= MAX_ROWS, label + " exceeds complete proof bound")
                result[label] = rows
            terminal = [row for row in result["working_orders"] if local_terminal_proof(row)]
            result["terminal_local_rows"] = [dict(id=str(row["id"]), intent_id=str(row["intent_id"]),
                account=row["account"], symbol=row["symbol"], basis="bound_client_abort_or_broker_submit_rejection") for row in terminal]
            result["working_orders"] = [row for row in result["working_orders"] if row not in terminal]
            retired = {str(row["intent_id"]) for row in terminal}
            result["inflight_intents"] = [row for row in result["inflight_intents"]
                if not (str(row["id"]) in retired and row["status"] == "aborted")]
            for row in result["working_orders"]:
                # Keep identities and proof bits, not arbitrary intent metadata.
                row.pop("intent_payload", None)
            return result
    finally:
        engine.dispose()


def evaluate(receipt, now):
    blockers, operators, unknown = [], [], list(receipt.get("errors", []))
    sql = receipt.get("sql")
    if sql:
        for label in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents"):
            for row in sql[label]:
                if label in {"working_orders", "inflight_intents"} and str(row.get("status") or "").lower() not in LIVE:
                    unknown.append(label + " unknown/unproven status " + str(row.get("status")))
                else:
                    blockers.append(dict(kind=label, account=row["account"], symbol=row["symbol"], status=row.get("status")))
    try:
        if sql:
            need(sql["complete"] is True and datetime.fromisoformat(sql["session_start"]) == session_start(now),
                 "SQL census incomplete/wrong session")
            counts = {}
            for label in ("session_order_counts", "session_fill_counts"):
                for row in sql[label]:
                    key = row["account"], row["symbol"]
                    count = quantity(row["total"])
                    need(key[0] in ACCOUNTS and count > 0 and count == count.to_integral_value(), "session census malformed")
                    counts[key] = counts.get(key, 0) + count
        for account in ACCOUNTS:
            source = receipt.get("brokers", {}).get(account)
            if not source:
                unknown.append(account + " direct source missing")
                continue
            need(source.get("complete") is True and source.get("identity_bound") is True, "direct source incomplete/unbound")
            need(0 <= (now - datetime.fromisoformat(source["started_at"])).total_seconds() <= FRESH_SECONDS,
                 account + " direct source stale/future")
            for row in source["working_orders"]:
                blockers.append(dict(kind="broker_working_order", **row))
            for row in source["holdings"]:
                need(row["account"] == account, "foreign holding account")
                key = account, row["symbol"]
                if sql and counts.get(key, 0) == 0 and quantity(row["quantity"]) > 0 and quantity(row["short"]) == 0:
                    operators.append(dict(**row, session_orders=0, session_fills=0, basis="zero_orders_AND_zero_fills_not_net_zero"))
                else:
                    blockers.append(dict(kind="bot_or_unproven_broker_holding", **row))
        if sql:
            need(0 <= (now - datetime.fromisoformat(sql["observed_at"])).total_seconds() <= FRESH_SECONDS, "SQL snapshot stale/future")
        else:
            unknown.append("SQL snapshot missing")
    except Exception as exc:
        unknown.append(str(exc))
    receipt.update(blockers=blockers, operator_only_holdings=operators, unknown=unknown,
                   completed_at=now.isoformat(), rc=1 if blockers else 2 if unknown else 0)
    return receipt["rc"]


async def schwab_reads(adapter):
    need(adapter._adapter_refresh_enabled is False, "Schwab adapter refresh must be OFF")
    account = adapter.accounts_by_name.get(ACCOUNTS[0])
    need(account is not None, "Schwab account configuration absent")
    token = await adapter._get_access_token()
    need(adapter._access_token_expires_at is not None and datetime.now(UTC) < adapter._access_token_expires_at,
         "Schwab token expired/expiry unreadable; gate never refreshes")
    async def read(path, label="account mapping/positions"):
        # Deliberately bypass _authorized_request_json's retry/refresh path.
        status, _, body = await asyncio.wait_for(adapter._request_json("GET", path, access_token=token), 20)
        need(200 <= status < 300, "Schwab GET HTTP " + str(status) + " " + label)
        return bounded(body)

    started = datetime.now(UTC).isoformat()
    number = schwab_account_number(await read("/trader/v1/accounts/accountNumbers"), account.account_hash)
    base = "/trader/v1/accounts/" + quote(account.account_hash, safe="")
    holdings = schwab_positions(await read(base + "?fields=positions"), number)
    active, raw_counts = {}, {}
    # Filter each live state separately: an unfiltered capped day list is NOT a
    # complete working-order proof. Include children and fail on a capped reply.
    now = datetime.now(UTC)
    # PARTIAL_FILL is an adapter report spelling, not a valid Schwab status
    # filter (both PARTIAL_FILL/PARTIALLY_FILLED return HTTP400). Also scan the
    # complete session without a status filter; never hide partially filled rows.
    for status in sorted(adapter.ACCEPTED_STATUSES) + ["SESSION_ALL"]:
        start = session_start(now) if status == "SESSION_ALL" else now - timedelta(days=60)
        params = dict(maxResults=3000, fromEnteredTime=start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                      toEnteredTime=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
        if status != "SESSION_ALL":
            params["status"] = status
        query = urlencode(params)
        rows = await read(base + "/orders?" + query, label="orders status=" + status)
        need(isinstance(rows, list) and len(rows) < 3000, "Schwab filtered order list capped/unreadable")
        # working_rows has the tighter 1024 evidence bound.
        raw_counts[status] = len(rows)
        for row in working_rows(rows, ACCOUNTS[0], expected_id=number, schwab=True):
            prior = active.get(row["id"])
            need(prior is None or prior == row, "Schwab order changed during scan")
            active[row["id"]] = row
    return dict(started_at=started, identity_bound=True, complete=True,
                holdings=holdings, working_orders=list(active.values()), filtered_status_counts=raw_counts)


def webull_reads(adapter):
    from webull.trade.request.get_account_positions_request import AccountPositionsRequest
    from webull.trade.request.get_today_orders_request import TodayOrdersListRequest

    account = adapter.accounts_by_name.get(ACCOUNTS[1])
    need(account is not None and account.account_id, "Webull account configuration absent")
    started = datetime.now(UTC).isoformat()
    reader = SpacedClient(adapter._get_client())
    holdings = webull_holdings(webull_pages(reader, AccountPositionsRequest, account.account_id, "positions"))
    orders = webull_pages(reader, TodayOrdersListRequest, account.account_id, "orders")
    return dict(started_at=started, identity_bound=True, complete=True, holdings=holdings,
                working_orders=working_rows(orders, ACCOUNTS[1], expected_id=account.account_id),
                today_order_count=len(orders))


async def collect(config):
    from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
    from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter

    receipt = dict(started_at=datetime.now(UTC).isoformat(), brokers={}, errors=[])
    for account, action in ((ACCOUNTS[0], lambda: schwab_reads(SchwabBrokerAdapter(config))),
                            (ACCOUNTS[1], lambda: asyncio.to_thread(webull_reads, WebullBrokerAdapter(config)))):
        try:
            receipt["brokers"][account] = await asyncio.wait_for(action(), 110)
        except Exception as exc:
            traceback.print_exc(file=sys.stderr)
            receipt["errors"].append(account + ": " + type(exc).__name__ + ": " + str(exc))
    try:
        receipt["sql"] = sql_snapshot(config, datetime.now(UTC))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        receipt["errors"].append("SQL: " + type(exc).__name__ + ": " + str(exc))
    evaluate(receipt, datetime.now(UTC))
    return receipt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default="/etc/project-mai-tai/project-mai-tai.env")
    args = parser.parse_args()
    try:
        need(os.geteuid() == 0, "live gate requires root")
        from project_mai_tai.settings import Settings
        config = Settings(_env_file=args.env_file)
        need(config.schwab_adapter_token_refresh_enabled is False, "token refresh enabled; no read permitted")
        receipt = asyncio.run(collect(config))
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        receipt = dict(rc=2, blockers=[], unknown=[type(exc).__name__ + ": " + str(exc)])
    print(json.dumps(receipt, indent=2, default=str))
    return receipt["rc"]


if __name__ == "__main__":
    sys.exit(main())

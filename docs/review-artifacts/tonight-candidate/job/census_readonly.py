"""Explicit read-only census, not install authority. Exit 2 means UNKNOWN/STOP.

Run through SSH stdin as root/nice19 with PYTHONDONTWRITEBYTECODE=1 and
PYTHONPATH=/home/trader/project-mai-tai/src. No snapshot-batches, refresh grant,
order polling/realignment, persistence, Redis mutation, or service action.
Only the 14 independently retained ticket identities are recognized. No date
or phase filter is applied to the journal population. New identities STOP.
"""
import asyncio
import argparse
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import inspect
import json
from pathlib import Path
import subprocess
import sys
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from sqlalchemy import event, text
from sqlalchemy.orm import Session

from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.session import build_engine
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.settings import Settings

ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
MAX_ROWS = 64
MAX_BODY = 1_000_000
MAX_OUTPUT = 2_000_000
KNOWN = {
    "fbfd692d-ec1f-5a39-9e11-1a133abccc96": (ACCOUNTS[0], "APUS"),
    "bd6ac0b9-727c-500b-8581-aabdd95992d4": (ACCOUNTS[1], "APUS"),
    "a007716c-4b50-5759-a354-ddc5b8961154": (ACCOUNTS[0], "VEEA"),
    "faa55c1f-262b-52e2-adcc-280ab1e5e2ff": (ACCOUNTS[1], "VEEA"),
    "7c7c5afb-30ff-539f-8baa-4c2e585c6e12": (ACCOUNTS[0], "APUS"),
    "ee3d0d07-abea-5fe2-98c9-cace5c08d135": (ACCOUNTS[1], "APUS"),
    "ff6464ff-d65e-5657-8f47-1dc8d7b053c3": (ACCOUNTS[0], "RETO"),
    "ba108172-04f6-5659-892b-a0fc10d22b15": (ACCOUNTS[1], "RETO"),
    "c539a57f-6111-59ae-8b79-79b7fee1700a": (ACCOUNTS[0], "MI"),
    "4be7cb2d-406b-56db-b597-3783f6e956ff": (ACCOUNTS[1], "MI"),
    "a9eac442-d340-59ab-8c26-9c04d3453f1d": (ACCOUNTS[0], "SCKT"),
    "6fb89c93-a101-5200-b569-320acf5f4f85": (ACCOUNTS[1], "SCKT"),
    "889889cd-2de7-59ca-997f-fc546b728ec9": (ACCOUNTS[1], "SCKT"),
    "d86d5d38-200b-52b6-8147-6eecfe13698b": (ACCOUNTS[1], "SCKT"),
}
TERMINAL = {"CANCELLED", "CANCELED", "FILLED", "REJECTED", "EXPIRED", "REPLACED"}
REVIEWED_SOURCES = {
    "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
    "7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf",
}
# From 7823's committed tests/fixtures/rpgstuck1_startup_later.json, not this live pull.
FIXTURE_SHA256 = "21e0352a642f81c6f9e68ca51831d0aff470f057b387f4fb5900638d38650b87"
IMMUTABLE = {
    "fbfd692d-ec1f-5a39-9e11-1a133abccc96": "5acab4ab94a9163097818db2fc9ffd376f89d4a127f1100bba978842ab6ddcdc",
    "bd6ac0b9-727c-500b-8581-aabdd95992d4": "3c04897feb56dfcc349a2264342a7c5ab703af953c8148d887e0ccd97cdbe822",
    "a007716c-4b50-5759-a354-ddc5b8961154": "28675587ffe20887fdf51bb991ec1d6c312f8a8a90b5a7123d19d8668c7f4e47",
    "faa55c1f-262b-52e2-adcc-280ab1e5e2ff": "e8c4c81389645eb372254771f1f28ef09a686d4881d57035d1581ed5f85d8687",
    "7c7c5afb-30ff-539f-8baa-4c2e585c6e12": "3f101e27c3697c5d5834e29524e42bcb6a49c9cdf8868cf178b6ba45486c8737",
    "ee3d0d07-abea-5fe2-98c9-cace5c08d135": "af80f773a616677678945546e5c7b6136e520a57f41b0a3e2d76b55d620adea7",
    "ff6464ff-d65e-5657-8f47-1dc8d7b053c3": "ee068597cc71af1b8a1ab715e71ae057f3b0b573b9823626c0de20071f26f373",
    "ba108172-04f6-5659-892b-a0fc10d22b15": "d6f4e83ca6bc1dd6ccbcebd2dc2da54845bcf959c6cd5ed8c9e3d032a28ea672",
    "c539a57f-6111-59ae-8b79-79b7fee1700a": "705414f1befff668b442827f18f321d150e63531560fd4399cb465f77389e0e6",
    "4be7cb2d-406b-56db-b597-3783f6e956ff": "358384a950b2935623139768752478c0dff1efe11579a4ceeb852fba566cdb89",
    "a9eac442-d340-59ab-8c26-9c04d3453f1d": "617f6f46a33eefde049a5e937f263a7655839933e74859c9a79608aa3cee08cb",
    "6fb89c93-a101-5200-b569-320acf5f4f85": "f2eea79f47cc8ef0e906812640f4f09d41ce6ed4fbf17b66fc8090be24946f27",
    "889889cd-2de7-59ca-997f-fc546b728ec9": "ce7534201c37a74854282dc4318c16f33cef31a84e2c1f2bcaaab8d2dbaecef0",
    "d86d5d38-200b-52b6-8147-6eecfe13698b": "363e317e9de30110ea905f9b73183d55a583dd7f87b626f75d0fa4753bff56f7",
}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def immutable_digest(job):
    return digest({key: job[key] for key in ("old", "slot", "segment_id")})


def validate_immutable(key, job):
    if immutable_digest(job) != IMMUTABLE.get(str(key)):
        raise Stop("immutable old request/slot/segment differs from reviewed fixture: " + str(key))


def source_bound(result):
    root = Path("/home/trader/project-mai-tai")
    response = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
    if response.returncode or response.stdout.strip() not in REVIEWED_SOURCES:
        raise Stop("box source SHA outside reviewed baseline/candidate")
    module_path = Path(inspect.getfile(HandoffJournal)).resolve()
    if module_path != root / "src/project_mai_tai/oms/atr_reprice_handoff.py":
        raise Stop("HandoffJournal import source path not bound to box checkout")
    result["source"] = {"box_sha": response.stdout.strip(), "module": str(module_path),
                        "module_sha256": hashlib.sha256(module_path.read_bytes()).hexdigest()}


class Stop(Exception):
    pass


def stamp():
    return datetime.now(timezone.utc).isoformat()


def bounded(value, limit=MAX_BODY):
    size = len(json.dumps(value, default=str).encode())
    if size > limit:
        raise Stop("evidence byte bound exceeded")
    return size


def redacted(value):
    if isinstance(value, dict):
        return {key: "<redacted>" if key in {
            "accountNumber", "accountId", "account_id", "accountHash", "hashValue"
        } else redacted(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redacted(item) for item in value]
    return value


def rows(connection, query, parameters=None, limit=MAX_ROWS):
    data = [dict(row) for row in connection.execute(text(query), parameters or {}).mappings()]
    if len(data) > limit:
        raise Stop("SQL row overflow: " + query.split()[1])
    bounded(data)
    return data


def sql_census(config, result):
    engine = build_engine(config.database_url, connect_timeout_s=5, statement_timeout_ms=5000)

    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            result["transaction"] = rows(connection,
                "SELECT current_setting('transaction_read_only') readonly, "
                "current_setting('transaction_isolation') isolation, now() captured_at")
            total = connection.scalar(text("SELECT count(*) FROM dashboard_snapshots "
                                           "WHERE snapshot_type='atr_reprice_handoff'"))
            result["all_date_ticket_count"] = total
            if total > MAX_ROWS:
                raise Stop("all-date journal exceeds 64-row bound")
            # Invoke the actual unfiltered runtime reader only after its size is proven bounded.
            with Session(bind=connection, autoflush=False) as session:
                jobs = HandoffJournal(None).jobs(session=session)
            captured = rows(connection, "SELECT id,created_at,payload FROM dashboard_snapshots "
                "WHERE snapshot_type='atr_reprice_handoff' ORDER BY id LIMIT 65")
            if {str(key): value for key, value in jobs} != {
                    str(row["id"]): row["payload"] for row in captured} or len(jobs) != total:
                raise Stop("actual HandoffJournal.jobs differs from bounded all-date census")
            result["jobs"] = captured
            result["journal_digest_sha256"] = digest({str(key): value for key, value in jobs})
            result["reviewed_token_digest_sha256"] = digest(sorted(KNOWN))
            result["reviewed_fixture_sha256"] = FIXTURE_SHA256
            result["immutable_digests"] = {str(key): immutable_digest(job) for key, job in jobs}
            result["jobs_reader_sha256"] = hashlib.sha256(
                inspect.getsource(HandoffJournal.jobs).encode()).hexdigest()
            actual_ids = {str(key) for key, _ in jobs}
            result["outside_reviewed_14"] = sorted(actual_ids - set(KNOWN))
            result["missing_reviewed_14"] = sorted(set(KNOWN) - actual_ids)
            if actual_ids != set(KNOWN):
                raise Stop("outside/missing reviewed 14-ticket population: STOP")
            ids, clients, broker_ids, generations = set(), set(), set(), set()

            def keys(value):
                if isinstance(value, dict):
                    for key, item in value.items():
                        if isinstance(item, str) and item:
                            if key == "rpg_resting_generation":
                                generations.add(item)
                            if key in {"client_order_id", "target_client_order_id"}:
                                clients.add(item)
                            if key == "broker_order_id":
                                broker_ids.add(item)
                            if key in {"order_id", "old_order_id"}:
                                ids.add(item)
                        keys(item)
                elif isinstance(value, list):
                    for item in value:
                        keys(item)

            for key, job in jobs:
                validate_immutable(key, job)
                old = job.get("old") if isinstance(job, dict) else None
                if (not isinstance(old, dict) or
                        (old.get("broker_account_name"), old.get("symbol")) != KNOWN[str(key)] or
                        old.get("strategy_code") != "schwab_1m_v2" or
                        old.get("side") != "buy" or job.get("slot") not in {"first", "reclaim"} or
                        job.get("phase") not in {"refused", "held_unknown", "filled", "placed", "expired"} or
                        not isinstance(old.get("metadata"), dict) or
                        not old.get("client_order_id") or not isinstance(job.get("revision"), int)):
                    raise Stop("reviewed identity has untested/unreadable ticket shape: " + str(key))
                keys(job)
            if any(len(values) > 128 for values in (ids, clients, broker_ids, generations)):
                raise Stop("ticket identity key bound exceeded")
            parameters = {"ids": sorted(ids), "clients": sorted(clients),
                          "brokers": sorted(broker_ids), "generations": sorted(generations)}
            result["linked_orders"] = rows(connection,
                "SELECT b.id,b.client_order_id,b.broker_order_id,b.symbol,b.side,b.quantity,"
                "b.status,b.submitted_at,b.updated_at,b.payload,s.code strategy,a.name account "
                "FROM broker_orders b JOIN strategies s ON s.id=b.strategy_id "
                "JOIN broker_accounts a ON a.id=b.broker_account_id WHERE "
                "CAST(b.id AS text)=ANY(:ids) OR b.client_order_id=ANY(:clients) OR "
                "b.broker_order_id=ANY(:brokers) OR (s.code='schwab_1m_v2' AND "
                "a.name IN ('live:schwab_1m_v2','live:orb') AND lower(b.side)='buy' AND "
                "b.payload->>'rpg_resting_generation'=ANY(:generations)) "
                "ORDER BY b.submitted_at,b.id LIMIT 65", parameters)
            result["linked_intents"] = rows(connection,
                "SELECT t.id,t.created_at,t.status,t.symbol,t.side,t.intent_type,t.quantity,"
                "t.reason,t.payload,s.code strategy,a.name account FROM trade_intents t "
                "JOIN strategies s ON s.id=t.strategy_id JOIN broker_accounts a "
                "ON a.id=t.broker_account_id WHERE s.code='schwab_1m_v2' AND "
                "a.name IN ('live:schwab_1m_v2','live:orb') AND "
                "t.payload->'metadata'->>'rpg_resting_generation'=ANY(:generations) "
                "ORDER BY t.created_at,t.id LIMIT 65", parameters)
            result["linked_fills"] = rows(connection,
                "SELECT f.id,f.order_id,f.symbol,f.side,f.quantity,f.price,f.filled_at,"
                "s.code strategy,a.name account FROM fills f JOIN strategies s ON s.id=f.strategy_id "
                "JOIN broker_accounts a ON a.id=f.broker_account_id WHERE CAST(f.order_id AS text)"
                "=ANY(:ids) ORDER BY f.filled_at,f.id LIMIT 65",
                {"ids": [str(row["id"]) for row in result["linked_orders"]]})
            for label, table, time_column in (
                    ("nonterminal_orders", "broker_orders", "submitted_at"),
                    ("nonterminal_intents", "trade_intents", "created_at")):
                result[label] = rows(connection,
                    f"SELECT t.id,t.symbol,t.side,t.status,t.{time_column},a.name account "
                    f"FROM {table} t JOIN broker_accounts a ON a.id=t.broker_account_id "
                    "WHERE a.name IN ('live:schwab_1m_v2','live:orb') AND (t.status IS NULL OR "
                    "lower(t.status) NOT IN ('filled','rejected','cancelled','canceled','expired',"
                    "'replaced')) ORDER BY t.id LIMIT 65")
            if result["nonterminal_orders"] or result["nonterminal_intents"]:
                raise Stop("SQL nonterminal orders/intents present")
            result["schema_revision"] = rows(connection, "SELECT version_num FROM alembic_version LIMIT 3", limit=2)
            if result["schema_revision"] != [{"version_num": "20260916_0021"}]:
                raise Stop("pre-write schema is not exactly initial 0021")
            result["shape_classes"] = {str(key): value["phase"] for key, value in jobs}
            result["managed_schema"] = rows(connection,
                "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
                "WHERE table_schema='public' AND table_name='oms_managed_positions' "
                "ORDER BY ordinal_position LIMIT 65")
    finally:
        engine.dispose()


def fresh_schwab_get(adapter, token, path, reads):
    # No authorized-request retry, refresh grant or adapter poll is invoked.
    request = Request(adapter.base_url + path, method="GET", headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urlopen(request, timeout=20) as response:
            status, raw = response.status, response.read(MAX_BODY + 1)
    except HTTPError as exc:
        status, raw = exc.code, exc.read(MAX_BODY + 1)
    account_hash = adapter.accounts_by_name[ACCOUNTS[0]].account_hash
    reads.append({"account": ACCOUNTS[0], "method": "GET", "read_at": stamp(),
                  "path": path.replace(quote(account_hash, safe=""), "<configured-account>"),
                  "path_sha256": hashlib.sha256(path.encode()).hexdigest(),
                  "http": status, "response_bytes": len(raw)})
    if len(raw) > MAX_BODY:
        raise Stop("Schwab GET exceeds 1 MB bound")
    if status != 200:
        raise Stop("Schwab GET HTTP " + str(status) + "; no retry/refresh")
    return json.loads(raw)


def webull_pages(adapter, request_type, label, reads):
    account = adapter.accounts_by_name.get(ACCOUNTS[1])
    if account is None:
        raise Stop("Webull account missing")
    output, pages, cursor, seen = [], [], None, set()
    for page in range(1, 7):
        request = request_type()
        request.set_account_id(account.account_id)
        request.set_page_size(50)
        if cursor:
            request.set_last_client_order_id(cursor)
        response = adapter._get_client().get_response(request)
        code = int(getattr(response, "status_code", 0))
        if not 200 <= code < 300:
            raise Stop("Webull " + label + " HTTP " + str(code))
        body = adapter._body(response)
        size = bounded(body)
        reads.append({"account": ACCOUNTS[1], "method": "GET", "read_at": stamp(),
                      "path": "/trade/orders/list-" + label, "page_size": 50,
                      "cursor": cursor, "http": code, "response_bytes": size,
                      "response_bytes_kind": "serialized SDK-decoded JSON"})
        if not isinstance(body, dict) or not isinstance(body.get("orders"), list):
            raise Stop("Webull " + label + " unreadable envelope")
        batch = body["orders"]
        if len(batch) > 50 or any(not isinstance(row, dict) for row in batch):
            raise Stop("Webull " + label + " invalid page")
        output.extend(batch)
        pages.append({"page": page, "http": code, "body": redacted(body)})
        if len(output) > 256:
            raise Stop("Webull " + label + " exceeds 256-row bound")
        markers = [body[key] for key in ("has_next", "hasNext") if key in body]
        if not markers or any(type(value) is not bool for value in markers) or len(set(markers)) != 1:
            raise Stop("Webull " + label + " completion marker unproven")
        if markers[0] is False:
            return {"complete": True, "count": len(output), "pages": pages}
        if not batch:
            raise Stop("Webull " + label + " empty incomplete page")
        cursor = batch[-1].get("client_order_id") or batch[-1].get("clientOrderId")
        if not cursor or cursor in seen:
            raise Stop("Webull " + label + " missing/repeated cursor")
        seen.add(cursor)
    raise Stop("Webull " + label + " page overflow")


async def brokers(config, result):
    if config.schwab_adapter_token_refresh_enabled:
        raise Stop("Schwab adapter refresh enabled: refuses before broker reads")
    adapter = SchwabBrokerAdapter(config)
    account = adapter.accounts_by_name.get(ACCOUNTS[0])
    if account is None:
        raise Stop("Schwab account missing")
    token = await adapter._get_access_token()  # Pure-reader mode verified above.
    now = datetime.now(timezone.utc)
    start, end = now - timedelta(hours=12), now + timedelta(hours=1)
    path = (f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders?"
            f"fromEnteredTime={start.strftime('%Y-%m-%dT%H:%M:%S.000Z')}&"
            f"toEnteredTime={end.strftime('%Y-%m-%dT%H:%M:%S.000Z')}&maxResults=500")
    reads = result["direct_requests"] = []
    body = await asyncio.to_thread(fresh_schwab_get, adapter, token, path, reads)
    if not isinstance(body, list) or len(body) >= 500:
        raise Stop("Schwab OMS-sync order list unreadable/truncated")
    active = []

    def walk(node, depth=0):
        if not isinstance(node, dict) or depth > 12:
            raise Stop("Schwab order-tree shape unreadable")
        legs, children = node.get("orderLegCollection", []), node.get("childOrderStrategies", [])
        if not isinstance(legs, list) or not isinstance(children, list):
            raise Stop("Schwab order tree collections unreadable")
        status = str(node.get("status") or "").upper()
        if legs:
            if status not in TERMINAL | adapter.ACCEPTED_STATUSES:
                raise Stop("Schwab order status unrecognized")
            if status not in TERMINAL:
                active.append(redacted(node))
        elif not children:
            raise Stop("Schwab empty order node")
        for child in children:
            walk(child, depth + 1)

    for node in body:
        walk(node)
    result["schwab_orders"] = {"read_at": stamp(), "scope": "OMS-sync 12h past / 1h future",
        "all_time_working_absence_proven": False, "count": len(body),
        "active": active, "body": redacted(body)}
    from webull.trade.request.get_open_orders_request import OpenOrdersListRequest
    from webull.trade.request.get_today_orders_request import TodayOrdersListRequest
    from webull.trade.request.get_order_detail_request import OrderDetailRequest
    webull = WebullBrokerAdapter(config)
    result["webull_open"] = await asyncio.to_thread(webull_pages, webull, OpenOrdersListRequest, "open", reads)
    result["webull_today"] = await asyncio.to_thread(webull_pages, webull, TodayOrdersListRequest, "today", reads)
    result["exact_linked_parents"] = []
    for row in result["linked_orders"]:
        if not row["broker_order_id"] or not row["client_order_id"]:
            raise Stop("linked order lacks exact broker/client identity")
        if row["account"] == ACCOUNTS[0]:
            detail_path = (f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders/"
                           + quote(row["broker_order_id"], safe=""))
            detail = await asyncio.to_thread(fresh_schwab_get, adapter, token, detail_path, reads)
            if not isinstance(detail, dict) or str(detail.get("orderId")) != row["broker_order_id"]:
                raise Stop("Schwab exact linked parent identity mismatch")
            legs = detail.get("orderLegCollection")
            if (not isinstance(legs, list) or len(legs) != 1 or
                    legs[0].get("instrument", {}).get("symbol") != row["symbol"] or
                    legs[0].get("instruction") != "BUY" or
                    Decimal(str(detail.get("quantity"))) != Decimal(str(row["quantity"]))):
                raise Stop("Schwab exact parent symbol/side/quantity mismatch")
            walk(detail)
            status = str(detail.get("status", "")).upper()
        elif row["account"] == ACCOUNTS[1]:
            request = OrderDetailRequest()
            request.set_account_id(webull.accounts_by_name[ACCOUNTS[1]].account_id)
            request.set_client_order_id(row["client_order_id"])
            evidence = {"account": ACCOUNTS[1], "method": "GET", "path": "/trade/order/detail",
                        "client_order_id": row["client_order_id"], "read_at": stamp(), "http": "UNKNOWN"}
            reads.append(evidence)
            try:
                response = await asyncio.to_thread(webull._get_client().get_response, request)
            except Exception as exc:
                evidence["error_class"] = type(exc).__name__
                raise Stop("Webull exact parent GET unreadable: " + row["client_order_id"]) from None
            code = int(getattr(response, "status_code", 0))
            evidence["http"] = code
            if not 200 <= code < 300:
                raise Stop("Webull exact parent GET HTTP " + str(code))
            detail = webull._body(response)
            result["last_webull_exact_body"] = redacted(detail)
            size = bounded(detail)
            evidence.update(response_bytes=size, response_bytes_kind="serialized SDK-decoded JSON")
            if not isinstance(detail, dict) or detail.get("order_id") != row["broker_order_id"]:
                raise Stop("Webull exact parent broker identity mismatch")
            items = detail.get("items")
            if not isinstance(items, list) or len(items) != 1:
                raise Stop("Webull exact parent items unreadable")
            item = items[0]
            if detail.get("client_order_id") != row["client_order_id"]:
                raise Stop("Webull exact parent client identity mismatch")
            if (not isinstance(item, dict) or item.get("symbol") != row["symbol"] or
                    item.get("side") != "BUY" or
                    Decimal(str(item.get("qty"))) != Decimal(str(row["quantity"]))):
                raise Stop("Webull exact parent symbol/side/quantity mismatch")
            status = str(item.get("order_status", "")).upper()
        else:
            raise Stop("linked order foreign account")
        result["exact_linked_parents"].append({"account": row["account"], "symbol": row["symbol"],
            "client_order_id": row["client_order_id"], "broker_order_id": row["broker_order_id"],
            "status": status, "body": redacted(detail)})
        if status not in TERMINAL:
            raise Stop("linked exact parent is not terminal")
    result["reviewed_linked_parent_terminal_proof"] = True
    result["broker_read_completed_at"] = stamp()
    if active or result["webull_open"]["count"]:
        raise Stop("fresh direct broker working-order census nonempty")


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require-reviewed", action="store_true",
                        help="explicit strict reviewed-population census (also the default)")
    args = parser.parse_args()
    result = {"started_at": stamp(), "readonly": True, "rc": 2, "install_authority": False}
    result["require_reviewed"] = args.require_reviewed
    try:
        config = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
        source_bound(result)
        sql_census(config, result)
        await asyncio.wait_for(brokers(config, result), 150)
        after = {}
        sql_census(config, after)
        result["journal_digest_after_sha256"] = after["journal_digest_sha256"]
        if result["journal_digest_sha256"] != result["journal_digest_after_sha256"]:
            raise Stop("journal changed during direct reads: repeat census before writes")
        result["rc"] = 0
        result["disposition"] = "bounded census only; not a strict-flat or install gate"
    except Stop as exc:
        result["stop"] = str(exc)
    except Exception as exc:
        # Arbitrary HTTP/DB exceptions may contain protected URLs or credentials.
        result["stop"] = "unreadable evidence: " + type(exc).__name__
    result["completed_at"] = stamp()
    try:
        bounded(result, MAX_OUTPUT)
        print(json.dumps(result, default=str, indent=2))
    except Stop:
        print(json.dumps({"rc": 2, "stop": "global output bound exceeded", "readonly": True}))
        return 2
    return result["rc"]


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

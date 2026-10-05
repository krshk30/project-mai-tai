"""Standing MI/NXL install allowance. No token refresh or DB/Redis write.

Exit 0: proven clear; 1: measured blocker; 2: unreadable/unknown. Run as root
with PYTHONDONTWRITEBYTECODE=1. Broker calls are fresh GETs, never cached reads.
"""
import asyncio
import argparse
import copy
import json
import sys
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from zoneinfo import ZoneInfo
from urllib.request import urlopen

from sqlalchemy import event, text
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings
from project_mai_tai.deploy_preflight import evaluate_live_deploy_preflight, parse_datetime

ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
MAX_BODY_BYTES = 1_000_000
ALLOWANCES = {"MI": Decimal("180"), "NXL": Decimal("2")}
FRESH_SECONDS = 120


def quantity(value):
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("unreadable quantity") from exc
    if not result.is_finite():
        raise ValueError("nonfinite quantity")
    return result


def bounded_body(body):
    size = len(json.dumps(body, separators=(",", ":")).encode())
    if size > MAX_BODY_BYTES:
        raise ValueError("broker body exceeds 1 MB evidence budget")
    return size


def fresh(value, now, label):
    stamp = parse_datetime(str(value))
    if stamp is None or not 0 <= (now - stamp).total_seconds() <= FRESH_SECONDS:
        raise ValueError(label + " stale/unreadable/future")
    return stamp


def standing_allowance(result, overview, run, findings, heartbeat, now):
    """Validate original evidence, then adjust only an in-memory gate input copy."""
    for key in ("broker_holdings", "managed_rows", "virtual_rows", "account_rows",
                "working_orders", "inflight_intents"):
        if result[key]:
            raise ValueError("standing allowance void: " + key)
    for account in ACCOUNTS:
        fresh(result["direct_read_started_at"][account], now, account + " direct read")
    stamps = result["account_stamps"]
    if len(stamps) != 2 or {row["account"] for row in stamps} != set(ACCOUNTS):
        raise ValueError("account-position stamps absent/ambiguous")
    for row in stamps:
        fresh(row["updated_at"], now, row["account"] + " stored positions")
    checked = fresh(run["summary"].get("checked_at"), now, "SQL reconciliation")
    if run["status"] != "completed" or run["completed_at"] is None:
        raise ValueError("SQL reconciliation incomplete")
    fresh(run["completed_at"], now, "SQL reconciliation completion")
    latest = overview["reconciliation"]["latest_run"]
    overview_checked = fresh(latest["summary"].get("checked_at"), now, "overview reconciliation")
    cached_run = result["overview_sql_run"]
    if cached_run["status"] != "completed" or cached_run["summary"] != latest["summary"]:
        raise ValueError("overview run not bound to exact SQL run")
    fresh(cached_run["completed_at"], now, "overview SQL run completion")
    # Overview is cached; accept different fresh run times only when every
    # other summary field and the complete finding identities agree.
    if (latest["status"] != "completed"
            or {key: value for key, value in latest["summary"].items() if key != "checked_at"}
            != {key: value for key, value in run["summary"].items() if key != "checked_at"}):
        raise ValueError("overview/SQL reconciliation mismatch")
    symbols = set()
    for finding in findings:
        symbol = finding["symbol"]
        payload = finding["payload"]
        if (symbol not in ALLOWANCES or symbol in symbols
                or finding["finding_type"] != "position_quantity_mismatch"
                or finding["severity"] != "critical"
                or payload.get("fingerprint") != "position-quantity:" + ACCOUNTS[0] + ":" + symbol
                or payload.get("account_name") != ACCOUNTS[0]
                or quantity(payload.get("net_fill_balance")) != ALLOWANCES[symbol]
                or any(quantity(payload.get(key)) != 0 for key in (
                    "account_quantity", "virtual_quantity", "managed_quantity"))):
            raise ValueError("finding not an exact standing allowance: " + str(symbol))
        symbols.add(symbol)
    count = len(findings)
    summary = run["summary"]
    if (summary.get("total_findings") != count or summary.get("critical_findings") != count
            or summary.get("warning_findings") != 0 or summary.get("info_findings") != 0):
        raise ValueError("SQL finding count mismatch")
    cached_findings = result["overview_sql_findings"]
    identity_keys = ("fingerprint", "account_name", "net_fill_balance",
                     "account_quantity", "virtual_quantity", "managed_quantity")
    def identity(row):
        return (row["symbol"], row["finding_type"], row["severity"],
                *(str(row["payload"][key]) for key in identity_keys))
    if sorted(map(identity, cached_findings)) != sorted(map(identity, findings)):
        raise ValueError("cached/full SQL finding identities differ")
    overview_findings = overview["reconciliation"]["findings"]
    if (len(overview_findings) != count or sorted(
            (row["symbol"], row["finding_type"], row["severity"], row["title"]) for row in overview_findings)
            != sorted((row["symbol"], row["finding_type"], row["severity"], row["payload"]["title"])
                      for row in cached_findings)):
        raise ValueError("overview findings mismatch")
    services = [row for row in overview["services"] if row["service_name"] == "reconciler"]
    if len(services) != 1:
        raise ValueError("reconciler service missing/ambiguous")
    service = services[0]
    observed = fresh(heartbeat["observed_at"], now, "published reconciler heartbeat")
    if observed < overview_checked or parse_datetime(service.get("observed_at_raw")) != observed:
        raise ValueError("reconciler heartbeat not bound to current reconciliation")
    expected_status = "degraded" if count else "healthy"
    details = heartbeat["payload"].get("details")
    if (heartbeat["status"] != expected_status
            or service.get("effective_status", service.get("status")) != expected_status
            or service.get("details") != details or not isinstance(details, dict)
            or set(details) != {"total_findings", "critical_findings", "run_status", "cutover_confidence"}
            or details["total_findings"] != str(count)
            or details["critical_findings"] != str(count)
            or details["run_status"] != "completed"
            or details["cutover_confidence"] != str(summary["cutover_confidence"])):
        raise ValueError("reconciler degradation not attributable to exact findings")
    for row in result["net_bot_fills"]:
        if (row["account"] != ACCOUNTS[0] or row["symbol"] != "MI"
                or quantity(row["net"]) != ALLOWANCES["MI"] or "MI" not in symbols):
            raise ValueError("current-session net fills outside MI standing allowance")
    audit = ["[STANDING-ALLOWANCE] fingerprint=position-quantity:" + ACCOUNTS[0]
             + ":" + symbol + " balance=" + str(ALLOWANCES[symbol])
             + " account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh"
             + " sql_checked_at=" + checked.isoformat()
             + " overview_checked_at=" + overview_checked.isoformat() for symbol in sorted(symbols)]
    if result["net_bot_fills"]:
        audit.append("[STANDING-ALLOWANCE] current_session_net_fill account=" + ACCOUNTS[0]
                     + " symbol=MI balance=180 broker_flat=fresh")
    adjusted = copy.deepcopy(overview)
    adjusted["reconciliation"]["latest_run"]["summary"]["total_findings"] = 0
    adjusted["reconciliation"]["latest_run"]["summary"]["critical_findings"] = 0
    if count:
        for row in adjusted["services"]:
            if row["service_name"] == "reconciler":
                row["effective_status"] = "healthy"
    return adjusted, audit


def schwab_account_number(mapping, account_hash):
    if not isinstance(mapping, list) or len(mapping) > 64:
        raise ValueError("Schwab account-number mapping unreadable/oversized")
    if any(not isinstance(row, dict) or not isinstance(row.get("hashValue"), str)
           or not isinstance(row.get("accountNumber"), str) or not row["accountNumber"].isdigit()
           for row in mapping):
        raise ValueError("Schwab account-number mapping malformed")
    matches = [row["accountNumber"] for row in mapping if row["hashValue"] == account_hash]
    if len(matches) != 1:
        raise ValueError("Schwab configured account not uniquely mapped")
    return matches[0]


def schwab_holdings(response, expected_number):
    body = response.get("securitiesAccount")
    if (not isinstance(body, dict) or body.get("accountNumber") != expected_number
            or not isinstance(body.get("currentBalances"), dict)):
        raise ValueError("Schwab positions account identity/envelope unproven")
    rows = body.get("positions", [])
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Schwab positions malformed/oversized")
    holdings = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("instrument"), dict):
            raise ValueError("Schwab holding malformed")
        symbol = row["instrument"].get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("Schwab holding symbol absent")
        if "longQuantity" not in row and "shortQuantity" not in row:
            raise ValueError("Schwab holding quantity absent")
        long, short = quantity(row.get("longQuantity", 0)), quantity(row.get("shortQuantity", 0))
        if long or short:
            holdings.append([ACCOUNTS[0], symbol.upper(), str(long), str(short)])
    return holdings


def webull_positions(adapter):
    from webull.trade.request.get_account_positions_request import AccountPositionsRequest

    account = adapter.accounts_by_name.get(ACCOUNTS[1])
    if account is None:
        raise ValueError("Webull account mapping missing")
    cursor, seen, holdings, sizes = None, set(), [], []
    for page in range(1, 21):
        request = AccountPositionsRequest()
        request.set_account_id(account.account_id)
        request.set_page_size(50)
        if cursor:
            request.set_last_instrument_id(cursor)
        response = adapter._get_client().get_response(request)
        if not 200 <= int(getattr(response, "status_code", 0)) < 300:
            raise ValueError("Webull positions HTTP failure")
        body = adapter._body(response)
        if not isinstance(body, dict):
            raise ValueError("Webull positions malformed")
        sizes.append(bounded_body(body))
        if "holdings" in body and "positions" in body and body["holdings"] != body["positions"]:
            raise ValueError("Webull holdings/positions conflict")
        rows = body.get("holdings", body.get("positions"))
        if not isinstance(rows, list) or len(rows) > 50:
            raise ValueError("Webull holdings absent/oversized")
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("Webull holding malformed")
            instrument = row.get("instrument")
            symbol = row.get("symbol") or row.get("ticker") or (
                instrument.get("symbol") if isinstance(instrument, dict) else None)
            raw = next((row[key] for key in ("quantity", "qty", "position", "shares")
                        if key in row), None)
            if not isinstance(symbol, str) or not symbol or raw is None:
                raise ValueError("Webull holding symbol/quantity absent")
            qty = quantity(raw)
            if qty:
                holdings.append([ACCOUNTS[1], symbol.upper(), str(qty)])
        markers = [body[key] for key in ("has_next", "hasNext") if key in body]
        if not markers or any(type(marker) is not bool for marker in markers) or len(set(markers)) != 1:
            raise ValueError("Webull pagination malformed")
        more = markers[0]
        if not more:
            return holdings, sizes
        if not rows or page == 20:
            raise ValueError("Webull pagination incomplete")
        cursor = adapter._first_str(rows[-1], "instrument_id", "instrumentId")
        if not cursor or cursor in seen:
            raise ValueError("Webull pagination cursor invalid")
        seen.add(cursor)
    raise ValueError("Webull pagination cap reached")


async def collect(service_target=None):
    config = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    if config.schwab_adapter_token_refresh_enabled:
        raise ValueError("adapter token refresh enabled; read-only gate refuses")
    schwab = SchwabBrokerAdapter(config)
    account = schwab.accounts_by_name.get(ACCOUNTS[0])
    if account is None:
        raise ValueError("Schwab account mapping missing")
    direct_started = {ACCOUNTS[0]: datetime.now(timezone.utc).isoformat()}
    map_status, _, mapping = await asyncio.wait_for(schwab._authorized_request_json(
        "GET", "/trader/v1/accounts/accountNumbers"), 20)
    if not 200 <= map_status < 300:
        raise ValueError("Schwab account-number mapping HTTP failure")
    mapping_size = bounded_body(mapping)
    expected_number = schwab_account_number(mapping, account.account_hash)
    status, _, response = await asyncio.wait_for(schwab._authorized_request_json(
        "GET", f"/trader/v1/accounts/{quote(account.account_hash, safe='')}?fields=positions"), 20)
    if not 200 <= status < 300 or not isinstance(response, dict):
        raise ValueError("Schwab positions HTTP failure/malformed body")
    size = bounded_body(response)
    holdings = schwab_holdings(response, expected_number)
    direct_started[ACCOUNTS[1]] = datetime.now(timezone.utc).isoformat()
    webull, sizes = await asyncio.wait_for(
        asyncio.to_thread(webull_positions, WebullBrokerAdapter(config)), 60)
    holdings.extend(webull)
    now = datetime.now(ZoneInfo("America/New_York"))
    start = datetime.combine(now.date(), time(4), now.tzinfo)
    if now < start:
        start -= timedelta(days=1)
    engine = build_engine(config.database_url, connect_timeout_s=5, statement_timeout_ms=5000)
    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    queries = {
        "managed_rows": "SELECT broker_account_name account,symbol,current_quantity quantity FROM oms_managed_positions WHERE broker_account_name IN (:schwab,:webull) AND status='open' ORDER BY 1,2 LIMIT 65",
        "virtual_rows": "SELECT a.name account,v.symbol,v.quantity FROM virtual_positions v JOIN broker_accounts a ON a.id=v.broker_account_id WHERE a.name IN (:schwab,:webull) AND v.quantity<>0 ORDER BY 1,2 LIMIT 65",
        "account_rows": "SELECT a.name account,p.symbol,p.quantity FROM account_positions p JOIN broker_accounts a ON a.id=p.broker_account_id WHERE a.name IN (:schwab,:webull) AND p.quantity<>0 ORDER BY 1,2 LIMIT 65",
        "account_stamps": "SELECT a.name account,max(p.updated_at) updated_at FROM broker_accounts a LEFT JOIN account_positions p ON p.broker_account_id=a.id WHERE a.name IN (:schwab,:webull) GROUP BY a.name ORDER BY a.name LIMIT 3",
        "fill_balances": "SELECT a.name account,f.symbol,sum(CASE WHEN upper(f.side)='BUY' THEN f.quantity WHEN upper(f.side)='SELL' THEN -f.quantity END) net,count(*) total,count(CASE WHEN upper(f.side) IN ('BUY','SELL') THEN 1 END) known FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.name IN (:schwab,:webull) AND f.filled_at>=:start GROUP BY 1,2 ORDER BY 1,2 LIMIT 65",
        "working_orders": "SELECT a.name account,b.symbol,b.status,b.client_order_id,b.broker_order_id FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id WHERE a.name IN (:schwab,:webull) AND (b.status IS NULL OR lower(b.status) NOT IN ('cancelled','canceled','filled','rejected','expired','replaced')) ORDER BY b.submitted_at LIMIT 65",
        "inflight_intents": "SELECT a.name account,t.symbol,t.status FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN (:schwab,:webull) AND lower(t.status) IN ('pending','submitted','accepted') ORDER BY t.created_at LIMIT 65",
    }
    # One bounded overview plus a repeatable-read SQL snapshot; no Redis access.
    with urlopen("http://127.0.0.1:8100/api/overview", timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("overview exceeds 2 MB reply bound")
    overview = json.loads(raw)
    result = {"as_of_et": now.isoformat(), "fill_session_start_et": start.isoformat(),
              "broker_holdings": holdings, "schwab_response_bytes": size,
              "schwab_mapping_response_bytes": mapping_size, "schwab_identity_bound": True,
              "webull_response_bytes": sizes, "manual_exceptions": [],
              "direct_read_started_at": direct_started, "overview_bytes": len(raw)}
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            found = connection.execute(text("SELECT name FROM broker_accounts WHERE name IN (:schwab,:webull)"),
                {"schwab": ACCOUNTS[0], "webull": ACCOUNTS[1]}).scalars().all()
            if set(found) != set(ACCOUNTS):
                raise ValueError("database account mapping incomplete")
            for label, query in queries.items():
                data = [dict(row) for row in connection.execute(text(query), {
                    "schwab": ACCOUNTS[0], "webull": ACCOUNTS[1],
                    "start": start.astimezone(timezone.utc)}).mappings()]
                if len(data) > 64:
                    raise ValueError("flat query exceeds 64-row proof bound")
                result[label] = data
            runs = [dict(row) for row in connection.execute(text(
                "SELECT id,status,completed_at,summary FROM reconciliation_runs ORDER BY started_at DESC LIMIT 6")).mappings()]
            if not runs:
                raise ValueError("reconciliation run absent")
            run = runs[0]
            findings = [dict(row) for row in connection.execute(text(
                "SELECT symbol,finding_type,severity,payload FROM reconciliation_findings WHERE reconciliation_run_id=:id ORDER BY symbol LIMIT 65"),
                {"id": run["id"]}).mappings()]
            if len(findings) > 64:
                raise ValueError("findings exceed 64-row bound")
            matches = [row for row in runs if row["summary"].get("checked_at") == overview["reconciliation"]["latest_run"]["summary"].get("checked_at")]
            if len(matches) != 1:
                raise ValueError("overview run absent/ambiguous in bounded SQL run set")
            result["overview_sql_run"] = matches[0]
            cached_findings = [dict(row) for row in connection.execute(text(
                "SELECT symbol,finding_type,severity,payload FROM reconciliation_findings WHERE reconciliation_run_id=:id ORDER BY symbol LIMIT 65"),
                {"id": matches[0]["id"]}).mappings()]
            if len(cached_findings) > 64:
                raise ValueError("cached findings exceed 64-row bound")
            result["overview_sql_findings"] = cached_findings
            services = [row for row in overview["services"] if row["service_name"] == "reconciler"]
            if len(services) != 1 or not services[0].get("instance_name"):
                raise ValueError("overview reconciler instance absent/ambiguous")
            # This service publishes to Redis, not service_heartbeats in SQL.
            # The overview carries its raw event timestamp and unmodified details.
            published = services[0]
            heartbeat = {"status": published.get("raw_status", published.get("status")),
                         "observed_at": published.get("observed_at_raw"),
                         "payload": {"details": published.get("details")}}
    finally:
        engine.dispose()
    if any(row["total"] != row["known"] or row["net"] is None
           for row in result["fill_balances"]):
        raise ValueError("unknown fill side/net")
    result["net_bot_fills"] = [row for row in result.pop("fill_balances")
                               if quantity(row["net"]) != 0]
    checked_now = datetime.now(timezone.utc)
    result["proof_completed_at_utc"] = checked_now.isoformat()
    original_failures = (evaluate_live_deploy_preflight(overview, service_target=service_target,
                         now=checked_now) if service_target else [])
    result["original_general_failures"] = original_failures
    result["allowance_findings"] = findings
    result["allowance_run"] = run
    result["allowance_heartbeat"] = heartbeat
    adjusted, audit = standing_allowance(result, overview, run, findings, heartbeat, checked_now)
    for line in audit:
        print(line)
    result["allowance_audit"] = audit
    blocked = [key for key in ("broker_holdings", "managed_rows", "virtual_rows",
                               "account_rows", "working_orders", "inflight_intents") if result[key]]
    warnings = []
    remaining = (evaluate_live_deploy_preflight(adjusted, service_target=service_target,
                 now=checked_now, warnings=warnings) if service_target else [])
    result["remaining_general_failures"] = remaining
    result["general_warnings"] = warnings
    if remaining:
        blocked.append("general_preflight")
    result["blockers"] = blocked
    result["rc"] = 1 if blocked else 0
    print(json.dumps(result, indent=2, default=str))
    return result["rc"]


if __name__ == "__main__":
    try:
        parser = argparse.ArgumentParser()
        parser.add_argument("--service", choices=("oms", "strategy"))
        sys.exit(asyncio.run(collect(parser.parse_args().service)))
    except Exception as exc:
        print("UNKNOWN strict-flat: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)

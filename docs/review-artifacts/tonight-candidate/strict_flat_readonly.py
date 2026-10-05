"""October 5 candidate flat gate. No manual exception, token refresh or DB write.

Exit 0: proven clear; 1: measured blocker; 2: unreadable/unknown. Run as root
with PYTHONDONTWRITEBYTECODE=1. Broker calls are fresh GETs, never cached reads.
"""
import asyncio
import json
import sys
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

from sqlalchemy import event, text
from project_mai_tai.broker_adapters.schwab import SchwabBrokerAdapter
from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings
from webull.trade.request.get_account_positions_request import AccountPositionsRequest

ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
MAX_BODY_BYTES = 1_000_000


def quantity(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite quantity")
    return result


def bounded_body(body):
    size = len(json.dumps(body, separators=(",", ":")).encode())
    if size > MAX_BODY_BYTES:
        raise ValueError("broker body exceeds 1 MB evidence budget")
    return size


def webull_positions(adapter):
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
        more = body.get("has_next", body.get("hasNext", False))
        if type(more) is not bool:
            raise ValueError("Webull pagination malformed")
        if not more:
            return holdings, sizes
        if not rows or page == 20:
            raise ValueError("Webull pagination incomplete")
        cursor = adapter._first_str(rows[-1], "instrument_id", "instrumentId")
        if not cursor or cursor in seen:
            raise ValueError("Webull pagination cursor invalid")
        seen.add(cursor)
    raise ValueError("Webull pagination cap reached")


async def collect():
    config = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    if config.schwab_adapter_token_refresh_enabled:
        raise ValueError("adapter token refresh enabled; read-only gate refuses")
    schwab = SchwabBrokerAdapter(config)
    account = schwab.accounts_by_name.get(ACCOUNTS[0])
    if account is None:
        raise ValueError("Schwab account mapping missing")
    status, _, response = await asyncio.wait_for(schwab._authorized_request_json(
        "GET", f"/trader/v1/accounts/{quote(account.account_hash, safe='')}?fields=positions"), 20)
    if not 200 <= status < 300 or not isinstance(response, dict):
        raise ValueError("Schwab positions HTTP failure/malformed body")
    size = bounded_body(response)
    body = response.get("securitiesAccount", response)
    if not isinstance(body, dict):
        raise ValueError("Schwab account malformed")
    rows = body.get("positions", [])
    if not isinstance(rows, list) or len(rows) > 1000:
        raise ValueError("Schwab positions malformed/oversized")
    if "positions" not in body and not (
            isinstance(body.get("currentBalances"), dict) and body.get("accountNumber")):
        raise ValueError("Schwab empty positions not proven by account identity")
    holdings = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("instrument"), dict):
            raise ValueError("Schwab holding malformed")
        symbol = row["instrument"].get("symbol")
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("Schwab holding symbol absent")
        long, short = quantity(row.get("longQuantity", 0)), quantity(row.get("shortQuantity", 0))
        # Do not hide offsetting long and short exposure behind a net zero.
        if long or short:
            holdings.append([ACCOUNTS[0], symbol.upper(), str(long), str(short)])
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
        "fill_balances": "SELECT a.name account,f.symbol,sum(CASE WHEN upper(f.side)='BUY' THEN f.quantity WHEN upper(f.side)='SELL' THEN -f.quantity END) net,count(*) total,count(CASE WHEN upper(f.side) IN ('BUY','SELL') THEN 1 END) known FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id WHERE a.name IN (:schwab,:webull) AND f.filled_at>=:start GROUP BY 1,2 ORDER BY 1,2 LIMIT 65",
    }
    result = {"as_of_et": now.isoformat(), "fill_session_start_et": start.isoformat(),
              "broker_holdings": holdings, "schwab_response_bytes": size,
              "webull_response_bytes": sizes, "manual_exceptions": []}
    try:
        with engine.connect() as connection:
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
    finally:
        engine.dispose()
    if any(row["total"] != row["known"] or row["net"] is None
           for row in result["fill_balances"]):
        raise ValueError("unknown fill side/net")
    result["net_bot_fills"] = [row for row in result.pop("fill_balances")
                               if quantity(row["net"]) != 0]
    blocked = [key for key in ("broker_holdings", "managed_rows", "virtual_rows",
                               "account_rows", "net_bot_fills") if result[key]]
    result["blockers"] = blocked
    result["rc"] = 1 if blocked else 0
    print(json.dumps(result, indent=2, default=str))
    return result["rc"]


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(collect()))
    except Exception as exc:
        print("UNKNOWN strict-flat: " + type(exc).__name__ + ": " + str(exc), file=sys.stderr)
        sys.exit(2)

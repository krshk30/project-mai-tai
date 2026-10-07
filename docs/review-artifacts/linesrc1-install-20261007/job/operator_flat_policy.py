"""[codex] Exact fresh zero-session-record operator holdings; never net-zero proof."""
from datetime import datetime, time, timedelta
from decimal import Decimal
import re
from zoneinfo import ZoneInfo

from project_mai_tai.deploy_preflight import parse_datetime

ACCOUNTS = ("live:schwab_1m_v2", "live:orb")
ET = ZoneInfo("America/New_York")


def number(value):
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("nonfinite operator quantity")
    return result


def current(value, now, label):
    stamp = parse_datetime(str(value))
    if stamp is None or not 0 <= (now - stamp).total_seconds() <= 120:
        raise ValueError("operator " + label + " stale/unreadable/future")
    return stamp


def session_start(now):
    local = now.astimezone(ET)
    start = datetime.combine(local.date(), time(4), ET)
    return start if local >= start else start - timedelta(days=1)


def prove(result, findings, now):
    holdings, stored = result["broker_holdings"], result["account_rows"]
    if not holdings and not stored:
        return []
    if any(result[key] for key in ("managed_rows", "virtual_rows", "working_orders", "inflight_intents")):
        raise ValueError("operator exemption cannot waive open bot work")
    if (result.get("session_order_census_complete") is not True
            or result.get("session_fill_census_complete") is not True
            or parse_datetime(str(result.get("fill_session_start_et"))) != session_start(now)):
        raise ValueError("operator session census incomplete/wrong session")
    for label in ("as_of_et", "sql_snapshot_at_utc", "proof_completed_at_utc"):
        current(result.get(label), now, label)
    if result.get("schwab_identity_bound") is not True or result.get("webull_identity_bound") is not True:
        raise ValueError("operator direct account identity unproven")
    if set(result["direct_read_started_at"]) != set(ACCOUNTS):
        raise ValueError("operator direct account scope incomplete")
    for account in ACCOUNTS:
        current(result["direct_read_started_at"][account], now, account + " direct read")
    stamps = result["account_stamps"]
    if len(stamps) != 2 or {row["account"] for row in stamps} != set(ACCOUNTS):
        raise ValueError("operator stored account freshness incomplete")
    for row in stamps:
        current(row["updated_at"], now, row["account"] + " positions")
    direct, book = {}, {}
    for row in holdings:
        account, symbol = row[:2]
        key = account, symbol
        if (account not in ACCOUNTS or not isinstance(symbol, str)
                or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) or key in direct):
            raise ValueError("operator direct holding duplicate/foreign/malformed")
        if account == ACCOUNTS[0]:
            if len(row) != 4 or number(row[2]) < 0 or number(row[3]) < 0 or (number(row[2]) and number(row[3])):
                raise ValueError("operator long/short identity ambiguous")
            qty = number(row[2]) - number(row[3])
        else:
            if len(row) != 3:
                raise ValueError("operator Webull holding malformed")
            qty = number(row[2])
        if qty == 0:
            raise ValueError("operator direct holding zero/ambiguous")
        direct[key] = qty
    for row in stored:
        key = row["account"], row["symbol"]
        if key in book or key not in direct or number(row["quantity"]) != direct[key]:
            raise ValueError("operator stored/direct position mismatch")
        for label in ("updated_at", "source_updated_at"):
            current(row.get(label), now, "position " + label)
        book[key] = number(row["quantity"])
    if set(book) != set(direct):
        raise ValueError("operator complete stored/direct holdings mismatch")
    counts = {}
    for label in ("session_order_counts", "fill_balances"):
        seen = set()
        for row in result[label]:
            key = row["account"], row["symbol"]
            total = number(row["total"])
            if key in seen or key[0] not in ACCOUNTS or total <= 0 or total != total.to_integral_value():
                raise ValueError("operator session count duplicate/foreign/unreadable")
            seen.add(key)
            counts[key] = counts.get(key, Decimal(0)) + total
    proofs = []
    for key, qty in direct.items():
        if counts.get(key, Decimal(0)) != 0:
            raise ValueError("operator holding has session bot order/fill: " + str(key))
        matches = [row for row in findings if row["symbol"] == key[1]
                   and row["payload"].get("account_name") == key[0]]
        if len(matches) > 1:
            raise ValueError("operator matching position finding ambiguous")
        title = ("position present at broker with no matching fill balance of ours for "
                 + key[1] + " - not ours, taking no action")
        if matches:
            finding = matches[0]
            payload = finding["payload"]
        if matches and (finding["finding_type"] != "position_quantity_mismatch" or finding["severity"] != "info"
                or payload.get("fingerprint") != "position-quantity:" + key[0] + ":" + key[1]
                or payload.get("direction") != "broker_only_manual"
                or payload.get("ownership") != "manual_not_ours" or payload.get("title") != title
                or number(payload.get("account_quantity")) != qty
                or any(number(payload.get(label)) != 0 for label in (
                    "virtual_quantity", "managed_quantity", "our_quantity", "net_fill_balance"))
                or any(number(payload.get(label)) != abs(qty) for label in ("quantity_delta", "fill_delta"))):
            raise ValueError("operator finding is not the exact matching info position finding")
        proofs.append(dict(account=key[0], symbol=key[1], quantity=str(qty),
                           session_bot_orders=0, session_bot_fills=0, session_start_et=session_start(now).isoformat(),
                           expected_fingerprint="position-quantity:" + key[0] + ":" + key[1],
                           matching_finding_present=bool(matches), ownership_basis="zero_session_orders_AND_fills"))
    return proofs

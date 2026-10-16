"""Read-only disposable-copy preview. Live execute stays blocked pending evidence contracts."""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from ops.weekmaint.policy import FULL_TABLES, Refused, encoded, retirement_plan, session_anchor

BOUND = 200_000
TERMINALS = "'filled','cancelled','canceled','rejected','expired','aborted','completed','superseded'"


def clone_url(raw):
    url = make_url(raw)
    if (url.get_backend_name() != "postgresql" or not re.fullmatch(r"(?:replay_|qualification_)[A-Za-z0-9_]+", url.database or "")
            or set(url.query) - {"host", "port"}):
        raise Refused("explicit_disposable_copy_only")
    host, port = url.query.get("host", url.host), int(url.query.get("port", url.port or 0))
    if not 1024 <= port <= 65535 or port == 5432:
        raise Refused("nondefault_local_port_required")
    if host not in {"127.0.0.1", "::1"}:
        if not isinstance(host, str) or not host.startswith("/"):
            raise Refused("local_copy_only")
        if Path("/private/tmp") not in Path(host).resolve().parents:
            raise Refused("private_tmp_socket_only")
    return url, port


def inventory(connection, now):
    """Only source reads; full raw row text retained for future exact archives."""
    live = "a.name LIKE 'live:%' AND s.execution_mode='live'"
    specs = {
        "trade_intents": f"""SELECT to_jsonb(t)::text AS source_json_text,a.name AS account,
            t.created_at AS existence_at FROM trade_intents t JOIN strategies s ON s.id=t.strategy_id
            JOIN broker_accounts a ON a.id=t.broker_account_id WHERE {live} AND (t.status IS NULL OR t.status NOT IN({TERMINALS}))""",
        "broker_orders": f"""SELECT to_jsonb(t)::text AS source_json_text,a.name AS account,
            COALESCE(t.submitted_at,CASE WHEN p.strategy_id=t.strategy_id AND p.broker_account_id=t.broker_account_id
              AND p.symbol=t.symbol THEN p.created_at END) AS existence_at
            FROM broker_orders t JOIN strategies s ON s.id=t.strategy_id
            JOIN broker_accounts a ON a.id=t.broker_account_id LEFT JOIN trade_intents p ON p.id=t.intent_id
            WHERE {live} AND (t.status IS NULL OR t.status NOT IN({TERMINALS}))""",
        "oms_managed_positions": f"""SELECT to_jsonb(t)::text AS source_json_text,a.name AS account,NULL AS existence_at
            FROM oms_managed_positions t JOIN strategies s ON s.code=t.strategy_code
            JOIN broker_accounts a ON a.name=t.broker_account_name WHERE {live} AND t.status='open'""",
        "virtual_positions": f"""SELECT to_jsonb(t)::text AS source_json_text,a.name AS account,NULL AS existence_at
            FROM virtual_positions t JOIN strategies s ON s.id=t.strategy_id
            JOIN broker_accounts a ON a.id=t.broker_account_id WHERE {live} AND t.quantity<>0""",
    }
    rows, counts = [], {}
    for table, sql in specs.items():
        found = connection.execute(text(sql + " ORDER BY t.id LIMIT :bound"), {"bound": BOUND + 1}).mappings().all()
        if len(found) > BOUND:
            raise Refused("copy_inventory_bound_no_truncation")
        counts[table] = len(found)
        for value in found:
            rows.append({"table": table, "row": json.loads(value["source_json_text"], parse_float=Decimal),
                         "source_json_text": value["source_json_text"], "account": value["account"],
                         "existence_at": value["existence_at"], "live_scope_verified": True})
    accounts = connection.execute(text("SELECT name,provider FROM broker_accounts WHERE name LIKE 'live:%' ORDER BY name")).all()
    midnight = now.astimezone(ZoneInfo("America/New_York")).replace(hour=0, minute=0, second=0, microsecond=0)
    today = connection.execute(text(f"""SELECT count(*) FROM trade_intents t JOIN strategies s ON s.id=t.strategy_id
        JOIN broker_accounts a ON a.id=t.broker_account_id WHERE {live} AND t.created_at>=:today
        AND (t.status IS NULL OR t.status NOT IN({TERMINALS}))"""), {"today": midnight}).scalar_one()
    managed = connection.execute(text("""SELECT
        count(*) FILTER (WHERE t.current_quantity IS NULL OR t.current_quantity<>0),
        count(*) FILTER (WHERE t.current_quantity=0)
        FROM oms_managed_positions t JOIN strategies s ON s.code=t.strategy_code
        JOIN broker_accounts a ON a.name=t.broker_account_name
        WHERE a.name LIKE 'live:%' AND s.execution_mode='live' AND t.status='open'""")).one()
    # 10:0x says ALL bot orders/intents, including terminal rows. BrokerOrder
    # has no created_at; use its submitted/exact-bound intent existence anchor
    # and updated_at (which also records creation), refusing missing anchors.
    recent_orders = connection.execute(text(f"""SELECT count(*) FROM broker_orders t
        JOIN strategies s ON s.id=t.strategy_id JOIN broker_accounts a ON a.id=t.broker_account_id
        LEFT JOIN trade_intents p ON p.id=t.intent_id AND p.strategy_id=t.strategy_id
          AND p.broker_account_id=t.broker_account_id AND p.symbol=t.symbol
        WHERE {live} AND (t.updated_at IS NULL OR t.updated_at>=:since
          OR COALESCE(t.submitted_at,p.created_at) IS NULL
          OR COALESCE(t.submitted_at,p.created_at)>=:since)"""),
        {"since": now - timedelta(hours=2)}).scalar_one()
    recent_intents = connection.execute(text(f"""SELECT count(*) FROM trade_intents t
        JOIN strategies s ON s.id=t.strategy_id JOIN broker_accounts a ON a.id=t.broker_account_id
        WHERE {live} AND (t.created_at IS NULL OR t.updated_at IS NULL
          OR t.created_at>=:since OR t.updated_at>=:since)"""),
        {"since": now - timedelta(hours=2)}).scalar_one()
    return rows, {"live_accounts": dict(accounts), "open_managed_nonzero_or_unknown": managed[0],
                  "open_managed_zero": managed[1],
                  "nonterminal_live_intents_today": today, "candidate_source_counts": counts,
                  "recent_or_unknown_bot_order_intent_count": recent_orders + recent_intents,
                  "nonterminal_bot_order_rows": [item for item in rows if item["table"] == "broker_orders"]}


def preview(raw, now):
    url, port = clone_url(raw)
    engine = create_engine(url, connect_args={"connect_timeout": 3, "options":
        "-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=1000 -c timezone=UTC"})
    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection, connection.begin():
            identity = connection.execute(text("SELECT current_database(),current_setting('port'),"
                "current_setting('transaction_read_only'),inet_server_addr()::text,current_setting('server_version_num')")).one()
            if (identity[0] != url.database or int(identity[1]) != port or identity[2] != "on"
                    or identity[3] not in {None, "127.0.0.1", "::1"}
                    or not 160000 <= int(identity[4]) < 170000):
                raise Refused("local_readonly_copy_identity_failed")
            rows, facts = inventory(connection, now)
            candidates, unknown = retirement_plan(rows, now)
            return {"scope": "COPY_ONLY_NOT_LIVE_BROKER_OR_BOOT_PROOF", "database": identity[0],
                "readonly": True, "writes": 0, "facts": facts, "unresolved_rows": unknown,
                "session_anchor": session_anchor(now), "archive_candidates": [item.__dict__ for item in candidates],
                "gate": "UNKNOWN_NO_BROKER_INSTALL_WRITER_SEC_EVIDENCE", "would_execute": False,
                "full_plan": ["all gates + current SEC FULL success", "durable exact writer inventory + stop",
                    "fresh broker/DB recheck", "archive original rows+terminalization ONE transaction+CSV",
                    "VACUUM ANALYZE all tables outside transaction", f"disk-gated VACUUM FULL {sorted(FULL_TABLES)}",
                    "journal30d", "security-only approved window", "conditional reboot marker+single boot verifier"],
                "dashboard_snapshots_retention": "NONE pending reader/type approval"}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--copy-dsn")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--boot-verify", action="store_true")
    parser.add_argument("--force-test", action="store_true")
    args = parser.parse_args()
    if args.execute or args.boot_verify:
        print(json.dumps({"status": "BLOCKED_NOT_INSTALLED_SOURCE_STAGE",
            "reason": "live_census_writer_install_package_and_boot_backend_review_pending"}))
        return 78
    if not args.dry_run or not args.copy_dsn:
        parser.error("source-stage CLI supports only --dry-run --copy-dsn disposable local copy")
    try:
        value = preview(args.copy_dsn, datetime.now(UTC))
        value["harness_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        print(encoded(value).decode())  # MAIN redirects privately; contains original account/row data.
        return 2  # A copy plan with UNKNOWN broker gates can NEVER be a live GO receipt.
    except Exception as error:
        print(json.dumps({"status": "COPY_PLAN_REFUSED", "exception_type": type(error).__name__}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

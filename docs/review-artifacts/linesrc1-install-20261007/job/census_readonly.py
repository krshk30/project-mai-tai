"""[codex] Bounded all-date ticket phases and current install activity. No repair."""
from datetime import datetime, timezone
import json
import sys

from sqlalchemy import event, text
from sqlalchemy.orm import Session
from project_mai_tai.db.session import build_engine
from project_mai_tai.oms.atr_reprice_handoff import HandoffJournal
from project_mai_tai.settings import Settings
from ticket_inventory import require_idle

MAX_ROWS = 1024
MAX_BYTES = 4_000_000
ACCOUNTS = ("live:schwab_1m_v2", "live:orb")


def capture(connection, since=None):
    total, size = connection.execute(text("SELECT count(*),coalesce(sum(octet_length(payload::text)),0) "
        "FROM dashboard_snapshots WHERE snapshot_type='atr_reprice_handoff'")).one()
    if total > MAX_ROWS or size > MAX_BYTES:
        raise ValueError("all-date ticket evidence exceeds1024/4MB bound")
    with Session(bind=connection, autoflush=False) as session:
        jobs = HandoffJournal(None).jobs(session=session)
    rows = [dict(id=str(key), payload=payload) for key, payload in jobs]
    if len(rows) != total or len(json.dumps(rows, default=str).encode()) > MAX_BYTES:
        raise ValueError("journal count/byte bound differs")
    result = dict(at_utc=datetime.now(timezone.utc).isoformat(), rows=rows,
                  inventory=require_idle(rows), row_count_informational=total,
                  clears_unknown_ownership=False)
    if since:
        queries = {
            "buys": "SELECT b.id FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id "
                    "WHERE a.name IN (:schwab,:webull) AND lower(b.side)='buy' AND b.submitted_at>=:since LIMIT 1",
            "buy_intents": "SELECT t.id FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id "
                    "WHERE a.name IN (:schwab,:webull) AND lower(t.side)='buy' AND t.created_at>=:since LIMIT 1",
            "buy_fills": "SELECT f.id FROM fills f JOIN broker_accounts a ON a.id=f.broker_account_id "
                    "WHERE a.name IN (:schwab,:webull) AND upper(f.side)='BUY' AND f.filled_at>=:since LIMIT 1",
        }
        params = dict(schwab=ACCOUNTS[0], webull=ACCOUNTS[1], since=since)
        activity = {key: connection.execute(text(query), params).first() is not None
                    for key, query in queries.items()}
        if any(activity.values()):
            raise ValueError("startup buy activity present")
        result["activity"] = activity
    return result


def main():
    engine = build_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url,
                          connect_timeout_s=5, statement_timeout_ms=5000)

    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")

    try:
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            result = capture(connection, sys.argv[1] if len(sys.argv) == 2 else None)
        print(json.dumps(result, sort_keys=True, default=str))
        return 0
    except ValueError as exc:
        print("BLOCK ticket census: " + str(exc), file=sys.stderr)
        return 1
    except Exception as exc:
        print("UNKNOWN ticket census: " + type(exc).__name__, file=sys.stderr)
        return 2
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only historical evidence: at most two Webull order-detail requests.

No cancel/place/replace request is constructed. No Redis access. Run via stdin
on the box; stdout is the redacted evidence, never credentials.
"""
import json
import sys
import time
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.settings import Settings


def redact(value):
    if isinstance(value, dict):
        return {key: ("REDACTED" if key in {"account_id", "accountNumber", "tag"}
                      else redact(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main():
    settings = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    dsn = settings.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5,
                        options="-c default_transaction_read_only=on "
                        "-c statement_timeout=10000 -c lock_timeout=1000") as conn:
        counts = conn.execute("""
            SELECT a.name AS account, e.event_type, count(*)
            FROM broker_order_events e JOIN broker_orders o ON o.id=e.order_id
            JOIN broker_accounts a ON a.id=o.broker_account_id
            JOIN strategies s ON s.id=o.strategy_id
            WHERE s.code='schwab_1m_v2' AND o.side='buy'
              AND e.event_at >= '2026-09-08T00:00:00-04:00'
              AND a.name IN ('live:schwab_1m_v2','live:orb')
              AND e.event_type='partially_filled'
            GROUP BY a.name,e.event_type
        """).fetchall()
        harm = conn.execute("""
            SELECT a.name AS account, count(*) AS cancelled_buy_orders,
                   count(*) FILTER (WHERE EXISTS (
                       SELECT 1 FROM fills f WHERE f.order_id=o.id
                   )) AS cancelled_orders_with_booked_fills
            FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id
            JOIN strategies s ON s.id=o.strategy_id
            WHERE s.code='schwab_1m_v2' AND o.side='buy' AND o.status='cancelled'
              AND o.submitted_at >= '2026-09-08T00:00:00-04:00'
              AND a.name IN ('live:schwab_1m_v2','live:orb')
            GROUP BY a.name
        """).fetchall()
        if "--counts-only" in sys.argv:
            print(json.dumps({"captured_at": datetime.now(UTC), "harm_check": harm,
                              "partial_event_counts_since_sep08": counts}, default=str))
            return
        rows = []
        for status in ("cancelled", "filled"):
            rows.extend(conn.execute("""
                SELECT o.client_order_id,o.broker_order_id,o.symbol,o.quantity,o.status,
                       o.submitted_at,o.updated_at
                FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id
                JOIN strategies s ON s.id=o.strategy_id
                WHERE s.code='schwab_1m_v2' AND a.name='live:orb' AND o.side='buy'
                  AND o.submitted_at >= '2026-09-08T00:00:00-04:00' AND o.status=%s
                ORDER BY o.updated_at DESC LIMIT 1
            """, (status,)).fetchall())
    adapter = WebullBrokerAdapter(settings)
    account = adapter.accounts_by_name["live:orb"]
    try:
        from webull.trade.request.get_order_detail_request import OrderDetailRequest
    except ImportError:
        from webull.trade.request.v2.get_order_detail_request import OrderDetailRequest
    output = []
    for row in rows:
        request = OrderDetailRequest()
        request.set_account_id(account.account_id)
        request.set_client_order_id(row["client_order_id"])
        started = datetime.now(UTC)
        clock = time.monotonic()
        try:
            response = adapter._get_client().get_response(request)
            captured = {"http_status": adapter._response_status(response),
                        "body": redact(adapter._body(response))}
        except Exception as exc:
            captured = {"error_type": type(exc).__name__}
        output.append({"db_order": row, "read_started": started,
                       "read_completed": datetime.now(UTC),
                       "read_wall_ms": (time.monotonic() - clock) * 1000, **captured})
        time.sleep(2.1)
    print(json.dumps({"source": "historical exact-client-id Webull order-detail GET",
                      "partial_event_counts_since_sep08": counts,
                      "records": output}, default=str, sort_keys=True))


if __name__ == "__main__":
    main()

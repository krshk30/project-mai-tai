"""Bounded, read-only capture of the October 5 durable hand-off tickets."""

import json
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

from project_mai_tai.settings import Settings


def main():
    engine = create_engine(
        Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url
    )
    result = {"read_at": datetime.now(timezone.utc).isoformat()}
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        conn.execute(text("SET LOCAL statement_timeout='8s'"))
        jobs = [
            dict(row)
            for row in conn.execute(text(
                "SELECT id, created_at, payload FROM dashboard_snapshots "
                "WHERE snapshot_type='atr_reprice_handoff' "
                "AND created_at >= '2026-10-05T04:00:00Z' "
                "ORDER BY created_at LIMIT 65"
            )).mappings()
        ]
        if len(jobs) > 64:
            raise RuntimeError("ticket capture exceeded the declared 64-row bound")
        result["jobs"] = jobs
        order_ids = set()
        client_ids = set()
        def collect(payload):
            for key, value in payload.items():
                if key.endswith("order_id") and value:
                    order_ids.add(str(value))
                if "client" in key and value and isinstance(value, str):
                    client_ids.add(value)
                if isinstance(value, dict):
                    collect(value)

        for job in jobs:
            collect(job["payload"])
        result["order_keys"] = sorted(order_ids)
        result["client_keys"] = sorted(client_ids)
        result["orders"] = [
            dict(row)
            for row in conn.execute(text(
                "SELECT b.id, b.client_order_id, b.broker_order_id, b.symbol, "
                "b.submitted_at, b.updated_at, b.side, b.quantity, b.status, "
                "b.payload, s.code strategy, a.name account "
                "FROM broker_orders b JOIN strategies s ON s.id=b.strategy_id "
                "JOIN broker_accounts a ON a.id=b.broker_account_id "
                "WHERE CAST(b.id AS text)=ANY(:ids) "
                "OR b.client_order_id=ANY(:clients) "
                "OR (s.code='schwab_1m_v2' AND a.name='live:orb' "
                "AND b.side='buy' AND b.payload->>'rpg_resting_generation' IN "
                "('cf1c5bef-1928-49aa-91e2-506bf44478be', "
                "'233fccff-aea6-4607-9fbc-a1737adf1781', "
                "'5ba6122b-7e8a-4d3a-afe1-bc741f1f96cd')) "
                "ORDER BY b.submitted_at LIMIT 65"
            ), {"ids": sorted(order_ids), "clients": sorted(client_ids)}).mappings()
        ]
        if len(result["orders"]) > 64:
            raise RuntimeError("order capture exceeded the declared 64-row bound")
        result["deferred_intents"] = [
            dict(row)
            for row in conn.execute(text(
                "SELECT t.id, t.created_at, t.status, t.symbol, t.quantity, t.reason, "
                "t.payload, a.name account, s.code strategy "
                "FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id "
                "JOIN strategies s ON s.id=t.strategy_id "
                "WHERE s.code='schwab_1m_v2' AND a.name='live:orb' "
                "AND t.intent_type='open' AND t.side='buy' "
                "AND t.created_at >= '2026-10-05T04:00:00Z' "
                "AND t.payload->'metadata'->>'rpg_resting_generation' IN "
                "('cf1c5bef-1928-49aa-91e2-506bf44478be', "
                "'233fccff-aea6-4607-9fbc-a1737adf1781', "
                "'5ba6122b-7e8a-4d3a-afe1-bc741f1f96cd') "
                "ORDER BY t.created_at LIMIT 65"
            )).mappings()
        ]
        if len(result["deferred_intents"]) > 64:
            raise RuntimeError("intent capture exceeded the declared 64-row bound")
        conn.rollback()
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()

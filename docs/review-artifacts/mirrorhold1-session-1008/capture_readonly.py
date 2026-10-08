"""Bounded PostgreSQL/log capture over SSH stdin; no remote files or broker calls."""
import datetime as dt
import json
import subprocess


KEYS = {
    "fanout_segment_id", "fanout_slot_id", "fanout_leg", "fanout_source",
    "fanout_slot", "fanout_attempt_id", "resting_entry", "order_type",
    "stop_price", "limit_price", "reference_price", "entry_price", "cw_entry_slot",
    "rpg_resting_generation", "webull_mirror_generation_id", "mirrorhold_token",
    "webull_deferred_resubmit", "webull_local_no_wire", "webull_wire_submitted_at_utc",
    "webull_shape_market_price", "webull_shape_market_at_utc", "webull_shape_market_source",
    "mirrorhold_id", "mirrorhold_dispatch_generation", "reason", "reject_reason",
    "target_client_order_id", "webull_error_code", "webull_broker_filled_time",
}


def sql(query):
    result = subprocess.run(
        ["sudo", "-n", "-u", "postgres", "psql", "-X", "-At", "-d", "project_mai_tai",
         "-c", "BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; "
         "SET LOCAL lock_timeout='2s'; " + query + "; ROLLBACK"],
        capture_output=True, text=True, cwd="/tmp", check=True,
    )
    return [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]


limits = {}


def bounded(name, query, limit):
    rows = sql(query + f" LIMIT {limit + 1}) t")
    limits[name] = {"limit": limit, "returned": len(rows), "truncated": len(rows) > limit,
                    "null_strategy_identity": sum(row.get("strategy") is None for row in rows)
                    if name in {"orders", "intents", "historical_intents"} else None}
    return rows


def metadata(payload):
    return {k: v for k, v in (payload or {}).items() if k in KEYS}


scope = "a.name IN ('live:orb','live:schwab_1m_v2') AND o.side='buy' AND o.symbol IN ('AIXI','FLYE')"
orders = bounded("orders", "SELECT row_to_json(t) FROM (SELECT o.*,a.name AS account,s.code AS strategy "
             "FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id "
             "LEFT JOIN strategies s ON s.id=o.strategy_id WHERE " + scope +
             " ORDER BY o.submitted_at NULLS LAST,o.id", 200)
for order in orders:
    order["payload"] = metadata(order["payload"])
intents = bounded("intents", "SELECT row_to_json(t) FROM (SELECT i.*,a.name AS account,s.code AS strategy "
              "FROM trade_intents i JOIN broker_accounts a ON a.id=i.broker_account_id "
              "LEFT JOIN strategies s ON s.id=i.strategy_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') "
              "AND i.symbol IN ('AIXI','FLYE') AND i.created_at>='2026-10-08T13:34:00Z' "
              "AND i.created_at<'2026-10-08T13:57:00Z' ORDER BY i.created_at", 100)
historical_intents = bounded("historical_intents", "SELECT row_to_json(t) FROM (SELECT i.*,a.name AS account,s.code AS strategy "
              "FROM trade_intents i JOIN broker_orders o ON o.intent_id=i.id "
              "JOIN broker_accounts a ON a.id=o.broker_account_id "
              "LEFT JOIN strategies s ON s.id=i.strategy_id WHERE " + scope +
              " ORDER BY i.created_at,i.id", 200)
for intent in [*intents, *historical_intents]:
    payload = intent["payload"]
    intent["payload"] = {k: v for k, v in payload.items()
                         if k in {"event_id", "produced_at", "source_service"}} | {
                             "metadata": metadata(payload.get("metadata"))}
audits = bounded("audits", "SELECT row_to_json(t) FROM (SELECT e.* FROM broker_order_events e "
             "JOIN broker_orders o ON o.id=e.order_id JOIN broker_accounts a ON a.id=o.broker_account_id "
             "WHERE " + scope + " ORDER BY e.event_at,e.id", 1200)
for audit in audits:
    audit["payload"] = {k: v for k, v in audit["payload"].items()
                        if k in {"client_order_id", "reason", "status"}} | {
                            "metadata": metadata(audit["payload"].get("metadata"))}
fills = bounded("fills", "SELECT row_to_json(t) FROM (SELECT f.id,f.order_id,f.strategy_id,f.broker_account_id,"
                "f.symbol,f.side,f.quantity,f.price,f.filled_at FROM fills f "
                "JOIN broker_orders o ON o.id=f.order_id JOIN broker_accounts a ON a.id=o.broker_account_id "
                "WHERE " + scope + " ORDER BY f.filled_at,f.id", 300)
census = bounded("prior_working_or_null", "SELECT row_to_json(t) FROM (SELECT a.name AS account,o.symbol,o.status,o.time_in_force,"
             "o.submitted_at,o.client_order_id,o.payload->>'fanout_segment_id' AS segment "
             "FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id "
             "WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND o.side='buy' "
             "AND (o.submitted_at IS NULL OR (o.submitted_at<'2026-10-08T08:00:00Z' "
             "AND o.status NOT IN ('filled','rejected','cancelled','canceled','expired','aborted'))) "
             "ORDER BY o.submitted_at NULLS LAST", 100)
logs = subprocess.run(
    ["sudo", "-n", "grep", "-nE", "2026-10-08.*(13:35|13:55|14:00).*(AIXI|FLYE)",
     "/var/log/project-mai-tai/oms.log"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()
print(json.dumps({"captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                  "source": "mai-tai-vps bounded READ ONLY PostgreSQL; OMS file grep",
                  "metadata_projection": sorted(KEYS), "orders": orders, "intents": intents,
                  "audits": audits, "prior_working_or_null": census, "logs": logs,
                  "fills": fills,
                  "historical_intents": historical_intents,
                  "limits": limits, "complete": not any(v["truncated"] for v in limits.values())}, indent=2))

"""Read-only context for the October 5 deployment review, not deploy authority."""
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen

import redis
from sqlalchemy import event, text
from project_mai_tai.db.session import build_engine
from project_mai_tai.settings import Settings


def command(*args):
    row = subprocess.run(args, capture_output=True, text=True, timeout=20, check=True)
    if len(row.stdout.encode()) > 100_000:
        raise ValueError("administrative reply exceeds 100 KB")
    return row.stdout.strip()


def main():
    repo = Path("/home/trader/project-mai-tai")
    config = Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env")
    result = {"as_of_utc": datetime.now(timezone.utc).isoformat(),
              "box_sha": command("git", "-C", str(repo), "rev-parse", "HEAD"),
              "dirty": command("git", "-C", str(repo), "status", "--porcelain"),
              "import_path": str(Path(__import__("project_mai_tai").__file__).resolve())}
    with urlopen("http://127.0.0.1:8100/api/overview", timeout=20) as response:
        raw = response.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("overview reply exceeds 2 MB")
    overview = json.loads(raw)
    result["overview_bytes"] = len(raw)
    result["reconciliation"] = overview.get("reconciliation")
    result["overview_counts"] = overview.get("counts")
    result["overview_errors"] = overview.get("errors")
    result["services"] = [{key: row.get(key) for key in (
        "service_name", "status", "effective_status", "observed_at_raw", "observed_at")}
        for row in overview.get("services", [])]
    engine = build_engine(config.database_url, connect_timeout_s=5, statement_timeout_ms=5000)
    @event.listens_for(engine, "begin")
    def readonly(connection):
        connection.exec_driver_sql("SET TRANSACTION READ ONLY")
    queries = {
        "latest_findings": "SELECT finding_type,severity,symbol,payload FROM reconciliation_findings WHERE reconciliation_run_id=(SELECT id FROM reconciliation_runs ORDER BY started_at DESC LIMIT 1) ORDER BY symbol LIMIT 65",
        "nonterminal_orders": "SELECT a.name account,b.symbol,b.status,b.side,b.client_order_id,b.broker_order_id FROM broker_orders b JOIN broker_accounts a ON a.id=b.broker_account_id WHERE a.name IN ('live:schwab_1m_v2','live:orb') AND lower(b.status) NOT IN ('cancelled','canceled','filled','rejected','expired','replaced') ORDER BY b.submitted_at LIMIT 65",
        "inflight_intents": "SELECT a.name account,t.symbol,t.status,t.reason FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN ('live:schwab_1m_v2','live:orb') AND lower(t.status) IN ('pending','submitted','accepted') ORDER BY t.created_at LIMIT 65",
        "hold_census": "SELECT snapshot_type,payload->>'account_name' account,payload->>'symbol' symbol,payload->>'phase' phase,count(*) n FROM dashboard_snapshots WHERE snapshot_type IN ('atr_reprice_handoff','oms_webull_mirror_price_hold') GROUP BY 1,2,3,4 ORDER BY 1,2,3,4 LIMIT 65",
        "schema_revision": "SELECT version_num FROM alembic_version LIMIT 2",
    }
    try:
        with engine.connect() as connection:
            for key, query in queries.items():
                rows = [dict(row) for row in connection.execute(text(query)).mappings()]
                if len(rows) > 64:
                    raise ValueError("DB context exceeds 64-row proof bound")
                result[key] = rows
    finally:
        engine.dispose()
    client = redis.Redis.from_url(config.redis_url, decode_responses=True,
                                  socket_timeout=5, socket_connect_timeout=5)
    prefix = config.redis_stream_prefix
    result["redis_before"] = {"evicted_keys": client.info("stats")["evicted_keys"],
                              "used_memory": client.info("memory")["used_memory"]}
    key = prefix + ":market-data-subscription-owners"
    if client.hlen(key) != 7:
        raise ValueError("owners hash missing/unknown fields")
    owners = ("strategy-engine", "schwab-1m-v2", "orb", "orb-schwab", "momentum-paper",
              "_migration_complete", "_last_applied_id")
    if sum(client.hstrlen(key, field) for field in owners) > 100_000:
        raise ValueError("owners hash exceeds 100 KB pre-read budget")
    result["owners"] = client.hgetall(key)
    result["stream_types"] = {name: client.type(prefix + ":" + name) for name in (
        "heartbeats", "market-data", "market-data-subscriptions", "order-events",
        "runtime-controls", "snapshot-batches", "strategy-intents", "strategy-state",
        "strategy-state-isolated")}
    result["redis_after"] = {"evicted_keys": client.info("stats")["evicted_keys"],
                             "used_memory": client.info("memory")["used_memory"]}
    result["hashes"] = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (repo / "src/project_mai_tai/deploy_preflight.py",
                     repo / "ops/preflight/preflight_v2_restart.sh",
                     repo / "ops/preflight/preflight_oms_restart.sh",
                     Path("/home/trader/preopen.sh"))}
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()

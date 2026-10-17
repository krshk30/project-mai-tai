"""Opt-in READ-ONLY plan proof on an explicitly prepared synthetic local lab.

SOURCE AUTHORED ONLY. No default DSN, database creation, seeding, schema change,
setting change, network/provider client, cleanup or retry. Main seeds/reviews the
fresh lab and manifest separately, then grants one serial invocation per case.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re

from sqlalchemy import create_engine, or_, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import aliased

from project_mai_tai.db.models import BrokerAccount, BrokerOrder, Fill, Strategy, VirtualPosition
from project_mai_tai.oms.orb_protection_work import protection_guard_query, protection_work_query


EVIDENCE_SHA = "e981dd521ea75debe5d852ac9732b204e00225632fa6a73f380cef125136edb5"
BOX_ESTIMATES = {"broker_orders": 84932, "fills": 20657, "virtual_positions": 1018,
                 "broker_accounts": 12, "strategies": 15}


def qualification_url(raw, acknowledgement):
    url = make_url(raw)
    socket = url.query.get("host", "")
    if (os.geteuid() == 0 or url.drivername != "postgresql+psycopg"
            or not re.fullmatch(r"qualification_sync2x_[a-z0-9_]+", url.database or "")
            or acknowledgement != url.database or url.username != "killsw1_replay"
            or url.password is not None or url.host not in {None, "localhost"}
            or type(url.port) is not int or not 1024 <= url.port <= 65535 or url.port == 5432
            or set(url.query) != {"host"} or not isinstance(socket, str)
            or not socket.startswith("/private/tmp/") or ".." in Path(socket).parts):
        raise ValueError("explicit_local_synthetic_sync2x_lab_required")
    return url


def validate_manifest(manifest, database):
    if (manifest.get("database") != database or manifest.get("source_evidence_sha256") != EVIDENCE_SHA
            or manifest.get("dataset_kind") != "SYNTHETIC_NOT_PRODUCTION_COPY"
            or manifest.get("case") not in {"idle", "mixed"}
            or manifest.get("account") != "live:orb"
            or manifest.get("distribution_reviewed_by_main") is not True
            or not isinstance(manifest.get("expected_ids"), list)
            or not isinstance(manifest.get("expected_pg_major"), int)
            or manifest["expected_pg_major"] < 14):
        raise ValueError("reviewed_synthetic_manifest_required")
    for table, estimate in BOX_ESTIMATES.items():
        count = manifest.get("row_counts", {}).get(table)
        if type(count) is not int or count < estimate:
            raise ValueError("synthetic_box_scale_floor_not_met")
    if manifest["case"] == "mixed" and not manifest["expected_ids"]:
        raise ValueError("mixed_case_requires_independent_expected_work")
    if manifest["case"] == "idle" and manifest["expected_ids"]:
        raise ValueError("idle_case_requires_empty_expected_work")


def original_query(account):
    newer = aliased(BrokerOrder)
    superseded = select(newer.id).where(
        newer.strategy_id == BrokerOrder.strategy_id,
        newer.broker_account_id == BrokerOrder.broker_account_id,
        newer.symbol == BrokerOrder.symbol, newer.side == "buy",
        or_(newer.submitted_at > BrokerOrder.submitted_at,
            (newer.submitted_at == BrokerOrder.submitted_at) & (newer.id > BrokerOrder.id)),
    ).exists()
    filled = select(Fill.id).where(
        Fill.order_id == BrokerOrder.id, Fill.side == "buy", Fill.quantity > 0,
    ).exists()
    return select(BrokerOrder.id).join(Strategy).join(
        BrokerAccount, BrokerAccount.id == BrokerOrder.broker_account_id,
    ).outerjoin(VirtualPosition, (
        (VirtualPosition.strategy_id == BrokerOrder.strategy_id)
        & (VirtualPosition.broker_account_id == BrokerOrder.broker_account_id)
        & (VirtualPosition.symbol == BrokerOrder.symbol)
    )).where(
        Strategy.code == "orb_schwab", BrokerAccount.name == account, BrokerOrder.side == "buy",
        or_((~superseded) & or_(VirtualPosition.quantity > 0,
                VirtualPosition.quantity.is_(None) & filled),
            BrokerOrder.payload["orbroute1_protection"]["phase"].as_string().in_(
                {"submitting", "accepted", "working", "unknown"})),
        BrokerOrder.payload["orb_country_route"].as_string() == "true",
    )


def acceptable_plan(plan):
    return (plan["Plan"]["Total Cost"] < 100000
            and plan["Execution Time"] <= 40
            and plan.get("JIT", {}).get("Functions", 0) == 0)


def explain(connection, statement):
    sql = str(statement.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True}))
    return connection.execute(text("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql)).scalar_one()[0]


def inspect_lab(url, manifest):
    socket = Path(url.query["host"])
    if not socket.is_dir() or socket.is_symlink() or socket.resolve() != socket:
        raise ValueError("local_socket_directory_binding_required")
    engine = create_engine(url, isolation_level="REPEATABLE READ", connect_args={
        "connect_timeout": 3,
        "options": "-cdefault_transaction_read_only=on -cstatement_timeout=10000 -clock_timeout=2000 -csearch_path=public",
    })
    try:
        with engine.connect() as connection:
            identity = connection.execute(text(
                "SELECT current_database(),current_user,session_user,current_setting('port'),"
                "inet_server_addr()::text,current_setting('transaction_read_only'),"
                "current_setting('server_version_num'),current_setting('jit'),"
                "current_setting('jit_above_cost')"
            )).one()
            if (identity[:3] != (url.database, "killsw1_replay", "killsw1_replay")
                    or int(identity[3]) != url.port or identity[4] is not None or identity[5] != "on"
                    or int(identity[6]) // 10000 != manifest["expected_pg_major"] or identity[7] != "off"):
                raise ValueError("readonly_local_lab_identity_or_required_jit_off_failed")
            counts = {table: connection.scalar(text(f"SELECT count(*) FROM public.{table}"))
                      for table in BOX_ESTIMATES}
            if counts != manifest["row_counts"]:
                raise ValueError("prepared_dataset_cardinality_changed")
            indexes = [dict(row._mapping) for row in connection.execute(text(
                "SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' "
                "AND tablename IN ('broker_orders','fills','virtual_positions','strategies','broker_accounts') "
                "ORDER BY tablename,indexname"
            ))]
            if indexes != manifest.get("indexes"):
                raise ValueError("reviewed_index_manifest_changed")
            account = manifest["account"]
            statements = {"old": original_query(account), "guard": protection_guard_query(account),
                          "new": protection_work_query(account)}
            plans = {name: explain(connection, query) for name, query in statements.items()}
            old_ids = sorted(str(value) for value in connection.scalars(statements["old"]))
            new_ids = sorted(str(value) for value in connection.scalars(statements["new"]))
            guard_present = connection.scalar(statements["guard"]) is not None
            guarded_ids = new_ids if guard_present else []
            expected = sorted(manifest["expected_ids"])
            equal = old_ids == new_ids == guarded_ids == expected
            path_ms = plans["guard"]["Execution Time"] + (plans["new"]["Execution Time"] if guard_present else 0)
            return {"scope": "SYNTHETIC_LOCAL_READONLY_NOT_PRODUCTION_CAUSAL_PROOF",
                "identity": list(identity), "case": manifest["case"], "row_counts": counts,
                "indexes": indexes, "plans": plans, "expected_ids": expected,
                "old_ids": old_ids, "new_ids": new_ids, "guarded_ids": guarded_ids,
                "equivalent": equal, "guarded_path_execution_ms": path_ms,
                "passed": equal and path_ms <= 40 and acceptable_plan(plans["guard"]) and acceptable_plan(plans["new"]),
                "limits": "Box row figures are estimates; synthetic distribution requires main review. "
                          "Plans run old/guard/new serially, not a cold-cache benchmark. Each raw plan is the sole "
                          "measured invocation; later SELECTs are equality checks, not retries."}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dsn", required=True)
    parser.add_argument("--database-write-ack", required=True, help="Exact synthetic database name; this tool remains read-only")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    url = qualification_url(args.dsn, args.database_write_ack)
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    validate_manifest(manifest, url.database)
    # Reserve a new private evidence file before opening the DB; never overwrite a run.
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        try:
            result = inspect_lab(url, manifest)
        except Exception as exc:
            json.dump({"passed": False, "status": "REFUSED_OR_INCOMPLETE", "error_class": type(exc).__name__,
                       "manifest_sha256": hashlib.sha256(raw).hexdigest()}, output, indent=2)
            raise
        result["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
        json.dump(result, output, indent=2)
        output.write("\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Execute production-compiled predicates in bounded READ ONLY PostgreSQL."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shlex
from types import SimpleNamespace
import subprocess
from uuid import UUID

from sqlalchemy.dialects import postgresql

from project_mai_tai.db.models import BrokerOrder
from project_mai_tai.oms.mirror_retained_hold import MirrorRetainedHoldMixin


ROOT = Path(__file__).resolve().parents[3]
FIXTURE = ROOT / "tests/fixtures/mirrorhold1_session_1008_recorded.json"
recorded = json.loads(FIXTURE.read_text())
account_id = UUID("3bf71604-e975-4339-9ac1-6ce82117b1ea")
compiled_queries = {}


def compile_query(symbol, segment, cutoff=None):
    service = MirrorRetainedHoldMixin()
    service._nfq_now = lambda: datetime.fromisoformat("2026-10-08T13:35:05+00:00")
    event = SimpleNamespace(payload=SimpleNamespace(broker_account_name="live:orb", strategy_code="schwab_1m_v2",
        symbol=symbol, metadata={"fanout_segment_id": segment, "fanout_slot_id": "CONTROLLED"}))

    class Capture:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def scalars(self, query):
            if cutoff is not None:
                query = query.where(BrokerOrder.submitted_at < cutoff)
            query = query.with_only_columns(BrokerOrder.client_order_id)
            compiled_queries[symbol] = str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
            return SimpleNamespace(all=lambda: [])

    service._mirrorhold_session_orders(Capture(), SimpleNamespace(id=account_id), event)
    return compiled_queries[symbol]


def literal(value):
    return "'" + value.replace("'", "''") + "'"


queries = []
for symbol, intent_id in [("AIXI", "34af52a6-b13d-44b7-80ca-dafa699863a6"),
                          ("FLYE", "5446a17f-af09-4d49-a91b-80c4d066a82f")]:
    intent = next(row for row in recorded["intents"] if row["id"] == intent_id)
    query = compile_query(symbol, intent["payload"]["metadata"]["fanout_segment_id"], datetime.fromisoformat(intent["created_at"]))
    queries.append("SELECT json_build_object('case'," + literal(symbol) + ",'clients',coalesce(json_agg(t.client_order_id),'[]'::json)) FROM (" + query + ") t")

wirecap = json.loads((ROOT / "tests/fixtures/mirrorhold1_wirecap_1008_recorded.json").read_text())
cap_intent = next(i for i in wirecap["intents"] if i["payload"].get("refusal_code") == "mirrorhold_actual_submission_cap")
query = compile_query("FLYE", cap_intent["payload"]["metadata"]["fanout_segment_id"], datetime.fromisoformat(cap_intent["created_at"]))
queries.append("SELECT json_build_object('case','FLYE_D28','clients',coalesce(json_agg(t.client_order_id),'[]'::json)) FROM (" + query + ") t")

controls = [
    ("current_pending", "2026-10-08T09:00:00Z", {"fanout_segment_id": "1791388800000"}, "pending"),
    ("old_working_gtc", "2026-08-25T15:00:00Z", {"fanout_segment_id": "1791388800000"}, "accepted"),
    ("old_same_filled", "2026-08-25T15:00:00Z", {"fanout_segment_id": "1791466502449"}, "filled"),
    ("null_submitted", None, {"fanout_segment_id": "1791388800000"}, "rejected"),
    ("explicit_null", "2026-08-25T15:00:00Z", {"fanout_segment_id": None}, "rejected"),
    ("bad_segment", "2026-08-25T15:00:00Z", {"fanout_segment_id": "bad"}, "rejected"),
    ("boolean_true", "2026-08-25T15:00:00Z", {"fanout_segment_id": True}, "rejected"),
    ("zero_segment", "2026-08-25T15:00:00Z", {"fanout_segment_id": "00"}, "rejected"),
    ("null_status", "2026-08-25T15:00:00Z", {"fanout_segment_id": "1791388800000"}, None),
    ("orphan_slot", "2026-08-25T15:00:00Z", {"fanout_slot_id": "orphan"}, "rejected"),
    ("old_legacy_terminal", "2026-08-25T15:00:00Z", {}, "rejected"),
    ("old_unrelated_filled", "2026-08-25T15:00:00Z", {"fanout_segment_id": "1791388800000"}, "filled"),
    ("old_unrelated_terminal", "2026-08-25T15:00:00Z", {"fanout_segment_id": "1791388800000"}, "cancelled"),
]
values = []
for name, timestamp, metadata, status in controls:
    values.append("(" + ",".join([literal(name), literal(str(account_id)) + "::uuid", "'AIXI'", "'buy'",
        literal(timestamp) + "::timestamptz" if timestamp else "NULL::timestamptz",
        literal(json.dumps(metadata)) + "::json", literal(status) if status else "NULL::text"]) + ")")
query = compile_query("AIXI", "1791466502449")
queries.append("WITH broker_orders(client_order_id,broker_account_id,symbol,side,submitted_at,payload,status) AS (VALUES "
               + ",".join(values) + ") SELECT json_build_object('case','CONTROLLED PostgreSQL VALUES','clients',json_agg(t.client_order_id)) FROM (" + query + ") t")
sql = "BEGIN READ ONLY; SET LOCAL statement_timeout='15s'; SET LOCAL lock_timeout='2s';\n" + ";\n".join(queries) + ";\nROLLBACK;\n"
sql_path = Path("/tmp/mirrorholdG-queue-postgres.sql")
sql_path.write_text(sql)
command = shlex.join(["sudo", "-n", "-u", "postgres", "psql", "-X", "-At", "-v", "ON_ERROR_STOP=1", "-d", "project_mai_tai"])
process = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "mai-tai-vps", command],
                         input=sql, text=True, capture_output=True, check=True, timeout=60)
rows = [json.loads(line) for line in process.stdout.splitlines() if line.startswith("{")]
by_case = {row["case"]: set(row["clients"]) for row in rows}
assert by_case["AIXI"] == {"schwab_1m_v2-AIXI-open-ec407a4e45b0"}
assert by_case["FLYE"] == set()
assert by_case["FLYE_D28"] == {o["client_order_id"] for o in wirecap["orders"] if o["submitted_at"] >= "2026-10-08T08:00:00Z"}
assert by_case["CONTROLLED PostgreSQL VALUES"] == {row[0] for row in controls[:10]}
print(json.dumps({"passed": True, "rows": rows, "sql_path": str(sql_path),
                  "sql_sha256": hashlib.sha256(sql.encode()).hexdigest(),
                  "source_sha256": hashlib.sha256((ROOT / "src/project_mai_tai/oms/mirror_retained_hold.py").read_bytes()).hexdigest(),
                  "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest()}, indent=2))

"""Print a read-only SSH-stdin probe of the committed proof methods; no remote file."""
import ast
import json
from pathlib import Path

source = Path("src/project_mai_tai/services/schwab_1m_v2_bot.py").read_text()
tree = ast.parse(source)
methods = [ast.get_source_segment(source, node) for cls in tree.body if isinstance(cls, ast.ClassDef)
    for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    and node.name in {"_soft_rest_boot_positions", "_soft_rest_boot_proofs"}]
assert len(methods) == 2
print("import json, asyncio, types\nfrom datetime import UTC,datetime")
print("import project_mai_tai.services.schwab_1m_v2_bot as module")
print("from project_mai_tai.settings import Settings")
print("from project_mai_tai.db.session import build_timed_session_factory")
print("namespace = dict(vars(module))")
print("from project_mai_tai.db.models import DashboardSnapshot")
print("namespace['DashboardSnapshot'] = DashboardSnapshot")
print("exec(" + json.dumps("\n\n".join(methods)) + ", namespace)")
print("reader = object.__new__(module.SchwabV2BotService)")
print("reader.settings = Settings(_env_file='/etc/project-mai-tai/project-mai-tai.env')")
print("reader.strategy = types.SimpleNamespace(_dual_broker_fanout_enabled=True)")
print("reader.session_factory = build_timed_session_factory(reader.settings, service='schwab_1m_v2', profile='fast')")
for name in ("_soft_rest_boot_positions", "_soft_rest_boot_proofs"):
    print(f"reader.{name} = types.MethodType(namespace[{name!r}], reader)")
print("proofs = reader._soft_rest_boot_proofs({'AIXI':1791462062227})")
print("print(json.dumps({'as_of_utc':datetime.now(UTC).isoformat(),'read_only':True,'proofs':proofs},indent=2))")

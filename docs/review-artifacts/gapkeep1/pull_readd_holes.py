"""Bounded tape counts for the independently captured retained hold lines."""
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta

raw = json.load(open(sys.argv[1]))
holes = []
for event in raw["events"]:
    line = event["line"]
    if "[V2-GAP-HOLD]" not in line or "reason=recovery_gap" in line or event["day"] < "2026-09-28":
        continue
    sym = re.search(r"\[V2-GAP-HOLD\] (\S+)", line)[1]
    at = datetime.fromisoformat(event["at"])
    age = float(re.search(r"last_bar_age_s=([\d.]+)", line)[1])
    holes.append({**event, "symbol": sym, "lo": str(at - timedelta(seconds=age)),
                  "hi": str(at - timedelta(seconds=10))})
program = '''
import json
from datetime import datetime
from sqlalchemy import create_engine,text
from project_mai_tai.settings import Settings
holes = HOLES
engine=create_engine(Settings(_env_file="/etc/project-mai-tai/project-mai-tai.env").database_url)
with engine.connect() as c:
 for h in holes:
  for table in ("market_trade_ticks","market_capture_trades"):
   c.execute(text("SET TRANSACTION READ ONLY"));c.execute(text("SET LOCAL statement_timeout='5s'"))
   h[table]=dict(c.execute(text("SELECT count(*) n,count(*) FILTER (WHERE received_at<=:at) received_by_detect FROM "+table+" WHERE symbol=:sym AND event_ts>:lo AND event_ts<:hi"),dict(sym=h['symbol'],lo=datetime.fromisoformat(h['lo']),hi=datetime.fromisoformat(h['hi']),at=datetime.fromisoformat(h['at']))).mappings().one());c.rollback()
print(json.dumps(holes))
'''.replace("HOLES", repr(holes))
result = subprocess.run(["ssh", "mai-tai-vps", "cd /home/trader/project-mai-tai && sudo nice -n 19 .venv/bin/python -"], input=program, text=True, capture_output=True, check=True)
print(result.stdout, end="")

#!/usr/bin/env python3
"""Read-only box receipt. Safe over SSH stdin; no provider calls or file writes."""
import argparse
from datetime import datetime, UTC
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlparse
from urllib.request import urlopen
from zoneinfo import ZoneInfo

BASE = "860fba4c7d59edff18eb23468ba576dcb83f4d57"
REPO = Path("/home/trader/project-mai-tai")
LOGS = Path("/var/log/project-mai-tai")
UNITS = ("oms", "strategy", "schwab-1m-v2", "control", "market-data", "scanner",
         "orb-schwab", "reconciler", "momentum-paper")
SAFE_SETTINGS = {
    "MAI_TAI_OMS_BROKER_SYNC_INTERVAL_SECONDS",
    "MAI_TAI_STRATEGY_SCHWAB_1M_V2_BAR_POLL_INTERVAL_SECONDS",
    "MAI_TAI_STRATEGY_SCHWAB_1M_V2_MACD_PROBE_SYMBOLS",
    "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_QUEUE_MAXSIZE",
    "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_MONITOR_INTERVAL_SECONDS",
    "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_MONITOR_TIMEOUT_SECONDS",
    "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_HEALTH_PATH",
    "MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_LOG_DIRECTORY", "MAI_TAI_REDIS_STREAM_PREFIX", "TZ",
}
STREAM_PROJECT = r'''
local count=tonumber(ARGV[1])
if not count or count<1 or count>16 then return redis.error_reply('projection count bound') end
local entries=redis.call('XREVRANGE',KEYS[1],'+','-','COUNT',count)
local out={}
for _,entry in ipairs(entries) do
  local data=nil
  for i=1,#entry[2],2 do if entry[2][i]=='data' then data=entry[2][i+1] end end
  if not data or #data>8388608 then return redis.error_reply('existing payload8MiB bound') end
  local ok,row=pcall(cjson.decode,data)
  if not ok or type(row)~='table' or type(row.payload)~='table' then return redis.error_reply('payload unreadable') end
  local fields,counts={},{}
  for _,key in ipairs({'cycle_count','completed_at','symbol','tick_type'}) do
    local v=row.payload[key]
    if type(v)=='string' and #v<=128 or type(v)=='number' then fields[key]=v end
  end
  for _,key in ipairs({'watchlist','five_pillars','top_gainers','snapshots','symbols'}) do
    local v=row.payload[key]
    if type(v)=='table' then counts[key]=#v end
  end
  out[#out+1]={redis_id=entry[1],event_type=row.event_type,source_service=row.source_service,
              produced_at=row.produced_at,safe_fields=fields,counts=counts,source_payload_bytes=#data}
end
return cjson.encode(out)
'''


def run(args, *, text=None, timeout=5):
    completed = subprocess.run(args, input=text, capture_output=True, text=True, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"read command failed rc={completed.returncode}: {args[0]}")
    if len(completed.stdout) > 262144:
        raise ValueError("read output bound")
    return completed.stdout.strip()


def guarded(function):
    try:
        return function()
    except Exception as exc:
        return {"status": "UNKNOWN", "error_type": type(exc).__name__, "reason": str(exc)[:160]}


def environment(pid):
    allowed = {}
    raw = Path(f"/proc/{pid}/environ").read_bytes()
    for entry in raw.split(b"\0"):
        key, separator, value = entry.partition(b"=")
        name = key.decode(errors="replace")
        if not separator:
            continue
        decoded = value.decode(errors="replace")
        flag = name.startswith("MAI_TAI_") and name.endswith("_ENABLED")
        retry = name.startswith("MAI_TAI_") and name.endswith("_MAX_RETRIES")
        if name in SAFE_SETTINGS or flag or retry:
            if flag and decoded.lower() not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
                decoded = "UNKNOWN_nonboolean"
            allowed[name] = decoded
    # Expose only the configured DB name, never URL/user/password.
    for entry in raw.split(b"\0"):
        if entry.startswith(b"MAI_TAI_DATABASE_URL="):
            allowed["configured_database_name"] = urlparse(entry.split(b"=",1)[1].decode()).path.strip("/")
    return allowed


def process(unit):
    text = run(["systemctl", "show", f"project-mai-tai-{unit}.service", "-p", "MainPID",
                "-p", "ActiveState", "-p", "SubState", "-p", "ExecMainStartTimestamp",
                "-p", "NRestarts", "-p", "User", "-p", "LoadState"])
    data = dict(line.split("=",1) for line in text.splitlines() if "=" in line)
    pid = int(data.get("MainPID",0))
    if pid:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat.rsplit(")",1)[1].split()
        data["instance_id"] = f"{pid}:{int(fields[19])}"
        data["flags_and_settings"] = environment(pid)
        started = datetime.strptime(data["ExecMainStartTimestamp"], "%a %Y-%m-%d %H:%M:%S %Z").replace(tzinfo=UTC)
        data["started_ns_second_precision"] = int(started.timestamp()) * 1_000_000_000
        data["socket_fd_count_unclassified"] = sum(
            os.readlink(path).startswith("socket:[") for path in Path(f"/proc/{pid}/fd").iterdir()
        )
    return data


def sql_gate(database):
    if not re.fullmatch(r"[A-Za-z0-9_]+",database):
        raise ValueError("configured database name missing/invalid")
    query = """
BEGIN READ ONLY;
SET LOCAL statement_timeout='5s';
SELECT json_build_object(
 'observed_at',now(),
 'accounts',(SELECT count(*) FROM broker_accounts WHERE name IN ('live:orb','live:schwab_1m_v2')),
 'working',(SELECT count(*) FROM broker_orders o JOIN broker_accounts a ON a.id=o.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND o.status IN ('new','pending','submitted','accepted','partially_filled','working','pending_cancel','pending_replace','held')),
 'inflight',(SELECT count(*) FROM trade_intents t JOIN broker_accounts a ON a.id=t.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND t.status IN ('pending','submitted','dispatching','processing')),
 'managed',(SELECT count(*) FROM oms_managed_positions WHERE status='open'),
 'positions',(SELECT count(*) FROM account_positions p JOIN broker_accounts a ON a.id=p.broker_account_id WHERE a.name IN ('live:orb','live:schwab_1m_v2') AND p.quantity<>0),
 'receipts',(SELECT count(DISTINCT payload->>'broker_account_name') FROM dashboard_snapshots WHERE snapshot_type='oms_positions_read_receipt' AND payload->>'broker_account_name' IN ('live:orb','live:schwab_1m_v2') AND payload->>'writer'='oms.sync_broker_positions' AND (payload->>'position_count')::integer=0 AND (payload->>'read_at')::timestamptz BETWEEN now()-interval '30 seconds' AND now()),
 'latest_position_receipts',(SELECT json_agg(x) FROM (SELECT DISTINCT ON (payload->>'broker_account_name') payload->>'broker_account_name' AS account,payload->>'read_at' AS read_at,payload->>'position_count' AS position_count,payload->>'writer' AS writer FROM dashboard_snapshots WHERE snapshot_type='oms_positions_read_receipt' AND payload->>'broker_account_name' IN ('live:orb','live:schwab_1m_v2') ORDER BY payload->>'broker_account_name',created_at DESC) x)
);
ROLLBACK;
"""
    raw = run(["sudo","-n","-u","postgres","psql","-X","-At","-v","ON_ERROR_STOP=1",database],
              text=query,timeout=8)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end < start:
        raise ValueError("SQL receipt missing")
    # PostgreSQL json_agg may span lines; transaction tags are outside the object.
    data = json.loads(raw[start:end+1])
    expected = dict(accounts=2,working=0,inflight=0,managed=0,positions=0,receipts=2)
    data["existing_E_gate_pass_now"] = all(data.get(key)==value for key,value in expected.items())
    data["order_scope"] = "cached broker_orders statuses, not a new direct broker open-order read"
    return data


def v2_state(prefix):
    raw = run(["redis-cli","--raw","XREVRANGE",prefix+":strategy-state-isolated","+","-","COUNT","32"])
    entries = [json.loads(line) for line in raw.splitlines() if line.startswith("{")]
    data = next((row for row in entries if row.get("source_service")=="schwab-1m-v2"),None)
    if data is None:
        raise ValueError("no explicit v2 state producer")
    payload = data["payload"]
    produced = datetime.fromisoformat(data["produced_at"].replace("Z","+00:00"))
    age = (datetime.now(UTC)-produced).total_seconds()
    arms = payload.get("cw_armed_segments")
    watch = payload.get("watchlist")
    return {"source_service":data["source_service"],"produced_at":data["produced_at"],"age_seconds":age,
            "cw_armed_segments":arms,"watchlist":watch,"restoration_complete":payload.get("restoration_complete"),
            "strict_zero_armed_30s":isinstance(arms,list) and not arms and 0<=age<=30}


def log_sample(name, baseline=None):
    path = LOGS/(name+".log")
    with path.open("rb") as stream:
        stat = os.fstat(stream.fileno())
        if baseline and (stat.st_ino!=baseline["inode"] or stat.st_size<baseline["size"]):
            raise ValueError("log rotated/truncated since baseline")
        start = baseline["size"] if baseline else max(0,stat.st_size-65536)
        if stat.st_size-start>65536:
            raise ValueError("bounded log prefix exceeded")
        stream.seek(start)
        lines = stream.read(65537).decode(errors="replace").splitlines()
    ends, probes = [], []
    for line in lines:
        stamp = re.match(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d),(\d{3})",line)
        if stamp and "[OMS-BROKER-SYNC-PASS]" in line and "phase=end" in line:
            fields = dict(re.findall(r"(id|outcome|duration_ms)=([^ ]+)",line))
            ends.append({"log_stamp":stamp[1]+","+stamp[2],**fields})
        probe = re.search(r"\[V2-MACD-PROBE\] sym=(\S+) ts_ms=(\d+)",line)
        if stamp and probe:
            probes.append({"log_stamp":stamp[1]+","+stamp[2],"symbol":probe[1],"bar_start_ms":int(probe[2])})
    return {"path":str(path),"inode":stat.st_ino,"size":stat.st_size,"read_from":start,
            "new_error_lines":sum(bool(re.search(r"\b(?:ERROR|CRITICAL)\b",line)) for line in lines) if baseline else None,
            "oms_end_receipts":ends[-20:],"v2_completed_bar_probes":probes[-10:]}


def health():
    with urlopen("http://127.0.0.1:8100/health",timeout=5) as response:
        raw=response.read(262145)
    if len(raw)>262144:
        raise ValueError("health byte bound")
    source=json.loads(raw)
    keys=("service_name","raw_status","status","observed_at_raw","observed_at","pid",
          "process_id","code_sha","git_sha","loaded_git_sha","started_at")
    detail_keys=("enabled", "data_flow", "market_session", "secs_since_last_bar", "secs_since_last_quote",
                 "quotes_live", "rest_empty_streak_max", "watchlist_size", "bars_processed",
                 "active_symbols", "trade_count", "quote_count", "ticks_written", "loop_state")
    return {"service":source.get("service"),"timestamp":source.get("timestamp"),
            "database_connected":source.get("database_connected"),"redis_connected":source.get("redis_connected"),
            "services":[{**{key:row[key] for key in keys if key in row},
                         "safe_details":{key:value for key,value in row.get("details",{}).items() if key in detail_keys}}
                        for row in source.get("services",[]) if isinstance(row,dict)]}


def stream_overview(prefix, name, count=1):
    # Project retained fields on Redis before crossing the output byte bound.
    raw=run(["redis-cli","--raw","EVAL_RO",STREAM_PROJECT,"1",prefix+":"+name,str(count)])
    rows=json.loads(raw)
    if rows=={}:
        rows=[]
    if not isinstance(rows,list) or len(rows)>count:
        raise ValueError("projected row bound")
    return {"scope":"EVAL_RO/XREVRANGE projection of existing payload, no provider/new app API", "rows":rows}


def scanner(streams):
    return {"scope":"existing control view's strategy-state/snapshot-batches source fields only; no rendered UI live label",
            "strategy_state":streams["strategy-state"],"snapshot_batches":streams["snapshot-batches"]}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--observe-seconds",type=int,default=20,choices=range(0,31))
    args=parser.parse_args()
    observed=datetime.now(UTC)
    units={name:guarded(lambda name=name:process(name)) for name in UNITS}
    oms_settings=units["oms"].get("flags_and_settings",{})
    gateway_settings=units["market-data"].get("flags_and_settings",{})
    prefix=oms_settings.get("MAI_TAI_REDIS_STREAM_PREFIX","mai_tai")
    database=oms_settings.get("configured_database_name","")
    before={name:guarded(lambda name=name:log_sample(name)) for name in ("oms","strategy","schwab-1m-v2","orb-schwab","market-data","momentum-paper","control")}
    streams={name:guarded(lambda name=name:stream_overview(prefix,name,16 if name=="market-data" else 1))
             for name in ("strategy-state","market-data","snapshot-batches","market-data-subscriptions")}
    result={"mode":"READ_ONLY_REHEARSAL_NO_AUTH_REFRESH","observed_at_utc":observed.isoformat(),
            "observed_at_et":observed.astimezone(ZoneInfo("America/New_York")).isoformat(),
            "head":guarded(lambda:run(["git","-C",str(REPO),"rev-parse","HEAD"])),
            "worktree_clean":guarded(lambda:not run(["git","-C",str(REPO),"status","--porcelain"])),
            "timezone":guarded(lambda:run(["timedatectl","show","-p","Timezone","--value"])),
            "load_1m":os.getloadavg()[0],"units":units,"logs_baseline":before,
            "database_gate":guarded(lambda:sql_gate(database)),"v2_state":guarded(lambda:v2_state(prefix)),
            "health":guarded(health),"scanner":scanner(streams),"existing_streams":streams,
            "warning_health_setup":"UNKNOWN_until_reviewed_config_and_live_sources"}
    config_path=Path(gateway_settings.get("MAI_TAI_MARKET_DATA_MOMENTUM_WARNING_HEALTH_PATH",
                                          "/run/project-mai-tai/momentum-warning-health.json"))
    result["health_config_file"]={"path":str(config_path),"exists":config_path.is_file()}
    if config_path.is_file():
        raw=config_path.read_bytes()
        if len(raw)<=8192:
            config=json.loads(raw)
            keys=("source_config_version","oms_pid","v2_pid","oms_instance_id","v2_instance_id",
                  "oms_started_ns","v2_started_ns","verified_timezone","oms_sync_interval_seconds",
                  "v2_probe_symbols","v2_expectation_kind","v2_expected_start_et","v2_first_bar_start_et",
                  "v2_bar_interval_seconds")
            result["health_config_file"].update(sha256=hashlib.sha256(raw).hexdigest(),
                safe_fields={key:config[key] for key in keys if key in config})
    time.sleep(args.observe_seconds)
    result["ended_at_utc"]=datetime.now(UTC).isoformat()
    result["logs_since_baseline"]={name:guarded(lambda name=name:log_sample(name,before[name]))
        for name in before if "inode" in before[name]}
    result["final_database_gate"]=guarded(lambda:sql_gate(database))
    result["final_v2_state"]=guarded(lambda:v2_state(prefix))
    result["future_install_pass"]="NOT_RUN_NOT_AUTHORIZED_BY_THIS_RECEIPT"
    print(json.dumps(result,indent=2))


if __name__=="__main__":
    main()

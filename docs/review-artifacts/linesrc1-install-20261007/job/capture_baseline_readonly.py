"""[codex] Bounded root read-only capture; stdout only, no tokens or service actions."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess

SERVICES=("control","market-capture","market-data","oms","orb","orb-schwab","reconciler",
    "schwab-1m-v2","strategy","momentum-paper","option-a-daily-guard","redis","postgresql")
FIELDS=("MainPID","NRestarts","ActiveState","SubState","Result","ExecMainCode","ExecMainStatus",
    "ExecMainStartTimestamp","ExecMainStartTimestampMonotonic","FragmentPath","DropInPaths",
    "EnvironmentFiles","InactiveEnterTimestamp","InvocationID")


def read(args):
    result=subprocess.run(args,capture_output=True,timeout=15)
    if result.returncode or result.stderr.strip() or len(result.stdout)>262144:
        raise RuntimeError("bounded read failed: "+str(args))
    return result.stdout.decode()


def capture():
    fleet={}
    for role in SERVICES:
        unit=role+".service" if role in {"redis","postgresql"} else "project-mai-tai-"+role+".service"
        pairs=[line.split("=",1) for line in read(["systemctl","show","--all",unit,*["--property="+key for key in FIELDS]]).splitlines()]
        if any(len(pair)!=2 for pair in pairs) or len(dict(pairs))!=len(pairs): raise ValueError("malformed unit fields")
        state=dict(pairs)
        if role in {"redis","postgresql"} and "EnvironmentFiles" not in state:
            obj=read(["busctl","call","org.freedesktop.systemd1","/org/freedesktop/systemd1",
                "org.freedesktop.systemd1.Manager","GetUnit","s",unit]).strip()
            if not obj.startswith("o "): raise ValueError("D-Bus object missing")
            obj=json.loads(obj[2:])
            if read(["busctl","get-property","org.freedesktop.systemd1",obj,
                "org.freedesktop.systemd1.Service","EnvironmentFiles"]).strip()!="a(sb) 0": raise ValueError("system env scope differs")
            state["EnvironmentFiles"]=""
        if set(state)!=set(FIELDS): raise ValueError("incomplete unit fields")
        for key in ("MainPID","NRestarts","ExecMainCode","ExecMainStatus","ExecMainStartTimestampMonotonic"):
            state[key]=int(state[key])
        fleet[role]=state
    hashes={}
    for name in ("/etc/project-mai-tai/project-mai-tai.env","/home/trader/preopen.sh",
        "/home/trader/preopen-daily/runtime.json","/home/trader/restart_evidence/expected_flags.json",
        "/home/trader/restart_evidence/expected_numeric.json"):
        path=Path(name)
        if path.is_symlink() or path.stat().st_size>2_000_000: raise ValueError("unsafe/oversized capture file")
        hashes[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    now=datetime.now(timezone.utc).isoformat()
    baseline=dict(fleet_before=fleet,environment_sha256=hashes["/etc/project-mai-tai/project-mai-tai.env"],
        authorization_provenance="Latest human Oct7 paired LINESRC1+HOTFIX1114 standing GO; RPG handoff stays FALSE; #1115 parked; fresh read-only capture "+now)
    process={}
    keys=("MAI_TAI_STRATEGY_SCHWAB_1M_V2_LINE_CHART_RESTORATION_ENABLED",
        "MAI_TAI_OMS_V2_WEBULL_MIRROR_RETAINED_HOLD_ENABLED","MAI_TAI_STRATEGY_SCHWAB_1M_V2_ATR_REPRICE_HANDOFF_ENABLED")
    for role in ("oms","schwab-1m-v2"):
        with Path(f"/proc/{fleet[role]['MainPID']}/environ").open("rb") as stream: raw=stream.read(262145)
        if len(raw)>262144: raise ValueError("oversized proc")
        pairs=[piece.decode().split("=",1) for piece in raw.split(b"\0") if piece]
        process[role]={key:[value for name,value in pairs if name==key] for key in keys}
    return dict(baseline=baseline,hashes=hashes,process_flags=process,
        checkout=read(["runuser","-u","trader","--","git","-C","/home/trader/project-mai-tai","rev-parse","HEAD"]).strip(),
        dirty=read(["runuser","-u","trader","--","git","-C","/home/trader/project-mai-tai","status","--porcelain"]),
        installer=read(["systemctl","show","project-mai-tai-linesrc1-20261007.service","--property=MainPID","--property=ActiveState"]),
        timer=read(["systemctl","show","project-mai-tai-linesrc1-20261007.timer","--property=ActiveState","--property=NextElapseUSecRealtime"]))


if __name__=="__main__": print(json.dumps(capture(),sort_keys=True,indent=2))

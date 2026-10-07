"""Controlled literal Install2 rehearsal; no production I/O or source acceptance."""
import copy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import pytest
import attended
import binding
import catalog_policy
import closeout
import cumulative
import daily
import gate_patch
import linesrc_disposition as linesrc
import make_release
import post_proof
import release_policy as p
import restart_report

NOW = datetime(2026, 10, 6, 23, 10, tzinfo=timezone.utc)
EXPECTED_PHASES = (("stop", "schwab-1m-v2"), ("stop", "strategy"), ("stop", "oms"),
                   ("start", "oms"), ("start", "schwab-1m-v2"), ("start", "strategy"), ("restart", "control"))
JOB = Path(__file__).parent
BASE = JOB.parents[1] / "roundup1/job"
REPO = JOB.parents[3]
sys.path.append(str(BASE))


def fixture(name):
    spec = importlib.util.spec_from_file_location("fixture_" + name, BASE / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fleet():
    rows = {name: dict(MainPID=100+i, NRestarts=0, ActiveState="active", SubState="running",
            Result="success", ExecMainCode=1, ExecMainStatus=0, InvocationID=f"{i+1:032x}",
            ExecMainStartTimestamp="Tue 2026-10-06 22:00:00 UTC",
            ExecMainStartTimestampMonotonic=100, InactiveEnterTimestamp="")
            for i, name in enumerate(p.SERVICES)}
    rows["orb-schwab"]["MainPID"] = 612486
    rows["market-data"]["MainPID"] = 2907
    return rows


def catalogs():
    flags = json.loads(subprocess.check_output(["git","show",p.BOX+":ops/health/expected_flags.json"],cwd=REPO))["flags"]
    for key in p.NEW_ENV:
        flags.append(dict(name=key.removeprefix("MAI_TAI_").lower(),expected=True,
                          owning_service="schwab-1m-v2",also_check_services=["oms"],require_process_env=True))
    flags.append(dict(name="controlled_t43_enabled",expected=True,owning_service="oms"))
    next(row for row in flags if row["name"]==p.RETRY_ENABLED.removeprefix("MAI_TAI_").lower())["require_process_env"]=True
    numeric = catalog_policy.numeric_candidate(subprocess.check_output(
        ["git","show",p.BOX+":ops/health/expected_numeric.json"],cwd=REPO))
    return p.canonical(dict(schema_version=1,flags=flags)), numeric


def binding_value():
    return dict(approved_sha="a"*40,tree="b"*40,box_sha=p.BOX,control_restart=True,
                merged_prs=[dict(pr=pr,merge_sha=sha) for pr,sha in binding.EXISTING_MERGES]
                    + [dict(pr=1200,merge_sha="f"*40,governing_prs=[1104,1099,1105,1101])],
                reviewed_changed_paths=["src/project_mai_tai/settings.py"],
                t43_catalog_name="controlled_t43_enabled",source_combination_proof_sha256="c"*64,
                old_helper_hashes={name:"d"*64 for name in binding.HELPERS},
                install1={name:dict(path="/home/trader/CONTROLLED/"+name,sha256="e"*64) for name in binding.INPUTS})


def prior_files(tmp_path,current):
    before=copy.deepcopy(current)
    for name in p.CUMULATIVE:
        before[name]["MainPID"]-=1
        before[name]["ExecMainStartTimestamp"]="Tue 2026-10-06 20:01:00 UTC"
    snapshot=dict(captured_at_utc="2026-10-06T20:01:00+00:00",services={
        n:dict(pid=r["MainPID"],active_state=r["ActiveState"],sub_state=r["SubState"],n_restarts=r["NRestarts"])
        for n,r in before.items()})
    snapshot["services"]["tv-alerts"]=dict(pid=0,active_state="inactive",sub_state="dead",n_restarts=0)
    actions=sorted(cumulative.INSTALL1_ACTIONS)
    raws=dict(snapshot=p.canonical(snapshot),fleet_before=p.canonical(before),
              journal=b"\n".join(json.dumps(dict(argv=["systemctl",a,"project-mai-tai-"+n+".service"],rc=0)).encode()
                                for a,n in actions)+b"\n",
              continuation_journal=b"CONTROLLED text log: continuation receipts and disclosed FileExistsError\n")
    raws["human_review"]=p.canonical(dict(kind="DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE",
        application=p.BOX,original_complete=False,evidence_hashes={n:p.digest(v) for n,v in raws.items()},
        provenance=dict(human_quote="CONTROLLED fixture: user VERIFIED Install1",source="CONTROLLED test, not live"),
        verified_pins={n:{k:current[n][k] for k in ("MainPID","ExecMainStartTimestamp")} for n in p.CUMULATIVE}))
    result={}
    for name,raw in raws.items():
        path=tmp_path/("install1-"+name+".json")
        path.write_bytes(raw)
        result[name]=dict(path=str(path),sha256=p.digest(raw))
    return result


class FakeSystem(attended.Real):
    def __init__(self,job,release,attempt,repo):
        super().__init__(job,release,attempt)
        self.system,self.calls,self.head,self.repo=fleet(),[],p.BOX,repo
        self.fail=None

    def now(self): return NOW
    def fleet(self): return copy.deepcopy(self.system)
    def sql(self,since=None): return dict(tickets=[])

    def command(self,args,*,check=True,**kw):
        args=list(map(str,args))
        self.calls.append(args)
        if self.fail and self.fail in " ".join(args): raise p.Stop("CONTROLLED injected failure")
        out,rc=b"",0
        if "rev-parse" in args:
            out=((p.TREE if args[-1].endswith("^{tree}") else self.head)+"\n").encode()
        elif "show" in args and "git" in args: out=(self.repo/args[-1].split(":",1)[1]).read_bytes()
        elif "archive" in args: out=b"CONTROLLED source archive"
        elif "switch" in args: self.head=p.APP
        elif args[0]=="systemctl" and args[1] in {"stop","start","restart"}:
            name=args[2].removeprefix("project-mai-tai-").removesuffix(".service")
            assert (args[1],name) in p.PHASES
            row=self.system[name]
            if args[1]=="stop": row.update(MainPID=0,ActiveState="inactive",SubState="dead",Result="success")
            else:
                row.update(MainPID=fleet()[name]["MainPID"]+1000,ActiveState="active",SubState="running",
                           NRestarts=0,Result="success",ExecMainStartTimestampMonotonic=200,
                           ExecMainStartTimestamp="Tue 2026-10-06 23:10:00 UTC",InvocationID="f"*32)
                if name=="control":
                    text=f"INFO:     Started server process [{row['MainPID']}]\nINFO:     Application startup complete.\nINFO:     Uvicorn running on http://127.0.0.1:8100 (Press CTRL+C to quit)\n"
                else:
                    marker={"oms":"[BROKER-SYNC-CENSUS] live:orb: ok=1 failed=0 consecutive_now=0",
                            "schwab-1m-v2":"[V2-BOOT-HOLD] restored=1\n2026-10-06 23:10:00,001 [V2-LINE-RESTORE] entry_allowed=0",
                            "strategy":"strategy starting"}[name]
                    text="2026-10-06 23:10:00,000 "+marker+"\n"
                with (self.repo/"logs"/(name+".log")).open("a") as stream: stream.write(text)
        elif args[:2]==["systemctl","show"]:
            out=(b"NextElapseUSecRealtime=Wed 2026-10-07 10:20:00 UTC\nLastTriggerUSec=\n"
                 if args[2].endswith(".timer") else b"ExecMainStartTimestamp=\nActiveState=inactive\n")
        elif any(a.endswith("strict_flat_readonly.py") for a in args):
            value=dict(rc=0,blockers=[],broker_holdings=[],working_orders=[],managed_rows=[],virtual_rows=[],inflight_intents=[],
                       account_stamps=[dict(account=n,updated_at=NOW.isoformat()) for n in ("live:schwab_1m_v2","live:orb")])
            out=p.canonical(value)
        elif any(a.endswith("census_readonly.py") for a in args): out=b'{"rc":0,"controlled":true}'
        elif any(a.endswith("redis_checkpoint.py") for a in args): out=b'{"controlled":true}'
        elif any(a.endswith("armed_readonly.py") for a in args):
            import armed_readonly
            controlled=fixture("test_armed_readonly")
            controlled.NOW=NOW
            out=p.canonical(armed_readonly.proof(controlled.reply(),NOW))
        elif any(a.endswith("control_display_proof.py") for a in args):
            assert "--page-only" in args
            out=p.canonical(dict(mode="LIVE",provider="SCHWAB"))
        elif any(a.endswith("preflight_oms_restart.sh") for a in args):
            out=b"  [ok]    database reachable\n  [info]  strict all-account-position flatness enabled; no symbols are excluded\n  [ok]    zero open managed rows\n  [ok]    live:schwab_1m_v2 flat [2s old]\n  [ok]    live:orb flat [2s old]\n  ===> GO. Flat on every real-money account, zero managed rows, all sources fresh.\n"
        elif any(a.endswith("preflight_v2_restart.sh") for a in args):
            out=b"  [ok]    past 18:00 ET\n  [ok]    zero armed segments [published state, 2.0s old]\n  [ok]    zero open managed rows\n  [ok]    broker flat on both real-money accounts (operator manuals excluded)\n  ===> GO. Zero armed segments AND flat. Safe to restart v2.\n"
        elif any(a.endswith("expected_flags_check.py") for a in args):
            rows=json.loads((self.repo/"ops/health/expected_flags.json").read_bytes())["flags"]
            rows+=json.loads((self.job/p.NUMERIC_ARTIFACT).read_bytes())["settings"]
            ids=catalog_policy.identities(rows)
            out=("\n".join(f"PASS flag={n} service={o} expected=true actual=true source=env" for n,o in ids)
                 +f"\nFinal call: PASS; checked={len(ids)}/{len(ids)} mismatches=0 unknown=0\n").encode()
        elif "snapshot" in args:
            snapshot=dict(captured_at_utc=NOW.isoformat(),services={
                n:dict(pid=r["MainPID"],active_state=r["ActiveState"],sub_state=r["SubState"],n_restarts=r["NRestarts"])
                for n,r in self.system.items()})
            snapshot["services"]["tv-alerts"]=dict(pid=0,active_state="inactive",sub_state="dead",n_restarts=0)
            Path(args[args.index("--output")+1]).write_bytes(p.canonical(snapshot))
        elif "report" in args:
            assert "--schema-column" not in args and "--no-schema-change" in args
            Path(args[args.index("--output")+1]).write_text("CONTROLLED report\nFinal call: PASS; controlled\n")
        elif args[:2]==["bash","-n"]: subprocess.run(args,check=True,capture_output=True)
        elif args[0] in {"systemd-analyze","systemctl","runuser"} or "git" in args: pass
        else: raise AssertionError("unexpected boundary: "+repr(args))
        self.receipt("fake-command.json",p.canonical(dict(argv=args,rc=rc,controlled=True)))
        self.receipt("stdout.txt",out)
        self.receipt("stderr.txt",b"")
        if check: p.need(rc==0,"CONTROLLED rc")
        return SimpleNamespace(returncode=rc,stdout=out,stderr=b"")


def setup(monkeypatch,tmp_path):
    for module in (p,attended,closeout,daily,gate_patch): monkeypatch.setattr(module,"APP","a"*40)
    monkeypatch.setattr(p,"TREE","b"*40)
    monkeypatch.setattr(attended,"TREE","b"*40)
    repo,job,attempt=tmp_path/"repo",tmp_path/"job",tmp_path/"attempt"
    for path in (repo,job,attempt,repo/"logs",tmp_path/"helpers",tmp_path/"units"): path.mkdir(parents=True,exist_ok=True)
    application={}
    for name in make_release.SOURCES:
        path=repo/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(subprocess.check_output(["git","show",p.BOX+":"+name],cwd=REPO))
    flags,numeric=catalogs()
    (repo/"ops/health/expected_flags.json").write_bytes(flags)
    for name in make_release.SOURCES: application[name]=p.digest((repo/name).read_bytes())
    for name in make_release.NAMES: (job/name).write_bytes((JOB/name).read_bytes())
    (job/p.NUMERIC_ARTIFACT).write_bytes(numeric)
    data=binding_value()
    data["install1"]=prior_files(tmp_path,fleet())
    data["old_helper_hashes"]={}
    for name in closeout.CATALOGS:
        raw=(repo/"ops/health"/name).read_bytes()
        (tmp_path/"helpers"/name).write_bytes(raw)
        data["old_helper_hashes"][name]=p.digest(raw)
    (job/"binding.json").write_bytes(p.canonical(data))
    (job/"release.json").write_text("CONTROLLED not approval")
    monkeypatch.setattr(catalog_policy,"BINDING",data)
    gate,env=tmp_path/"preopen.sh",tmp_path/"env"
    gate.write_bytes((JOB/"preopen.baseline.sh").read_bytes()); gate.chmod(0o700)
    env.write_text("UNCHANGED=1\n"+p.RETRY_ENABLED+"=true\n"+p.RETRY_MAX+"=0\n"); env.chmod(0o600)
    for name in p.CHANGED: (repo/"logs"/(name+".log")).write_text("CONTROLLED old process\n")
    journal=tmp_path/"deployments.md"; journal.write_text("CONTROLLED journal\n")
    for module,key,value in [(attended,"REPO",repo),(attended,"GATE",gate),(attended,"ENV",env),
        (closeout,"HELPERS",tmp_path/"helpers"),(closeout,"ROOT",tmp_path/"daily"),
        (closeout,"UNIT_DIRECTORY",tmp_path/"units"),(closeout,"JOURNAL",journal)]: monkeypatch.setattr(module,key,value)
    monkeypatch.setattr(attended.os,"chown",lambda *args:None)
    monkeypatch.setattr(attended.pwd,"getpwnam",lambda _:SimpleNamespace(pw_uid=gate.stat().st_uid))
    original_stat=Path.stat
    def stat(path,*args,**kw):
        result=original_stat(path,*args,**kw)
        if path==env: return SimpleNamespace(st_uid=0,st_gid=0,st_mode=result.st_mode,st_size=result.st_size)
        return result
    monkeypatch.setattr(Path,"stat",stat)
    real_path=Path
    def log_path(value):
        if str(value)=="/var/log/project-mai-tai": return repo/"logs"
        if str(value)=="/var/log/project-mai-tai/control.log": return repo/"logs/control.log"
        return real_path(value)
    monkeypatch.setattr(attended,"Path",log_path)
    def proc(value):
        text=str(value)
        if text.startswith("/proc/"):
            path=tmp_path/"proc"/text.removeprefix("/proc/")
            path.parent.mkdir(parents=True,exist_ok=True)
            values={k:"true" for k in p.NEW_ENV+p.PM}
            values.update({p.ATR:"true",p.RETRY_ENABLED:"true",p.RETRY_MAX:"0","MAI_TAI_ORB_LIVE_SCHWAB_ORDERS_ENABLED":"true"})
            path.write_bytes(b"\0".join((k+"="+v).encode() for k,v in values.items()))
            return path
        return real_path(value)
    monkeypatch.setattr(post_proof,"Path",proc)
    release=dict(binding=data,application_blobs=application,catalog_counts=catalog_policy.validate_catalogs(flags,numeric,data),plan_commit="c"*40)
    return FakeSystem(job,release,attempt,repo),gate,env


def test_literal_install2_daily_cumulative(monkeypatch,tmp_path):
    assert p.PHASES==EXPECTED_PHASES
    fx,gate,env=setup(monkeypatch,tmp_path)
    attended.Sequence(fx).run()
    actions=[a for a in fx.calls if a[0]=="systemctl" and a[1] in {"stop","start","restart"}]
    assert actions==[["systemctl",a,"project-mai-tai-"+n+".service"] for a,n in EXPECTED_PHASES]
    assert fx.system["orb-schwab"]==fx.before["orb-schwab"]
    assert fx.system["market-data"]==fx.before["market-data"]
    assert (fx.attempt/"COMPLETE.json").is_file() and (tmp_path/"daily/runtime.json").is_file()
    assert (tmp_path/"units/project-mai-tai-preopen.timer").is_file()
    assert not any(a==["bash",str(gate)] for a in fx.calls)
    assert "--schema-column" not in gate.read_text() and gate.read_text().count("--restarted ")==5
    assert "EXPECTED_ORB_SCHWAB_PID=612486" in gate.read_text() and "2907" in gate.read_text()
    assert "EXPECTED_DATE//-/" in gate.read_text() and "daily.py paper" in gate.read_text()
    record=json.loads((fx.attempt/"cumulative-install-record.json").read_bytes())
    assert record["install2_restarted"]==list(p.CHANGED) and record["install1_original_complete"] is False
    assert {n for n,a in record["service_actions"].items() if a=="restarted"}==set(p.CUMULATIVE)
    assert (fx.attempt/"install1-original-before-restart.json").read_bytes()==fx.install1["raw"]["snapshot"]
    assert fx.release["catalog_counts"]["total"]!=153
    assert p.RETRY_MAX not in (fx.attempt/"env.diff").read_text()
    assert p.RETRY_ENABLED not in (fx.attempt/"env.diff").read_text()


def test_install1_actual_continuation_not_fabricated_stop_start(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    previous=cumulative.load_install1(fx.release["binding"],fx.system)
    actual={tuple(row) for row in previous["proof"]["actual_actions"]}
    assert actual==cumulative.INSTALL1_ACTIONS
    assert ("restart","strategy") in actual and ("restart","oms") in actual
    assert ("stop","oms") not in actual and ("start","schwab-1m-v2") not in actual
    inferred=previous["proof"]["identity_derived_start"]
    assert inferred["command_receipt"]=="UNAVAILABLE"
    assert inferred["old_pid"]!=inferred["new_pid"]
    assert previous["proof"]["original_complete"] is False


@pytest.mark.parametrize("field",["MainPID","ExecMainStartTimestamp"])
def test_install1_v2_missing_command_requires_positive_new_identity(monkeypatch,tmp_path,field):
    fx,_,_=setup(monkeypatch,tmp_path)
    data=fx.release["binding"]
    old=json.loads(Path(data["install1"]["fleet_before"]["path"]).read_bytes())
    fx.system["schwab-1m-v2"][field]=old["schwab-1m-v2"][field]
    path=Path(data["install1"]["human_review"]["path"])
    review=json.loads(path.read_bytes())
    review["verified_pins"]["schwab-1m-v2"][field]=fx.system["schwab-1m-v2"][field]
    raw=p.canonical(review);path.write_bytes(raw)
    data["install1"]["human_review"]["sha256"]=p.digest(raw)
    with pytest.raises(p.Stop,match="Install1 restart not measured"):
        cumulative.load_install1(data,fx.system)


def test_first_stop_clock_fence_does_not_abort_stopped_sequence(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    after_midnight=datetime(2026,10,7,4,1,tzinfo=timezone.utc)
    actions=[]
    def action(action,name):
        actions.append((action,name))
        if (action,name)==("stop","schwab-1m-v2"):
            monkeypatch.setattr(fx,"now",lambda:after_midnight)
    # Isolate clock fencing from the separate live-freshness and identity controls.
    for method in ("initial","prepare","v2_gate","oms_gate","finish_proof","closeout","complete"):
        monkeypatch.setattr(fx,method,lambda:None)
    monkeypatch.setattr(fx,"gates",lambda completed:None)
    monkeypatch.setattr(fx,"checkpoint",lambda completed:None)
    monkeypatch.setattr(fx,"action",action)
    attended.Sequence(fx).run()
    assert actions==list(EXPECTED_PHASES)


def test_first_stop_clock_fence_still_blocks_before_stop(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    original_prepare=fx.prepare
    after_midnight=datetime(2026,10,7,4,1,tzinfo=timezone.utc)
    def prepare():
        original_prepare()
        monkeypatch.setattr(fx,"now",lambda:after_midnight)
    monkeypatch.setattr(fx,"prepare",prepare)
    with pytest.raises(p.Stop,match="first-write/stop window closed"):
        attended.Sequence(fx).run()
    assert not any(a[0]=="systemctl" and a[1] in {"stop","start","restart"} for a in fx.calls)


@pytest.mark.parametrize("fault",["stop project-mai-tai-strategy","start project-mai-tai-sch",
    "restart project-mai-tai-control","control_display_proof.py","systemd-analyze verify",
    "enable --now","armed_readonly.py","census_readonly.py","switch --detach","pip install"])
def test_literal_abort_no_recovery(monkeypatch,tmp_path,fault):
    fx,_,_=setup(monkeypatch,tmp_path); fx.fail=fault
    with pytest.raises(p.Stop): attended.Sequence(fx).run()
    assert any(fault in " ".join(args) for args in fx.calls), "fault boundary never reached"
    receipt=json.loads((fx.attempt/"STOP.json").read_bytes())
    assert receipt["actual"]==fx.system and not receipt["recovery_authorized"]
    assert not (fx.attempt/"COMPLETE.json").exists()
    assert not any("reset-failed" in a for a in fx.calls)


def traceback(reason="current closed candle absent",stamp="2026-10-06 23:10:00,000"):
    return (f"{stamp} ERROR project_mai_tai.market_data.schwab_v2_rest_client | schwab_v2 anchored poll failed for TEST\n"
        'Traceback (most recent call last):\n'
        '  File "/home/trader/project-mai-tai/src/project_mai_tai/market_data/schwab_v2_rest_client.py", line 236, in poll\n'
        '    bars, proof = await asyncio.to_thread(\n    ^^^^^^^^^^^^^^^\n'
        '  File "/usr/lib/python3.12/asyncio/threads.py", line 25, in to_thread\n'
        '    return await loop.run_in_executor(None, func_call)\n'
        '  File "/usr/lib/python3.12/concurrent/futures/thread.py", line 58, in run\n'
        '    result = self.fn(*self.args, **self.kwargs)\n'
        '  File "/home/trader/project-mai-tai/src/project_mai_tai/market_data/schwab_v2_rest_client.py", line 450, in fetch_session_history\n'
        f'    raise ValueError("{reason}")\nValueError: {reason}\n').splitlines()


@pytest.mark.parametrize("reason",linesrc.REASONS)
def test_exact_linesrc_disclosed_not_pass(reason):
    result=linesrc.grade("schwab-1m-v2",traceback(reason))
    assert result["classification"]=="ACCEPTED_OPEN_LINESRC1" and result["accepted_open_count"]==1
    raw="Overall: PASS (0 failed / 0 unknown)\nFinal call: PASS; all checks\n| Tracebacks | headers=0 | PASS |\n"
    text=restart_report.render(raw,result["receipts"])
    assert "Final call: ACCEPTED_OPEN_LINESRC1;" in text and "Overall: PASS" not in text
    assert "Final call: REAL FAILURE;" in restart_report.render("Final call: REAL FAILURE; other\n",result["receipts"])


@pytest.mark.parametrize("mutation",["clock","logger","reason","frame","source","chained","othererror","foreign"])
def test_linesrc_near_miss_blocks(mutation):
    lines=traceback(); service="schwab-1m-v2"
    if mutation=="clock": lines=traceback(stamp="2026-10-06 19:59:59,000")
    if mutation=="logger": lines[0]=lines[0].replace("schwab_v2_rest_client","other_client")
    if mutation=="reason": lines[-1]="ValueError: startup failed"
    if mutation=="frame": lines[9]=lines[9].replace("fetch_session_history","run")
    if mutation=="source": lines[2]=lines[2].replace("market_data/","services/")
    if mutation=="chained": lines.append("During handling of the above exception, another exception occurred:")
    if mutation=="othererror": lines.append("2026-10-06 23:10:01,000 ERROR startup broke")
    if mutation=="foreign": service="oms"
    with pytest.raises(p.Stop): linesrc.grade(service,lines)


def test_unbound_verify_no_effects(tmp_path):
    raw=p.canonical(dict(binding={},blocking_acceptance=[]))
    (tmp_path/"release.json").write_bytes(raw)
    with pytest.raises(p.Stop,match="unbound"): attended.verify(tmp_path,p.digest(raw),NOW)
    assert len(list(tmp_path.iterdir()))==1


@pytest.mark.parametrize("field,value",[("approved_sha","UNBOUND"),("tree","HEAD"),("box_sha","c"*40),
    ("merged_prs",[]),("control_restart",False),("source_combination_proof_sha256",""),("install1",{}),("old_helper_hashes",{})])
def test_binding_mutations(field,value):
    value_before=binding_value(); binding.validate_binding(value_before); value_before[field]=value
    with pytest.raises(p.Stop): binding.validate_binding(value_before)


@pytest.mark.parametrize("mutation",["legacy_six","missing","reordered","duplicate","first_sha","second_sha","individual_batch","batch_sha"])
def test_batch_merge_provenance(mutation):
    data=binding_value(); binding.validate_binding(data)
    rows=data["merged_prs"]
    if mutation=="legacy_six": data["merged_prs"]=[dict(pr=1102+i,merge_sha=f"{i+1:040x}") for i in range(6)]
    if mutation=="missing": rows[2]["governing_prs"].pop()
    if mutation=="reordered": rows[2]["governing_prs"].reverse()
    if mutation=="duplicate": rows[2]["governing_prs"]=[1104,1099,1105,1105]
    if mutation=="first_sha": rows[0]["merge_sha"]="a"*40
    if mutation=="second_sha": rows[1]["merge_sha"]="a"*40
    if mutation=="individual_batch": rows[2]["pr"]=1104
    if mutation=="batch_sha": rows[2]["merge_sha"]=rows[1]["merge_sha"]
    with pytest.raises(p.Stop): binding.validate_binding(data)


def test_env_four_updates_retry_retained():
    before=("UNCHANGED=1\n"+p.RETRY_ENABLED+"=true\n"+p.RETRY_MAX+"=0\n").encode()
    after=attended.env_candidate(before)
    assert after.startswith(before) and len(p.ENV_UPDATES)==4
    for raw in (before.replace(b"=0",b"=1"),before.replace(b"=true",b"=false"),before+(p.RETRY_MAX.lower()+"=0\n").encode()):
        with pytest.raises(p.Stop): attended.env_candidate(raw)


def test_prior_identity_or_hash_drift(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path); data=fx.release["binding"]
    cumulative.load_install1(data,fx.system)
    current=copy.deepcopy(fx.system); current["orb-schwab"]["MainPID"]+=1
    with pytest.raises(p.Stop): cumulative.load_install1(data,current)
    data["install1"]["snapshot"]["sha256"]="0"*64
    with pytest.raises(p.Stop): cumulative.load_install1(data,fx.system)


def test_rc2_retry_only_three_attempts_60(monkeypatch,tmp_path):
    fx=attended.Real(tmp_path,{},tmp_path); calls=[]; sleeps=[]
    monkeypatch.setattr(fx,"command",lambda *a,**k: calls.append(a) or SimpleNamespace(returncode=2,stdout=b"",stderr=b"raw"))
    monkeypatch.setattr(attended.time,"sleep",sleeps.append)
    with pytest.raises(p.Stop): fx.reader("census_readonly.py")
    assert len(calls)==3 and sleeps==[60,60]
    calls.clear()
    monkeypatch.setattr(fx,"command",lambda *a,**k: calls.append(a) or SimpleNamespace(returncode=1,stdout=b"",stderr=b"raw"))
    with pytest.raises(p.Stop): fx.reader("census_readonly.py")
    assert len(calls)==1


def test_measured_box_archive_fits_bounded_backup_before_source_change(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    fx.initial()
    original=fx.command
    measured=b"x"*40_673_280
    limits=[]
    def command(args,**kw):
        if "archive" in list(map(str,args)):
            limits.append(kw["limit"])
            monkeypatch.setattr(attended.subprocess,"run",lambda *a,**k:
                SimpleNamespace(returncode=0,stdout=measured,stderr=b""))
            return attended.Real.command(fx,args,**kw)
        return original(args,**kw)
    monkeypatch.setattr(fx,"command",command)
    fx.prepare()
    assert limits==[64*1024*1024]
    assert (fx.attempt/"source-before.tar").read_bytes()==measured
    assert fx.head==p.APP


def test_archive_over_64mib_still_refuses_before_output_persistence(monkeypatch,tmp_path):
    fx=attended.Real(tmp_path,{},tmp_path)
    monkeypatch.setattr(attended.subprocess,"run",lambda *a,**k:
        SimpleNamespace(returncode=0,stdout=b"x"*(attended.SOURCE_ARCHIVE_LIMIT+1),stderr=b""))
    with pytest.raises(p.Stop,match="command output exceeds bound"):
        fx.command(["git","archive",p.BOX],limit=attended.SOURCE_ARCHIVE_LIMIT)
    assert list(tmp_path.iterdir())==[]


def test_local_derivation_retains_incomplete_no_original_record(monkeypatch,tmp_path):
    import derive_install1
    fx,_,_=setup(monkeypatch,tmp_path)
    current=tmp_path/"current-fleet.json"
    current.write_bytes(p.canonical(fx.system))
    refs=copy.deepcopy(fx.release["binding"]["install1"])
    review=json.loads(Path(refs.pop("human_review")["path"]).read_bytes())
    human,record=derive_install1.derive(refs,current,review["provenance"],tmp_path/"derivation")
    assert human["original_complete"] is False and record["original_complete"] is False
    assert record["classification"]=="DERIVED_HUMAN_VERIFIED_INSTALL1_INCOMPLETE"
    assert record["snapshot_captured_at_utc"]==json.loads(Path(refs["snapshot"]["path"]).read_bytes())["captured_at_utc"]
    assert "COMPLETE" not in [path.name for path in (tmp_path/"derivation").iterdir()]


@pytest.mark.parametrize("mutation",["noquote","complete","pin","unknownaction","missingaction","before","snapshot"])
def test_prior_derivation_mutations_block(monkeypatch,tmp_path,mutation):
    fx,_,_=setup(monkeypatch,tmp_path)
    data=fx.release["binding"]
    refs=data["install1"]
    key="human_review"
    value=json.loads(Path(refs[key]["path"]).read_bytes())
    if mutation=="noquote": value["provenance"]["human_quote"]="not reviewed"
    if mutation=="complete": value["original_complete"]=True
    if mutation=="pin": value["verified_pins"]["oms"]["MainPID"]+=1
    if mutation in {"unknownaction","missingaction"}:
        key="journal"
        rows=Path(refs[key]["path"]).read_text().splitlines()
        if mutation=="missingaction": rows.pop()
        else: rows.append(json.dumps(dict(argv=["systemctl","restart","project-mai-tai-market-data.service"],rc=0)))
        raw=("\n".join(rows)+"\n").encode()
    elif mutation=="before":
        key="fleet_before"
        value=json.loads(Path(refs[key]["path"]).read_bytes())
        value["market-data"]["MainPID"]+=1
        raw=p.canonical(value)
    elif mutation=="snapshot":
        key="snapshot"
        value=json.loads(Path(refs[key]["path"]).read_bytes())
        value["services"]["oms"]["pid"]+=1
        raw=p.canonical(value)
    else: raw=p.canonical(value)
    Path(refs[key]["path"]).write_bytes(raw)
    refs[key]["sha256"]=p.digest(raw)
    if key!="human_review":
        review_path=Path(refs["human_review"]["path"])
        review=json.loads(review_path.read_bytes())
        review["evidence_hashes"][key]=p.digest(raw)
        review_path.write_bytes(p.canonical(review))
        refs["human_review"]["sha256"]=p.digest(review_path.read_bytes())
    with pytest.raises(p.Stop): cumulative.load_install1(data,fx.system)


def test_real_journal_receipt_hashes_not_just_declared_actions(tmp_path):
    path=tmp_path/"runner-journal.jsonl"
    command=p.canonical(dict(argv=["systemctl","restart","project-mai-tai-oms.service"],rc=0))
    receipt=tmp_path/"001-command.json"
    receipt.write_bytes(command)
    raw=(json.dumps(dict(receipt=receipt.name,sha256=p.digest(command),bytes=len(command)))+"\n").encode()
    actions,hashes=cumulative.journal_actions(path,raw)
    assert actions==[("restart","oms")] and hashes[str(receipt)]==p.digest(command)
    receipt.write_bytes(command+b" ")
    with pytest.raises(p.Stop): cumulative.journal_actions(path,raw)


@pytest.mark.parametrize("name",["oms","strategy","schwab-1m-v2"])
def test_dirty_stop_blocks_without_cancelled_error_reset(monkeypatch,tmp_path,name):
    fx,_,_=setup(monkeypatch,tmp_path)
    old=fx.command
    def command(args,**kw):
        result=old(args,**kw)
        if list(map(str,args))==["systemctl","stop","project-mai-tai-"+name+".service"]:
            fx.system[name].update(Result="exit-code",ExecMainStatus=1)
        return result
    monkeypatch.setattr(fx,"command",command)
    with pytest.raises(p.Stop,match="not clean"): attended.Sequence(fx).run()
    assert not any("reset-failed" in args for args in fx.calls)
    assert not any(args==["systemctl","start","project-mai-tai-oms.service"] for args in fx.calls)


@pytest.mark.parametrize("field",["broker_holdings","working_orders","managed_rows","virtual_rows","inflight_intents"])
def test_fresh_nonzero_flat_population_blocks_before_writes(monkeypatch,tmp_path,field):
    fx,_,_=setup(monkeypatch,tmp_path)
    original=fx.command
    def command(args,**kw):
        value=original(args,**kw)
        if any(str(arg).endswith("strict_flat_readonly.py") for arg in args):
            parsed=json.loads(value.stdout); parsed[field]=[{"controlled":"nonzero"}]
            value.stdout=p.canonical(parsed)
        return value
    monkeypatch.setattr(fx,"command",command)
    with pytest.raises(p.Stop,match="requires fresh actual flat"): attended.Sequence(fx).run()
    assert not any("switch" in args or "stop" in args for args in fx.calls)


def test_actual_catalog_owners_not_invented_new_rows_require_on():
    flags,numeric=catalogs()
    parsed=json.loads(flags)
    for row in parsed["flags"]:
        if row["name"] in {key.removeprefix("MAI_TAI_").lower() for key in p.NEW_ENV}:
            row.pop("require_process_env")
            row.pop("also_check_services")
    counts=catalog_policy.validate_catalogs(p.canonical(parsed),numeric,binding_value())
    assert counts["total"]==catalog_policy.validate_catalogs(flags,numeric,binding_value())["total"]-4
    next(row for row in parsed["flags"] if row["name"]=="webull_list_primary_reads_enabled")["expected"]=False
    with pytest.raises(p.Stop): catalog_policy.validate_catalogs(p.canonical(parsed),numeric,binding_value())


def test_paper_unknown_denominator_dynamic_not_relabelled_pass():
    catalog=[dict(name=name,owning_service=owner) for name,owner in
             sorted(closeout.PAPER_FLAGS|{("controlled","oms")})]
    rows=["UNKNOWN flag="+name+" service="+owner+" reason=momentum-paper is not active"
          if owner=="momentum-paper" else "PASS flag="+name+" service="+owner+" expected=true actual=true"
          for name,owner in sorted(closeout.PAPER_FLAGS|{("controlled","oms")})]
    raw="\n".join(rows)+"\nFinal call: UNKNOWN; checked=1/3 mismatches=0 unknown=2\n"
    paper=dict(MainPID=0,NRestarts=0,ActiveState="inactive",SubState="dead",Result="success",
               ExecMainCode=1,ExecMainStatus=0,InactiveEnterTimestamp="Tue 2026-10-06 13:40:00 UTC")
    receipt=closeout.flag_result(2,raw,catalog,paper_before=paper,paper_after=paper,now=NOW)
    assert receipt["total"]==3 and receipt["original_verdict"]=="UNKNOWN"
    with pytest.raises(p.Stop): closeout.flag_result(2,raw.replace("1/3","151/153"),catalog,
                                                      paper_before=paper,paper_after=paper,now=NOW)


@pytest.mark.parametrize("verdict,expected",[("ACCEPTED_OPEN_LINESRC1",0),("FAIL",2),("UNKNOWN",2),
    ("ACCEPTED_OPEN_LINESRC1; counted\nFinal call: FAIL",2)])
def test_daily_exact_accepted_open_not_pass_or_failure_waiver(verdict,expected):
    now=datetime(2026,10,7,10,20,tzinfo=timezone.utc)
    raw="Generated: 2026-10-07 06:20:00 EDT (2026-10-07 10:20:00 UTC)\nFinal call: "+verdict+"; measured\n"
    assert daily.outcome(0,raw,now,now,now.timestamp())==expected
    if expected==0:
        rc,receipt,_=daily.run_checks(now,{datetime(2026,1,1).date()},lambda:(0,"CONTROLLED",now),lambda date:(raw,now.timestamp()))
        assert rc==0 and receipt["verdict"]=="ACCEPTED_OPEN_LINESRC1"
        assert daily.outcome(0,raw.replace("2026-10-07","2026-10-06"),now,now,now.timestamp())==2


def test_existing_daily_root_blocks_no_overwrite(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    (tmp_path/"daily").mkdir()
    with pytest.raises(p.Stop,match="unexpected existing"): attended.Sequence(fx).run()
    assert not any("switch" in args or "stop" in args for args in fx.calls)


def test_scope_disallows_untouched_service_action(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    for name in ("orb-schwab","market-data","orb","momentum-paper","option-a-daily-guard",
                 "reconciler","market-capture","redis","postgresql"):
        with pytest.raises(p.Stop,match="out-of-scope"): fx.action("restart",name)
    assert fx.calls==[]


def test_control_startup_real_error_not_owner_page_gate():
    state=fleet()["control"]
    raw=dict(text=f"INFO:     Started server process [{state['MainPID']}]\nINFO:     Application startup complete.\nINFO:     Uvicorn running on http://127.0.0.1:8100 (Press CTRL+C to quit)\n",ranges=[])
    post_proof.control_logs(raw,state)
    with pytest.raises(p.Stop): post_proof.control_logs({**raw,"text":raw["text"]+"ERROR startup failed\n"},state)


def test_release_scope_and_combination_binding_before_package(monkeypatch):
    import make_release
    data=binding_value()
    proof=dict(verdict="VERIFIED",application=data["approved_sha"],tree=data["tree"],
               merged_prs=data["merged_prs"],reviewed_changed_paths=data["reviewed_changed_paths"],
               known_reject_noid_client_abort="ACCEPTED",mi_nxl_helper_disposition="UNCHANGED",
               full_same_environment_pair="PASS",all_on="PASS",mutations="PASS",ci="PASS")
    raw=p.canonical(proof); data["source_combination_proof_sha256"]=p.digest(raw)
    flags,numeric=catalogs()
    def git(*args):
        if args[0]=="rev-parse": return (data["tree"]+"\n").encode()
        if args[0]=="diff": return ("\n".join(data["reviewed_changed_paths"])+"\n").encode()
        if args[0]=="merge-base": return b""
        raise AssertionError(args)
    def blob(ref,path):
        if path=="ops/health/expected_flags.json": return flags
        if path=="ops/health/expected_numeric.json": return numeric
        if path.startswith(make_release.PREFIX): return (JOB/path.removeprefix(make_release.PREFIX)).read_bytes()
        return b"CONTROLLED candidate source"
    monkeypatch.setattr(make_release,"git",git); monkeypatch.setattr(make_release,"blob",blob)
    manifest,package=make_release.assemble("f"*40,data,raw)
    assert manifest["approved_sha"]==data["approved_sha"] and manifest["tree"]==data["tree"]
    assert manifest["artifacts"]=={n:p.digest(v) for n,v in package.items()}
    assert {"binding.json","derive_install1.py","restart_report.py"}<=set(package)
    assert manifest["catalog_counts"]["total"]!=153
    proof["all_on"]="UNMEASURED"
    raw=p.canonical(proof); data["source_combination_proof_sha256"]=p.digest(raw)
    with pytest.raises(p.Stop,match="acceptance incomplete"): make_release.assemble("f"*40,data,raw)


def test_wrapper_actual_official_parser_only_filters_exact_known_header(monkeypatch,tmp_path):
    spec=importlib.util.spec_from_file_location("test_install2_official",REPO/"ops/health/v2_restart_evidence.py")
    official=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=official; spec.loader.exec_module(official)
    original=official.parse_log_files
    report=tmp_path/"report.md"
    def main(argv):
        result=official.parse_log_files([("CONTROLLED",traceback())],since=NOW,service="schwab-1m-v2")
        assert result.traceback_times_utc==()
        raw="Overall: PASS (0 failed / 0 unknown)\nFinal call: PASS; measured\n"
        report.write_text(raw); print(raw,end="")
        return 0
    official.main=main
    loader=SimpleNamespace(exec_module=lambda _:None)
    monkeypatch.setattr(restart_report.importlib.util,"spec_from_file_location",lambda *a:SimpleNamespace(name=spec.name,loader=loader))
    monkeypatch.setattr(restart_report.importlib.util,"module_from_spec",lambda _:official)
    assert restart_report.main(["report","--output",str(report)])==0
    assert "Final call: ACCEPTED_OPEN_LINESRC1;" in report.read_text()
    sidecars=list(tmp_path.glob("report.md.*.linesrc.json"))
    assert len(sidecars)==1
    receipt=json.loads(sidecars[0].read_bytes())
    assert receipt["accepted_open_count"]==1
    # The original parser still reports the same actual raw header.
    assert len(original([("CONTROLLED",traceback())],since=NOW,service="schwab-1m-v2").traceback_times_utc)==1


def test_journal_non_action_large_archive_not_new_derivation_gate(tmp_path):
    raw=(json.dumps(dict(receipt="999-stdout.txt",sha256="a"*64,bytes=40_000_000))+"\n").encode()
    actions,refs=cumulative.journal_actions(tmp_path/"journal.jsonl",raw)
    assert actions==[] and refs=={}


def test_classifier_refusal_writes_official_failure_report_and_keeps_each_rerun(monkeypatch,tmp_path):
    spec=importlib.util.spec_from_file_location("test_morning_official",REPO/"ops/health/v2_restart_evidence.py")
    official=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=official; spec.loader.exec_module(official)
    report=tmp_path/"report.md"
    def refusal(*args):
        raise p.Stop("LINESRC exception before16 ET")
    monkeypatch.setattr(restart_report,"classify",refusal)
    original=official.parse_log_files
    def main(argv):
        result=official.parse_log_files([("CONTROLLED",traceback())],since=NOW,service="schwab-1m-v2")
        assert len(result.traceback_times_utc)==1
        raw="Overall: REAL FAILURE\nFinal call: REAL FAILURE; unclassified traceback\n"
        report.write_text(raw); print(raw,end="")
        return 1
    official.main=main
    loader=SimpleNamespace(exec_module=lambda _:None)
    monkeypatch.setattr(restart_report.importlib.util,"spec_from_file_location",lambda *a:SimpleNamespace(name=spec.name,loader=loader))
    monkeypatch.setattr(restart_report.importlib.util,"module_from_spec",lambda _:official)
    for _ in range(2):
        official.parse_log_files=original
        assert restart_report.main(["report","--output",str(report)])==1
    assert "Final call: REAL FAILURE;" in report.read_text()
    assert "not waived" in report.read_text()
    sidecars=list(tmp_path.glob("report.md.*.linesrc.json"))
    assert len(sidecars)==2 and len(list(tmp_path.glob("report.md.*.official-unaccepted-scope.txt")))==2
    for path in sidecars:
        receipt=json.loads(path.read_bytes())
        assert receipt["accepted_open_count"]==0 and len(receipt["unclassified"])==1


def test_official_unknown_before_output_writes_dated_unknown_not_missing_report(monkeypatch,tmp_path):
    spec=importlib.util.spec_from_file_location("test_unknown_official",REPO/"ops/health/v2_restart_evidence.py")
    official=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=official; spec.loader.exec_module(official)
    report=tmp_path/"report.md"
    def main(argv):
        print("Final call: UNKNOWN; evidence unreadable: untimestamped traceback",file=sys.stderr)
        return 2
    official.main=main
    loader=SimpleNamespace(exec_module=lambda _:None)
    monkeypatch.setattr(restart_report.importlib.util,"spec_from_file_location",lambda *a:SimpleNamespace(name=spec.name,loader=loader))
    monkeypatch.setattr(restart_report.importlib.util,"module_from_spec",lambda _:official)
    assert restart_report.main(["report","--output",str(report)])==2
    raw=report.read_text()
    assert "Generated: " in raw and "Final call: UNKNOWN;" in raw and "untimestamped traceback" in raw
    assert "Final call: PASS;" not in raw
    sidecar=json.loads(next(tmp_path.glob("report.md.*.linesrc.json")).read_bytes())
    assert sidecar["collector_stderr"]=="Final call: UNKNOWN; evidence unreadable: untimestamped traceback\n"


def test_catalog_duplicate_identity_blocks():
    row=dict(name="controlled",owning_service="oms")
    with pytest.raises(p.Stop,match="duplicated"): catalog_policy.identities([row,row])


def test_zero_result_nonzero_exit_is_not_clean_stop(monkeypatch,tmp_path):
    fx,_,_=setup(monkeypatch,tmp_path)
    original=fx.command
    def command(args,**kw):
        result=original(args,**kw)
        if list(map(str,args))==["systemctl","stop","project-mai-tai-schwab-1m-v2.service"]:
            fx.system["schwab-1m-v2"]["ExecMainStatus"]=1
        return result
    monkeypatch.setattr(fx,"command",command)
    with pytest.raises(p.Stop,match="not clean"): attended.Sequence(fx).run()
    assert not any("start" in args for args in fx.calls)

"""[codex] Recorded morning bytes; controlled operational boundaries, never venue proof."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import continuity_readonly as continuity
import flag_admission
import make_release
import release_policy as p
import runner
from ticket_inventory import require_idle

HERE = Path(__file__).parent
FIX = HERE / "fixtures"
ROOT = HERE.parents[3]
NOW = datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)


def env():
    return ("UNCHANGED=abc\n" + "\n".join(key + "=true" for key in p.LIVE_KEYS)
            + "\n" + p.FLAG + "=false\n").encode()


def fleet():
    result = {name: dict(MainPID=100 + index, NRestarts=0, ActiveState="active", SubState="running",
        Result="success", ExecMainCode=1, ExecMainStatus=0, InvocationID=f"{index+1:032x}",
        ExecMainStartTimestamp="Wed 2026-10-07 11:00:00 UTC", ExecMainStartTimestampMonotonic=100,
        FragmentPath="CONTROLLED", DropInPaths="", EnvironmentFiles="CONTROLLED", InactiveEnterTimestamp="")
        for index, name in enumerate(p.SERVICES)}
    result[p.V2].update(MainPID=917354, ExecMainStartTimestamp="Wed 2026-10-07 11:14:07 UTC")
    result["orb-schwab"].update({key: int(value) if key in {"MainPID", "NRestarts"} else value
                                for key, value in p.ACK_STATE.items()})
    result["momentum-paper"].update(MainPID=0, ActiveState="inactive", SubState="dead",
        InactiveEnterTimestamp="Wed 2026-10-07 13:40:00 UTC")
    return result


def new_v2():
    value = deepcopy(fleet()[p.V2])
    value.update(MainPID=999001, ExecMainStartTimestamp="Wed 2026-10-07 23:30:00 UTC",
                 InvocationID="f" * 32, ExecMainStartTimestampMonotonic=200)
    return value


def catalog():
    return (ROOT / "ops/health/expected_flags.json").read_bytes(), (FIX / "restart_evidence/expected_numeric.json").read_bytes()


def test_recorded_byte_hashes_and_actual_catalog_147_plus10():
    assert p.digest((FIX / "preopen.sh").read_bytes()) == p.GATE_SHA
    assert p.digest((FIX / "preopen-daily/runtime.json").read_bytes()) == "ef103a82b59cf8b93e6b8bb7dd613edc4e3f956f935e19e393550e89b7ebda1c"
    assert p.catalog_counts(*catalog()) == dict(boolean=147, numeric=10, total=157)
    with pytest.raises(p.Stop):
        p.catalog_counts(catalog()[0], (ROOT / "ops/health/expected_numeric.json").read_bytes())


def test_env_only_literal_false_to_true_byte_change():
    assert p.env_candidate(env()) == env().replace((p.FLAG + "=false").encode(), (p.FLAG + "=true").encode())


@pytest.mark.parametrize("damage", ["duplicate", "alias", "missing", "already_true", "quoted", "malformed", "old_key"])
def test_env_invalid_or_live_key_drift_refuses(damage):
    raw = env()
    if damage == "duplicate": raw += (p.FLAG + "=false\n").encode()
    elif damage == "alias": raw += (p.FLAG.lower() + "=false\n").encode()
    elif damage == "missing": raw = raw.replace((p.FLAG + "=false\n").encode(), b"")
    elif damage == "already_true": raw = raw.replace(b"=false", b"=true")
    elif damage == "quoted": raw = raw.replace(b"=false", b"='false'")
    elif damage == "malformed": raw += b"BROKEN='\n"
    else: raw = raw.replace((p.LIVE_KEYS[0] + "=true").encode(), (p.LIVE_KEYS[0] + "=false").encode())
    with pytest.raises(p.Stop): p.env_candidate(raw)


@pytest.mark.parametrize("stamp,green", [("2026-10-07T19:59:59+00:00",False),
    ("2026-10-07T20:00:00+00:00",False), ("2026-10-07T20:00:01+00:00",True),
    ("2026-10-08T03:59:59+00:00",True), ("2026-10-08T04:00:00+00:00",False)])
def test_first_stop_window_exact_ET_boundaries(stamp, green):
    if green: p.first_stop_window(datetime.fromisoformat(stamp))
    else:
        with pytest.raises(p.Pending): p.first_stop_window(datetime.fromisoformat(stamp))


@pytest.mark.parametrize("phase", [0,1,2])
def test_all_untouched_identities_exact_including_ack_NRestarts1(phase):
    before, after = fleet(), fleet()
    if phase == 1: after[p.V2].update(MainPID=0, ActiveState="inactive", SubState="dead")
    if phase == 2: after[p.V2] = new_v2()
    p.states(before, after, phase)
    after["oms"]["MainPID"] += 1
    with pytest.raises(p.Stop): p.states(before, after, phase)


@pytest.mark.parametrize("field,value", [("Result","signal"),("ExecMainStatus",1),("NRestarts",1),
    ("SubState","failed"),("MainPID",999)])
def test_nonclean_stop_refuses_without_reset_waiver(field,value):
    after = fleet()
    after[p.V2].update(MainPID=0, ActiveState="inactive", SubState="dead")
    after[p.V2][field] = value
    with pytest.raises(p.Stop): p.states(fleet(), after, 1)


def test_actual_morning_patch_only_new_v2_scope_keeps_ack_dynamic_date_paper():
    raw = (FIX / "preopen.sh").read_bytes()
    candidate = p.gate_candidate(raw, new_v2(), "/CONTROLLED/snapshot.json", "/CONTROLLED/record.json")
    assert subprocess.run(["bash","-n"], input=candidate, capture_output=True).returncode == 0
    text = candidate.decode()
    assert "EXPECTED_PID=999001" in text and "EXPECTED_SHA=" + p.APP in text
    assert text.count("--restarted ") == 1 and "--restarted schwab-1m-v2" in text
    assert "--expect-flag 'oms:" not in text
    assert "upgrade_ack.py" in text and "daily.py paper" in text
    assert 'EXPECTED_DATE="$(TZ=America/New_York date +%F)"' in text
    for prefix in ("OMS_", "STRATEGY_", "ORB_", "ORB_SCHWAB_", "CONTROL_", "MARKET_DATA_", "PAPER_"):
        for key in ("PID","START"):
            import re
            pattern = "(?m)^EXPECTED_" + prefix + key + "=.*$"
            assert re.search(pattern,text)[0] == re.search(pattern,raw.decode())[0]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name,path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_ack_new_app_binding_exact_identity_no_counter_reset(tmp_path):
    helper, data = p.repin_ack((FIX / "preopen-daily/upgrade_ack.py").read_bytes(),
                             (FIX / "preopen-daily/upgrade-ack.json").read_bytes())
    file = tmp_path / "ack.py"
    file.write_bytes(helper)
    ack = load("controlled_oct7_ack",file)
    record = json.loads(data)
    assert ack.APP == p.APP and record["application"] == p.APP
    assert ack.validate(record,deepcopy(p.ACK_STATE),p.APP)["state"]["NRestarts"] == "1"
    for key in p.ACK_STATE:
        state=deepcopy(p.ACK_STATE); state[key]="different"
        with pytest.raises(ValueError): ack.validate(record,state,p.APP)
    with pytest.raises(ValueError): ack.validate(record,p.ACK_STATE,p.BOX)


def test_unknown_orb_log_stays_unknown_no_ack_render_PASS():
    ack = load("recorded_oct7_ack",FIX / "preopen-daily/upgrade_ack.py")
    raw = "Final call: UNKNOWN; unreadable orb log\n| Other | unreadable | UNKNOWN |\n\nUnknowns:\n- untimestamped orb\n"
    rendered, allowed = ack.render(raw,dict(state=p.ACK_STATE))
    assert not allowed and "Final call: UNKNOWN;" in rendered


def held_text():
    return "2026-10-07 23:30:01,000 WARNING [V2-BOOT-HOLD] HELD restoration_complete=0 armed_segments_observed=0 dangerous_observed=0\n"


def official(extra="", unknown=""):
    return ("Overall: FAIL\nFinal call: FAIL; held\n"
            "| REST warmup | none | FAIL |\n| BOOT-HOLD released | none | FAIL |\n"
            "| Bar continuity | actual | PASS |\n\nFailures:\n"
            "- no post-restart restoration_complete=1 line\n"
            "- no literal post-restart BOOT-HOLD release with restoration_complete=1\n" + extra + unknown)


def test_after16_held_pair_disclosed_not_collector_PASS_or_new_daily_verdict():
    held=p.held_logs(held_text(),new_v2(),NOW+timedelta(seconds=2))
    raw=official()
    result=p.report_disposition(1,raw,held)
    assert result.startswith(raw) and "Final call: FAIL;" in result
    assert "not collector PASS" in result and held["next_session"].startswith("2026-10-08")


@pytest.mark.parametrize("damage", ["error","traceback","stale","release","wrong_day"])
def test_held_proof_actual_startup_blockers(damage):
    text=held_text(); now=NOW+timedelta(seconds=2); state=new_v2()
    if damage=="error": text += "2026-10-07 23:30:02,000 ERROR boom\n"
    elif damage=="traceback": text += "Traceback (most recent call last):\n"
    elif damage=="stale": now += timedelta(seconds=121)
    elif damage=="release": text += "2026-10-07 23:30:02,000 [V2-BOOT-HOLD] released restoration_complete=1\n"
    else: state["ExecMainStartTimestamp"]="Tue 2026-10-06 23:30:00 UTC"
    with pytest.raises(p.Stop): p.held_logs(text,state,now)


@pytest.mark.parametrize("extra", ["- another failure\n", "- REST warmup completion population is inconsistent or unproven\n"])
def test_other_official_failures_never_become_mechanical_complete(extra):
    with pytest.raises(p.Stop): p.report_disposition(1,official(extra),dict(verdict="HELD_AFTER16_NOT_RESTORATION_PASS",literal_hold="CONTROLLED"))


def bar(symbol,minute):
    return dict(symbol=symbol,bar_time=minute,created_at=minute+timedelta(minutes=1),updated_at=minute+timedelta(minutes=1))


def test_measured_restart_overlap_minutes_and_outside_interval_fence():
    stop=NOW+timedelta(seconds=10); start=NOW+timedelta(seconds=50)
    rows=[bar("RETO",NOW-timedelta(minutes=1)),bar("RETO",NOW+timedelta(minutes=1))]
    value=continuity.grade(rows,["RETO"],stop,start,NOW+timedelta(minutes=2))
    assert value["verdict"]=="MEASURED_RESTART_WINDOW"
    assert value["per_stock"][0]["missing_persisted_minutes_utc"]==[NOW.isoformat()]
    assert value["independent_print_status"]=="UNMEASURED"
    rows[-1]=bar("RETO",NOW+timedelta(minutes=2))
    with pytest.raises(p.Stop): continuity.grade(rows,["RETO"],stop,start,NOW+timedelta(minutes=3))


def test_pending_not_false_zero_or_quiet_and_no_live_at_stop_claim():
    value=continuity.grade([bar("RETO",NOW-timedelta(minutes=1))],["RETO"],NOW+timedelta(seconds=10),
                           NOW+timedelta(seconds=50),NOW+timedelta(minutes=2))
    assert value["pending_symbols"]==["RETO"] and value["verdict"]=="PENDING_FIRST_CLOSED_BAR"
    quiet=continuity.grade([], ["RETO"],NOW,NOW+timedelta(seconds=20),NOW+timedelta(minutes=2))
    assert quiet["per_stock"][0]["no_live_at_stop"]=="no restart-spanning claim"


def test_official_known_gap_preserved_with_symbol_minute_exact_binding():
    raw=official("- REST backfill coverage not proven for 1 independently printed minute(s)\n")
    detail="RETO status=MISSING_PRINTED_MINUTE missing_printed=1 minutes=2026-10-07 19:30:00 EDT (2026-10-07 23:30:00 UTC);"
    raw=raw.replace("| Bar continuity | actual | PASS |","| Bar continuity | "+detail+" | FAIL |")
    proof=dict(verdict="MEASURED_RESTART_WINDOW",per_stock=[dict(symbol="RETO",missing_persisted_minutes_utc=[NOW.isoformat()])])
    held=dict(verdict="HELD_AFTER16_NOT_RESTORATION_PASS",literal_hold="CONTROLLED")
    # Production format_moment includes both zones; both must be exact, not loose substring.
    assert p.report_disposition(1,raw,held,proof).startswith(raw)
    for changed in (raw.replace("RETO status=","MI status="),raw.replace("19:30:00 EDT","19:31:00 EDT"),
                    raw.replace("missing_printed=1 minutes=","missing_printed=0 minutes=")):
        with pytest.raises(p.Stop): p.report_disposition(1,changed,held,proof)
    with pytest.raises(p.Stop): p.report_disposition(1,raw.replace("23:30:00 UTC","23:31:00 UTC"),held,proof)


def test_pending_official_unknown_is_specific_future_not_unreadable_ORB():
    held=dict(verdict="HELD_AFTER16_NOT_RESTORATION_PASS",literal_hold="CONTROLLED")
    proof=dict(verdict="PENDING_FIRST_CLOSED_BAR",pending_symbols=["RETO"])
    unknown="\nUnknowns:\n- REST backfill continuity UNKNOWN: RETO: no fresh post-restart warmup bar in persisted strategy history\n"
    raw=official(unknown=unknown)
    assert p.report_disposition(2,raw,held,proof).startswith(raw)
    with pytest.raises(p.Stop): p.report_disposition(2,official(unknown="\nUnknowns:\n- untimestamped orb\n"),held,proof)


def flag_output(rows):
    ids=p.catalog_ids(rows)
    paper=flag_admission.PAPER_FLAGS
    lines=[("UNKNOWN flag="+name+" service="+owner+" reason=momentum-paper is not active" if (name,owner) in paper
            else "PASS flag="+name+" service="+owner+" expected=true actual=true source=env") for name,owner in ids]
    return "\n".join(lines)+"\nFinal call: UNKNOWN; checked=155/157 mismatches=0 unknown=2\n"


def test_existing_dated_paper_unknown_two_truthful_no_start_for_green():
    flags,numeric=catalog()
    rows=json.loads(flags)["flags"]+json.loads(numeric)["settings"]
    raw=flag_output(rows); paper=fleet()["momentum-paper"]
    proof=flag_admission.flag_result(2,raw,rows,paper_before=paper,paper_after=paper,now=NOW)
    assert (proof["checked"],proof["total"],proof["unknown"])==(155,157,2)
    paper=deepcopy(paper); paper["NRestarts"]=1
    with pytest.raises(p.Stop): flag_admission.flag_result(2,raw,rows,paper_before=paper,paper_after=paper,now=NOW)


class FX:
    def __init__(self,fail=None): self.calls=[]; self.claimed=False; self.fail=fail; self.clock=NOW
    def now(self): return self.clock
    def hit(self,name):
        self.calls.append(name)
        if name==self.fail: raise p.Stop("CONTROLLED failure")
    def initial(self): self.hit("initial")
    def claim(self): self.claimed=True; self.hit("claim")
    def prepare(self): self.hit("prepare")
    def gates(self,phase): self.hit("gates"+str(phase))
    def v2_gate(self): self.hit("v2_gate")
    def action(self,action): self.hit(action)
    def checkpoint(self,phase): self.hit("checkpoint"+str(phase))
    def prove(self): self.hit("prove")
    def closeout(self): self.hit("closeout")
    def complete(self): self.hit("complete")
    def abort(self,*args): self.calls.append("abort")


def test_literal_stop_start_only_and_after_stop_midnight_not_abort():
    fx=FX()
    old=fx.checkpoint
    def crossed(phase):
        old(phase)
        if phase==1: fx.clock=datetime(2026,10,8,4,1,tzinfo=timezone.utc)
    fx.checkpoint=crossed
    runner.Sequence(fx).run()
    assert fx.calls==["initial","claim","prepare","gates0","v2_gate","stop","checkpoint1",
                      "gates1","start","checkpoint2","prove","closeout","complete"]


@pytest.mark.parametrize("failure", ["initial","claim","prepare","gates0","v2_gate","stop","checkpoint1",
                                    "gates1","start","checkpoint2","prove","closeout","complete"])
def test_every_abort_no_recovery_or_repeat_start(failure):
    fx=FX(failure)
    with pytest.raises(p.Stop): runner.Sequence(fx).run()
    assert fx.calls[-1]=="abort" and fx.calls.count("start")<=1
    if failure in {"initial","claim","prepare","gates0","v2_gate","stop","checkpoint1","gates1"}:
        assert "start" not in fx.calls


@pytest.mark.parametrize("codes,reads,sleeps", [([0],1,0),([2,0],2,1),([2,2,0],3,2),([2,2,2],3,2),([1],1,0),([124],1,0)])
def test_exact_rc2_retry_three_by60_raw_commands_retained(monkeypatch,codes,reads,sleeps,tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    responses=[subprocess.CompletedProcess([],code,b"CONTROLLED raw stdout",b"" if code==0 else b"CONTROLLED stderr") for code in codes]
    fx.command=Mock(side_effect=responses)
    sleep=Mock(); monkeypatch.setattr(runner.time,"sleep",sleep)
    if codes[-1]==0: assert fx.reader("strict_flat_readonly.py")==b"CONTROLLED raw stdout"
    else:
        with pytest.raises(p.Stop): fx.reader("strict_flat_readonly.py")
    assert fx.command.call_count==reads and sleep.call_count==sleeps
    assert all(call.args==(60,) for call in sleep.call_args_list)


def test_native_clock_pending_before_claim_no_override(monkeypatch,tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.flat=Mock(); fx.reader=Mock(); fx.armed=Mock()
    fx.command=Mock(return_value=subprocess.CompletedProcess([],1,b"  [BLOCK] it is before 18:00 ET.\n",b""))
    with pytest.raises(p.Pending): fx.v2_gate()
    assert fx.flat.call_args_list[0].args==("oms",) and fx.flat.call_args_list[1].args==("strategy",)
    assert len(fx.command.call_args.args[0])==1
    fx.claimed=True
    with pytest.raises(p.Stop): fx.v2_gate()


def test_exclusive_attempt_seal_cannot_duplicate(tmp_path):
    marker=tmp_path/"write-started.json"
    runner.exclusive(marker,b"CONTROLLED")
    with pytest.raises(FileExistsError): runner.exclusive(marker,b"second")
    assert marker.read_bytes()==b"CONTROLLED"


def test_source_archive_bound_measured42MB_fits64(monkeypatch,tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    monkeypatch.setattr(runner.subprocess,"run",lambda *args,**kwargs: subprocess.CompletedProcess([],0,b"x"*42_393_600,b""))
    fx.receipt=Mock()
    assert len(fx.command(["git","archive",p.BOX],limit=64_000_000).stdout)==42_393_600
    with pytest.raises(p.Stop): fx.command(["git","archive",p.BOX],limit=40_000_000)


def test_no_bulk_snapshot_or_service_writes_in_units():
    service=(HERE/"project-mai-tai-linesrc1-20261007.service").read_text()
    timer=(HERE/"project-mai-tai-linesrc1-20261007.timer").read_text()
    assert "ConditionPathExists=!" in service and "write-started.json" in service and "Restart=no" in service
    assert "ConditionPathExists=/home/trader/after-hours/2026-10-07/linesrc1-1a70da19/job/approval.json" in service
    assert "approval.pending.json" not in (HERE/"make_release.py").read_text()
    assert "SuccessExitStatus=75" in service and "Requires=" not in service
    assert "2026-10-07 16..23:" in timer and "Persistent=false" in timer
    assert "project-mai-tai-preopen.timer" not in timer


@pytest.mark.parametrize("phase", ["requested","price_wait","submitting"])
def test_full_ticket_phase_inventory_no_old11_terminal_population(phase):
    row=dict(id="CONTROLLED",payload=dict(phase=phase,revision=1,slot="first",old=dict(
        broker_account_name="live:orb",symbol="RETO",strategy_code="schwab_1m_v2",side="buy",client_order_id="CONTROLLED",metadata={})))
    with pytest.raises(ValueError): require_idle([row])
    row["payload"]["phase"]="held_unknown"
    assert require_idle([row])["total"]==1
    assert "clears_unknown_ownership=False" in (HERE/"census_readonly.py").read_text()


def test_proof_calls_direct_official_collector_before_ack_repin(monkeypatch,tmp_path):
    import log_ranges
    job=tmp_path/"job"; job.mkdir()
    attempt=tmp_path/"attempt"; attempt.mkdir()
    fx=runner.Real(job,{},attempt)
    fx.started=new_v2(); fx.log_base={}; fx.start_returned=NOW; fx.stop_started=NOW
    fx.now=lambda: NOW+timedelta(seconds=2)
    fx.gates=Mock(); fx.proc=Mock(); fx.flaggate=Mock()
    fx.measure_continuity=lambda: dict(verdict="N/A_OFF_SESSION")
    snapshot=dict(captured_at_utc=NOW.isoformat(),v2_watchlist=dict(symbols=["RETO"]),services=fleet())
    (attempt/"before-restart.json").write_bytes(p.canonical(snapshot))
    monkeypatch.setattr(log_ranges,"logs",lambda base: {p.V2: dict(text=held_text())})
    fx.command=Mock(return_value=subprocess.CompletedProcess([],1,official().encode(),b""))
    fx.prove()
    args=fx.command.call_args.args[0]
    assert args[2]==runner.REPO/"ops/health/v2_restart_evidence.py"
    assert "report" in args and runner.DAILY/"restart_report.py" not in args
    assert (attempt/"v2-only-install-record.json").exists()
    assert list(attempt.glob("*-official-raw-report.md"))[0].read_text()==official()


def test_actual_closeout_bundle_and_imported_daily_ack_contract(monkeypatch,tmp_path):
    dailyroot=tmp_path/"daily"; dailyroot.mkdir()
    before=json.loads((FIX/"preopen-daily/runtime.json").read_bytes())
    for name,sha in before["artifacts"].items():
        raw=(FIX/"preopen-daily"/name).read_bytes()
        assert p.digest(raw)==sha
        (dailyroot/name).write_bytes(raw)
    (dailyroot/"runtime.json").write_bytes((FIX/"preopen-daily/runtime.json").read_bytes())
    gate=tmp_path/"preopen.sh"; gate.write_bytes((FIX/"preopen.sh").read_bytes()); gate.chmod(0o700)
    job=tmp_path/"job"; job.mkdir(); (job/"release.json").write_bytes(b'{"controlled_release":true}\n')
    attempt=tmp_path/"attempt"; attempt.mkdir()
    for name in ("v2-only-install-record.json","before-restart.json","sealed-actions.json"):
        (attempt/name).write_bytes(b'{"controlled_not_production":true}\n')
    fx=runner.Real(job,{},attempt); fx.started=new_v2(); fx.record=attempt/"v2-only-install-record.json"
    current=fleet(); current[p.V2]=fx.started
    fx.fleet=lambda: current; fx.baseline=Mock(); fx.command=Mock()
    fx.replace=lambda path,raw: path.write_bytes(raw)
    monkeypatch.setattr(runner,"DAILY",dailyroot); monkeypatch.setattr(runner,"GATE",gate)
    fx.closeout()
    after=json.loads((dailyroot/"runtime.json").read_bytes())
    assert after["approved_sha"]==p.APP and after["catalog_counts"]==before["catalog_counts"]
    assert after["gate_uid"]==before["gate_uid"]
    assert all(after["evidence_inputs"][path]==sha for path,sha in before["evidence_inputs"].items())
    assert len(after["evidence_inputs"])==len(before["evidence_inputs"])+3
    changed={name for name in after["artifacts"] if after["artifacts"][name]!=before["artifacts"][name]}
    assert changed=={"binding.json","upgrade_ack.py","upgrade-ack.json"}
    for name,sha in after["artifacts"].items():
        assert p.digest((dailyroot/name).read_bytes())==sha
    oldpolicy=load("controlled_actual_daily_policy",dailyroot/"release_policy.py")
    assert oldpolicy.APP==p.APP and oldpolicy.TREE==p.TREE
    monkeypatch.setitem(sys.modules,"release_policy",oldpolicy)
    daily=load("controlled_actual_daily",dailyroot/"daily.py")
    monkeypatch.setitem(sys.modules,"daily",daily)
    daily.ROOT=dailyroot; daily.GATE=gate; daily.REPO=ROOT
    ack=load("controlled_actual_repinned_ack",dailyroot/"upgrade_ack.py")
    originalstat=Path.stat
    def controlled_owner(path,*args,**kwargs):
        result=originalstat(path,*args,**kwargs)
        if path==gate or dailyroot in path.parents:
            values=list(result); values[4]=before["gate_uid"] if path==gate else 0
            return os.stat_result(values)
        return result
    monkeypatch.setattr(Path,"stat",controlled_owner)
    monkeypatch.setattr(daily.os,"geteuid",lambda: 0)
    class HistoricalEvidence:
        # Exact historical hashes were recorded, but those remote bytes are NOT
        # exported. Only this old-evidence IO is controlled, never proof of them.
        def __init__(self,path): self.path=path
        def is_symlink(self): return False
        def read_bytes(self): return ("CONTROLLED_OLD_EVIDENCE:"+self.path).encode()
    daily.Path=lambda path: HistoricalEvidence(str(path)) if str(path) in before["evidence_inputs"] else Path(path)
    daily.digest=lambda raw: (before["evidence_inputs"][raw.decode().split(":",1)[1]]
        if raw.startswith(b"CONTROLLED_OLD_EVIDENCE:") else p.digest(raw))
    assert daily.verify_runtime()["approved_sha"]==p.APP
    ack.Path=lambda path: dailyroot/"upgrade-ack.json" if str(path)=="/home/trader/preopen-daily/upgrade-ack.json" else Path(path)
    state=deepcopy(p.ACK_STATE)
    def query(args,**kwargs):
        output="\n".join(key+"="+value for key,value in state.items()) if args[0]=="systemctl" else p.APP+"\n"
        return subprocess.CompletedProcess(args,0,output,"")
    monkeypatch.setattr(ack.subprocess,"run",query)
    assert ack.current()["state"]["NRestarts"]=="1"
    state["InvocationID"]="0"*32
    with pytest.raises(ValueError): ack.current()
    (dailyroot/"binding.json").write_bytes(b"{}\n")
    with pytest.raises(oldpolicy.Stop): daily.verify_runtime()


def test_abort_notification_errors_leave_actual_STOP_no_service_action(monkeypatch,tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.fleet=lambda: fleet()
    fx.deployment_note=Mock(side_effect=OSError("CONTROLLED note unavailable"))
    fx.command=Mock(side_effect=TimeoutError("CONTROLLED notification timeout"))
    fx.abort("start-v2",1)
    value=json.loads((tmp_path/"STOP.json").read_bytes())
    assert value["phase"]=="start-v2" and value["actual"][p.V2]["MainPID"]==917354
    assert not value["recovery_authorized"]
    assert list(tmp_path.glob("*-alert-unreadable.json"))
    assert "systemctl" not in fx.command.call_args.args[0]


def test_local_package_exact_bytes_standing_GO_present_and_tamper_refused(monkeypatch,tmp_path):
    plan="a"*40
    baseline=dict(fleet_before=fleet(),environment_sha256="b"*64,
                  authorization_provenance="CONTROLLED local assembly; not an actual production baseline")
    def git(*args):
        if args[0]=="rev-parse": return (p.TREE+"\n").encode()
        if args[0]=="merge-base": return b""
        if args[0]=="diff": return b"" if "src" in args else (make_release.REL+"runner.py\n").encode()
        assert args[0]=="show"
        sha,path=args[1].split(":",1)
        return (HERE/path.removeprefix(make_release.REL)).read_bytes() if sha==plan else (ROOT/path).read_bytes()
    monkeypatch.setattr(make_release,"git",git)
    target=tmp_path/"package"
    result=make_release.assemble(plan,baseline,target)
    assert result["production_run"] is False and result["catalog_counts"]["total"]==157
    assert (target/"approval.json").exists() and not (target/"approval.pending.json").exists()
    release=json.loads((target/"release.json").read_bytes())
    assert release["environment_sha256"]==baseline["environment_sha256"]
    assert all(p.digest((target/name).read_bytes())==sha for name,sha in release["artifacts"].items())
    monkeypatch.setattr(make_release,"git",lambda *args: b"tampered" if args[0]=="show" else git(*args))
    with pytest.raises(p.Stop): make_release.assemble(plan,baseline,tmp_path/"refused")
    assert not (tmp_path/"refused").exists()


@pytest.mark.parametrize("damage", ["none","artifact","approval","release_bytes","scope","inventory","symlink","writable"])
def test_published_manifest_admission_refuses_each_tamper(monkeypatch,tmp_path,damage):
    target=tmp_path/"package"; target.mkdir(mode=0o700)
    raw={name:(HERE/name).read_bytes() for name in make_release.ARTIFACTS}
    for name,value in raw.items():
        (target/name).write_bytes(value); (target/name).chmod(0o600)
    release=dict(application=p.APP,tree=p.TREE,box=p.BOX,date_et=p.DAY,scope=p.SCOPE,
                 plan_commit="a"*40,artifacts={name:p.digest(value) for name,value in raw.items()})
    if damage=="scope": release["application"]=p.BOX
    if damage=="inventory": del release["artifacts"]["strict_flat_readonly.py"]
    release_raw=p.canonical(release); expected=p.digest(release_raw)
    (target/"release.json").write_bytes(release_raw)
    decision=dict(authority="operator-standing-mechanics-authority",decision="APPROVED",
                  application=p.APP,plan_commit="a"*40,date_et=p.DAY,scope=p.SCOPE,release_sha256=expected)
    if damage=="approval": decision["plan_commit"]="b"*40
    (target/"approval.json").write_bytes(p.canonical(decision))
    if damage=="artifact": (target/"runner.py").write_bytes(b"different")
    if damage=="release_bytes": (target/"release.json").write_bytes(release_raw+b" ")
    if damage=="symlink":
        (target/"runner.py").unlink(); (target/"runner.py").symlink_to(HERE/"runner.py")
    if damage=="writable": (target/"approval.json").chmod(0o666)
    original=Path.stat
    def root_owned(path,*args,**kwargs):
        result=original(path,*args,**kwargs)
        if path==target or target in path.parents:
            values=list(result); values[4]=0; return os.stat_result(values)
        return result
    monkeypatch.setattr(Path,"stat",root_owned)
    if damage=="none": assert runner.verify(target,expected)==release
    else:
        with pytest.raises(p.Stop): runner.verify(target,expected)


def test_readme_closeout_only_installer_timer_not_daily_and_not_zero_marker():
    text=(HERE/"README.md").read_text()
    assert "After COMPLETE or STOP the runner disables/stops only its installer timer" in text
    assert "Existing daily preopen timer remains enabled and unchanged" in text
    assert "zero such markers is NOT promised" in text
    assert "Parent stages exact approval.json NOW" in text and "UNATTENDED" in text


@pytest.mark.parametrize("wire_rc,readback_bad", [(0,False),(1,False),(0,True)])
def test_prepare_git_success_notice_requires_exact_readback_before_env(monkeypatch,tmp_path,wire_rc,readback_bad):
    job=tmp_path/"job"; job.mkdir()
    attempt=tmp_path/"attempt"; attempt.mkdir()
    gate=tmp_path/"gate"; gate.write_bytes(b"CONTROLLED gate")
    envfile=tmp_path/"env"; envfile.write_bytes(env())
    logfile=tmp_path/"log"; logfile.write_bytes(b"CONTROLLED log")
    dailyroot=tmp_path/"daily"; dailyroot.mkdir()
    for name in ("runtime.json","binding.json","upgrade_ack.py","upgrade-ack.json"):
        (dailyroot/name).write_bytes((FIX/"preopen-daily"/name).read_bytes())
    monkeypatch.setattr(runner,"GATE",gate); monkeypatch.setattr(runner,"ENV",envfile)
    monkeypatch.setattr(runner,"DAILY",dailyroot); monkeypatch.setattr(runner,"LOG",logfile)
    fx=runner.Real(job,{},attempt)
    fx.gates=Mock(); fx.baseline=Mock(); fx.flat=Mock()
    fx.env_before=env(); fx.env_after=p.env_candidate(env())
    fx.source=Mock(side_effect=p.Stop("CONTROLLED readback bad") if readback_bad else None)
    fx.replace=lambda path,raw: path.write_bytes(raw)
    def invoke(args,**kwargs):
        if "switch" in args:
            assert kwargs["check"] is False
            return subprocess.CompletedProcess(args,wire_rc,b"",b"HEAD is now at f9c9bd33 controlled notice\n")
        return subprocess.CompletedProcess(args,0,b"CONTROLLED archive" if "archive" in args else b"",b"")
    fx.command=Mock(side_effect=invoke)
    if wire_rc or readback_bad:
        with pytest.raises(p.Stop): fx.prepare()
        assert envfile.read_bytes()==env()
    else:
        fx.prepare()
        fx.source.assert_called_once_with(p.APP)
        assert envfile.read_bytes()==p.env_candidate(env())
        assert (attempt/"logs-before-stop.json").exists()


def test_midnight_clean_restart_still_proves_actual_held_no_clock_abort():
    stopped=datetime(2026,10,8,3,59,55,tzinfo=timezone.utc)
    started=datetime(2026,10,8,4,0,5,tzinfo=timezone.utc)
    state=new_v2(); state["ExecMainStartTimestamp"]="Thu 2026-10-08 04:00:05 UTC"
    raw="2026-10-08 04:00:06,000 WARNING [V2-BOOT-HOLD] HELD restoration_complete=0\n"
    assert p.held_logs(raw,state,started+timedelta(seconds=2),stopped)["entry_allowed"] is False
    with pytest.raises(p.Stop): p.held_logs(raw,state,started+timedelta(seconds=2),stopped-timedelta(minutes=5))


def test_actual_empty_watched_population_not_fabricated_unreadable_or_tape():
    connection=Mock()
    value=continuity.capture(connection,[],NOW,NOW+timedelta(seconds=20),NOW+timedelta(seconds=90))
    connection.execute.assert_not_called()
    assert value["per_stock"]==[] and value["verdict"]=="MEASURED_RESTART_WINDOW"
    assert value["no_zero_holes_claim"] and value["independent_print_status"]=="UNMEASURED"


def test_complete_rechecks_late_startup_error_before_sealing(monkeypatch,tmp_path):
    import log_ranges
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.started=new_v2(); fx.stop_started=NOW; fx.log_base={}
    fx.now=lambda: NOW+timedelta(seconds=2)
    fx.gates=Mock(); fx.proc=Mock(); fx.flaggate=Mock()
    monkeypatch.setattr(log_ranges,"logs",lambda base: {p.V2:dict(text=held_text()+"2026-10-07 23:30:02,000 ERROR late failure\n")})
    with pytest.raises(p.Stop): fx.complete()
    assert not (tmp_path/"COMPLETE.json").exists()

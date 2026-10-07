"""[codex] Controlled future lifecycle/wait transitions using captured morning identities."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock
from uuid import uuid4

import pytest
import armed_readonly as armed
import paper_lifecycle as life
import release_policy as p
import runner
import flag_admission
from test_runner import NOW, HERE, FX


def morning():
    return json.loads((HERE/"fixtures/unattended-fleet-20261007T125302Z.json").read_bytes())["fleet_before"]


def after_close():
    before=morning(); current=deepcopy(before)
    for role,stamp in (("momentum-paper","Wed 2026-10-07 13:40:01 UTC"),
                       ("option-a-daily-guard","Wed 2026-10-07 13:40:07 UTC")):
        current[role].update(MainPID=0,ActiveState="inactive",SubState="dead",Result="success",NRestarts=0,
                             ExecMainCode=1,ExecMainStatus=0,InactiveEnterTimestamp=stamp)
    audit=dict(at_utc="2026-10-07T13:40:02+00:00",action="stop_paper",reason="scheduled_session_close",systemctl_rc=0)
    return before,current,audit


def test_recorded_active_morning_exact_core11_controlled_normal_close_actual_audit_key():
    before,current,audit=after_close()
    proof=life.admit(before,current,NOW,audit)
    assert proof["transitioned"]==list(life.ROLES) and proof["no_service_actions"]
    assert before["momentum-paper"]["MainPID"]==797678 and before["option-a-daily-guard"]["MainPID"]==797561
    for role in life.ROLES:
        assert current[role]["InvocationID"]==before[role]["InvocationID"]
        assert current[role]["ExecMainStartTimestamp"]==before[role]["ExecMainStartTimestamp"]
    p.states(current,current,0)


@pytest.mark.parametrize("damage",["invocation","start","mono","restart","exitcode","result","substate","pid","early","wrongday",
    "core11","audit_rc_key","audit_rc_bad","audit_reason_suffix","audit_before_stop","audit_after_guard","audit_missing"])
def test_scheduled_transition_every_identity_failure_and_nonliteral_audit_refuses(damage):
    before,current,audit=after_close(); paper=current[life.ROLES[0]]
    fields=dict(invocation=("InvocationID","0"*32),start=("ExecMainStartTimestamp","Wed 2026-10-07 07:40:05 UTC"),
        mono=("ExecMainStartTimestampMonotonic",999),restart=("NRestarts",1),exitcode=("ExecMainStatus",1),
        result=("Result","signal"),substate=("SubState","failed"),pid=("MainPID",999),
        early=("InactiveEnterTimestamp","Wed 2026-10-07 13:39:59 UTC"),wrongday=("InactiveEnterTimestamp","Tue 2026-10-06 13:40:01 UTC"))
    if damage in fields: key,value=fields[damage]; paper[key]=value
    elif damage=="core11": current["oms"]["MainPID"]+=1
    elif damage=="audit_rc_key": audit["rc"]=audit.pop("systemctl_rc")
    elif damage=="audit_rc_bad": audit["systemctl_rc"]=1
    elif damage=="audit_reason_suffix": audit["reason"]+="_before_owner_unreadable"
    elif damage=="audit_before_stop": audit["at_utc"]="2026-10-07T13:40:00+00:00"
    elif damage=="audit_after_guard": audit["at_utc"]="2026-10-07T13:40:08+00:00"
    else: audit=None
    with pytest.raises(p.Stop): life.admit(before,current,NOW,audit)


def test_no_arbitrary_upper_normal_close_limit_with_bound_actual_audit():
    before,current,audit=after_close()
    current[life.ROLES[0]]["InactiveEnterTimestamp"]="Wed 2026-10-07 13:46:01 UTC"
    current[life.ROLES[1]]["InactiveEnterTimestamp"]="Wed 2026-10-07 13:46:07 UTC"
    audit["at_utc"]="2026-10-07T13:46:02+00:00"
    assert life.admit(before,current,NOW,audit)["transitioned"]


@pytest.mark.parametrize("fraction", ["07.999999", "08.000000"])
def test_guard_audit_same_exit_second_precision_not_one_second_waiver(fraction):
    before,current,audit=after_close()
    audit["at_utc"]="2026-10-07T13:40:"+fraction+"+00:00"
    if fraction.startswith("07"):
        proof=life.admit(before,current,NOW,audit)
        assert proof["stop_timestamp_precision_seconds"]==1
        flag_admission.inactive_paper(current[life.ROLES[0]],current[life.ROLES[0]],NOW,proof)
    else:
        with pytest.raises(p.Stop): life.admit(before,current,NOW,audit)


def test_late_clean_close_flag_population_requires_same_positive_audit():
    before,current,audit=after_close()
    current[life.ROLES[0]]["InactiveEnterTimestamp"]="Wed 2026-10-07 13:46:01 UTC"
    current[life.ROLES[1]]["InactiveEnterTimestamp"]="Wed 2026-10-07 13:46:07 UTC"
    audit["at_utc"]="2026-10-07T13:46:07.500000+00:00"
    proof=life.admit(before,current,NOW,audit)
    flag_admission.inactive_paper(current[life.ROLES[0]],current[life.ROLES[0]],NOW,proof)
    proof["close_audit"]["systemctl_rc"]=1
    with pytest.raises(p.Stop): flag_admission.inactive_paper(current[life.ROLES[0]],current[life.ROLES[0]],NOW,proof)


@pytest.mark.parametrize("damage", [None,"duplicate","wrong_reason","partial","unsafe_mode","symlink"])
def test_bounded_actual_key_guard_audit_tail(tmp_path,monkeypatch,damage):
    from types import SimpleNamespace
    before,current,audit=after_close()
    path=tmp_path/"audit.jsonl"
    content=b'{"action":"old"}\n'*30000+json.dumps(audit).encode()+b"\n"
    if damage=="duplicate": content+=json.dumps(audit).encode()+b"\n"
    if damage=="wrong_reason": content=content.replace(b"scheduled_session_close",b"before_owner_unreadable")
    if damage=="partial": content=content.rstrip(b"\n")
    path.write_bytes(content)
    stat=life.os.fstat
    def root_stat(fd):
        info=stat(fd)
        return SimpleNamespace(st_uid=0,st_mode=0o100622 if damage=="unsafe_mode" else 0o100600,
            st_size=info.st_size,st_ino=info.st_ino,st_dev=info.st_dev)
    monkeypatch.setattr(life.os,"fstat",root_stat)
    if damage=="symlink":
        link=tmp_path/"link.jsonl"; link.symlink_to(path); path=link
    if damage:
        with pytest.raises(p.Stop): life.read_close(path,before,current,NOW)
    else:
        row,raw=life.read_close(path,before,current,NOW)
        assert row==audit and raw["offset"]>0 and raw["bytes"]<=262144
        assert raw["tail_sha256"]==p.digest(raw["raw_tail"].encode())


@pytest.mark.parametrize("kind",["flat","armed","tickets"])
def test_only_typed_fresh_wait_preclaim_then_claimed_same_refusal_stops(tmp_path,kind):
    fx=runner.Real(tmp_path,{},tmp_path)
    if kind=="flat":
        value=dict(rc=1,waiting_kind="FRESH_KNOWN_BOT_WORK",blockers=["working_orders"],remaining_general_failures=[])
        invoke=fx.flat
    elif kind=="armed":
        value=dict(rc=1,waiting_kind="FRESH_ARMED",armed_count=1,completeness="explicit-field-present")
        invoke=fx.armed
    else:
        value=dict(rc=1,waiting_kind="KNOWN_TICKET_PHASES",clears_unknown_ownership=False,rows=[dict(id="CONTROLLED",payload=dict(
            phase="price_wait",revision=1,slot="first",old=dict(broker_account_name="live:orb",symbol="RETO",
            strategy_code="schwab_1m_v2",side="buy",client_order_id="CONTROLLED",metadata={})))])
        invoke=fx.census
    fx.reader=Mock(return_value=json.dumps(value).encode())
    with pytest.raises(p.Pending): invoke()
    assert not (tmp_path/"write-started.json").exists()
    fx.claimed=True
    with pytest.raises(p.Stop) as error: invoke()
    assert not isinstance(error.value,p.Pending)


def test_generic_rc1_cannot_be_pending_and_rc1_never_read_retry(tmp_path,monkeypatch):
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.command=Mock(return_value=subprocess.CompletedProcess([],1,b'{"rc":1,"blockers":["foreign"]}',b""))
    sleep=Mock(); monkeypatch.setattr(runner.time,"sleep",sleep)
    with pytest.raises(p.Stop): fx.flat()
    assert fx.command.call_count==1 and not sleep.called


def test_repeated_unclaimed_PENDING_then_single_sequence_no_abort_or_duplicate():
    fx=FX(); original=fx.initial
    for _ in range(2):
        fx.initial=Mock(side_effect=p.Pending("CONTROLLED fresh held book"))
        with pytest.raises(p.Pending): runner.Sequence(fx).run()
        assert not fx.claimed and not fx.calls
    fx.initial=original
    runner.Sequence(fx).run()
    assert fx.calls.count("stop:"+p.V2)==fx.calls.count("restart:oms")==fx.calls.count("start:"+p.V2)==1 and "abort" not in fx.calls


def test_pre18_efficiency_pending_does_not_claim_unrun_native_or_broker_gates(tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.now=lambda: datetime(2026,10,7,20,1,tzinfo=timezone.utc)
    fx.baseline=Mock(); fx.flat=Mock(); fx.command=Mock()
    with pytest.raises(p.Pending): fx.initial()
    assert not fx.baseline.called and not fx.flat.called and not fx.command.called and not fx.claimed
    raw=json.loads(next(tmp_path.glob("*-clock-only-pending.json")).read_bytes())
    assert raw["native_gate_executed"] is False and raw["other_gates"]=="UNMEASURED"


@pytest.mark.parametrize("bad",[False,True])
def test_own_timer_self_disables_only_own_timer_no_self_service_or_daily(bad,tmp_path):
    fx=runner.Real(tmp_path,{},tmp_path)
    fx.command=Mock(side_effect=[subprocess.CompletedProcess([],0,b"",b"Removed controlled symlink\n"),
        subprocess.CompletedProcess([],0,b"ActiveState="+(b"active" if bad else b"inactive")+b"\nUnitFileState=disabled\n",b"")])
    if bad:
        with pytest.raises(p.Stop): fx.retire_timer()
    else: fx.retire_timer()
    args=fx.command.call_args_list[0].args[0]
    assert args==["systemctl","disable","--now","project-mai-tai-linesrc1-20261007.timer"]
    assert fx.command.call_args_list[0].kwargs["check"] is False


def test_fresh_positive_armed_and_negative_stale_missing_foreign_never_empty():
    event=dict(event_type="isolated_bot_state",source_service=p.V2,event_id=str(uuid4()),produced_at=NOW.isoformat(),
        payload=dict(account_name="live:schwab_1m_v2",strategy_code="schwab_1m_v2",cw_armed_segments=[dict(symbol="RETO")]))
    reply=[str(int(NOW.timestamp()*1000))+"-0",json.dumps(event)]
    value=armed.proof(reply,NOW)
    assert value["rc"]==1 and value["waiting_kind"]=="FRESH_ARMED" and value["armed_count"]==1
    with pytest.raises(p.Stop): armed.proof(reply,NOW+timedelta(seconds=61))
    event["payload"].pop("cw_armed_segments")
    with pytest.raises(p.Stop): armed.proof([reply[0],json.dumps(event)],NOW)
    event["payload"]["cw_armed_segments"]=[]; event["payload"]["account_name"]="foreign"
    with pytest.raises(p.Stop): armed.proof([reply[0],json.dumps(event)],NOW)


@pytest.mark.parametrize("kind", ["invalid_manifest","pending","unsealed_error","sealed_complete"])
def test_bootstrap_real_shell_seal_timer_cleanup_and_read_only_PENDING(tmp_path,kind):
    # Run the literal shell with only external executable paths substituted.
    log=tmp_path/"calls"
    fake=tmp_path/"fake"
    fake.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$CALLS"\n')
    fake.chmod(0o755)
    python=tmp_path/"python"
    python.write_text('#!/usr/bin/env bash\n'+('touch "$JOB/write-started.json"\n' if kind=="sealed_complete" else '')+
        'exit '+str(75 if kind=="pending" else 2 if kind=="unsealed_error" else 0)+'\n')
    python.chmod(0o755)
    script=(HERE/"run.sh").read_text().replace('/home/trader/project-mai-tai/.venv/bin/python',str(python))
    script=script.replace('systemctl ',str(fake)+' systemctl ').replace('timeout 35 ',str(fake)+' timeout 35 ')
    path=tmp_path/"run.sh"; path.write_text(script)
    (tmp_path/"release.sha256").write_text(('invalid' if kind=="invalid_manifest" else 'a'*64)+'\n')
    result=subprocess.run(['bash',str(path)],capture_output=True,env={**os.environ,'CALLS':str(log),'JOB':str(tmp_path)})
    calls=log.read_text() if log.exists() else ''
    if kind=="pending":
        assert result.returncode==75 and not (tmp_path/"write-started.json").exists() and not calls
    elif kind=="sealed_complete":
        assert result.returncode==0 and not calls
    else:
        assert result.returncode!=0
        seal=json.loads((tmp_path/"write-started.json").read_bytes())
        assert seal["verdict"]=="ABORTED_BOOTSTRAP" and seal["app_writes"] is False
        assert 'systemctl disable --now project-mai-tai-linesrc1-20261007.timer' in calls
        assert 'preopen.timer' not in calls and 'start schwab' not in calls

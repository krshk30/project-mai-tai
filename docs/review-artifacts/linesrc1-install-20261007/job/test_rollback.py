"""[codex] Recorded authorized rollback pins; controlled future install boundaries."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import Mock

import pytest
import paper_lifecycle as life
import release_policy as p
import runner
from test_runner import HERE, FIX, ROOT, NOW, env, new_v2


def test_overlay_only_expected_false_no_source_owner_population_or_other_setting_change():
    original=(ROOT/"ops/health/expected_flags.json").read_bytes()
    candidate=p.rollback_catalog(original)
    assert candidate==(HERE/"rollback-expected_flags.json").read_bytes()
    source=json.loads(original); applied=json.loads(candidate)
    row=next(row for row in source["flags"] if row["name"]=="oms_v2_webull_mirror_retained_hold_enabled")
    assert row["owning_service"]=="oms" and row["expected"] is True
    row["expected"]=False
    assert source==applied
    assert p.catalog_counts(candidate,(FIX/"restart_evidence/expected_numeric.json").read_bytes())["total"]==157
    old=(FIX/"preopen-daily/runtime.json").read_bytes()
    new=p.rollback_runtime(old,candidate)
    assert new==(HERE/"rollback-runtime.json").read_bytes()
    expected=json.loads(old); expected["evidence_inputs"]["/home/trader/restart_evidence/expected_flags.json"]=p.digest(candidate)
    assert json.loads(new)==expected


@pytest.mark.parametrize("damage",["value","owner","also_owner","other_value","reason","numeric"])
def test_any_nonreviewed_source_catalog_mutation_refuses_overlay(damage):
    data=json.loads((ROOT/"ops/health/expected_flags.json").read_bytes())
    row=next(row for row in data["flags"] if row["name"]=="oms_v2_webull_mirror_retained_hold_enabled")
    if damage=="value": row["expected"]=False
    elif damage=="owner": row["owning_service"]="strategy"
    elif damage=="also_owner": row["also_check_services"]=["schwab-1m-v2"]
    elif damage=="reason": row["reason"]="unbound"
    elif damage=="numeric": data["schema_version"]=999
    else: data["flags"][0]["expected"]=not data["flags"][0]["expected"]
    with pytest.raises(p.Stop): p.rollback_catalog(p.canonical(data))


@pytest.mark.parametrize("damage",["oldOMS","OMSrestart","OMSstart","OMSnonce","otherowner","oldenv","authority"])
def test_release_cannot_adopt_any_other_identity_environment_or_authority(damage):
    baseline=p.rollback_baseline()
    assert baseline["fleet_before"]["oms"]["MainPID"]==1043365
    assert baseline["fleet_before"]["oms"]["ExecMainStartTimestamp"]=="Wed 2026-10-07 14:02:34 UTC"
    if damage=="oldOMS": baseline["fleet_before"]["oms"]["MainPID"]=626190
    elif damage=="OMSrestart": baseline["fleet_before"]["oms"]["NRestarts"]=1
    elif damage=="OMSstart": baseline["fleet_before"]["oms"]["ExecMainStartTimestamp"]="Wed 2026-10-07 14:02:35 UTC"
    elif damage=="OMSnonce": baseline["fleet_before"]["oms"]["InvocationID"]="0"*32
    elif damage=="otherowner": baseline["fleet_before"]["strategy"]["MainPID"]+=1
    elif damage=="oldenv": baseline["environment_sha256"]="6677c4bd25fadc5c229b2a0c682f5d1ae8a4bc30052b9121bc044ea2932f35ce"
    else: baseline["authorization_provenance"]="generic future adoption"
    with pytest.raises(p.Stop): p.require_rollback_baseline(baseline)


@pytest.mark.parametrize("damage",[None,"true","missing","duplicate","alias"])
def test_env_only_restoration_change_retained_literal_false_is_not_reactivated(damage):
    raw=env()
    if damage=="true": raw=raw.replace((p.RETAINED_FLAG+"=false").encode(),(p.RETAINED_FLAG+"=true").encode())
    elif damage=="missing": raw=raw.replace((p.RETAINED_FLAG+"=false\n").encode(),b"")
    elif damage=="duplicate": raw+=(p.RETAINED_FLAG+"=false\n").encode()
    elif damage=="alias": raw+=(p.RETAINED_FLAG.lower()+"=false\n").encode()
    if damage:
        with pytest.raises(p.Stop): p.env_candidate(raw)
    else:
        candidate=p.env_candidate(raw)
        assert candidate==raw.replace((p.FLAG+"=false").encode(),(p.FLAG+"=true").encode())
        p.retained_off(candidate.replace(b"\n",b"\0"))
        assert p.process_values(candidate.replace(b"\n",b"\0"),"true")[p.RETAINED_FLAG]=="false"


def test_new_morning_OMS_pin_is_prior_rollback_not_a_second_LINESRC_restart():
    baseline=p.rollback_baseline()
    raw=p.gate_candidate((FIX/"preopen.sh").read_bytes(),new_v2(),"CONTROLLED_SNAPSHOT","CONTROLLED_RECORD",
        untouched_oms=baseline["fleet_before"]["oms"])
    text=raw.decode()
    assert "EXPECTED_OMS_PID=1043365" in text
    assert "EXPECTED_OMS_START='Wed 2026-10-07 14:02:34 UTC'" in text
    assert "--restarted oms" not in text and text.count("--restarted ")==1
    assert "--expect-flag 'oms:" not in text
    assert "schwab-1m-v2:"+p.RETAINED_FLAG+"=false" in text
    for role in ("STRATEGY","ORB","ORB_SCHWAB","CONTROL"):
        for suffix in ("PID","START"):
            import re
            key="EXPECTED_"+role+"_"+suffix
            assert re.search("(?m)^"+key+"=.*$",text)[0]==re.search("(?m)^"+key+"=.*$",(FIX/"preopen.sh").read_text())[0]
    baseline["fleet_before"]["oms"]["MainPID"]+=1
    with pytest.raises(p.Stop): p.gate_candidate((FIX/"preopen.sh").read_bytes(),new_v2(),"S","R",untouched_oms=baseline["fleet_before"]["oms"])


def test_already_recorded_inactive_paper_keeps_positive_close_audit_for_final_flaggate():
    baseline=p.rollback_baseline()["fleet_before"]
    audit=dict(action="stop_paper",reason="scheduled_session_close",systemctl_rc=0,
        at_utc="2026-10-07T13:40:03.500000+00:00")  # Controlled timing inside captured stop interval.
    proof=life.admit(baseline,deepcopy(baseline),NOW,audit)
    assert proof["transitioned"]==[] and proof["close_audit"]==audit
    from flag_admission import inactive_paper
    inactive_paper(baseline["momentum-paper"],baseline["momentum-paper"],NOW,proof)
    audit["systemctl_rc"]=1
    with pytest.raises(p.Stop): life.admit(baseline,deepcopy(baseline),NOW,audit)


@pytest.mark.parametrize("old",[False,True])
def test_catalog_reader_requires_staged_false_overlay_not_source_true(tmp_path,monkeypatch,old):
    helper=tmp_path/"helpers"; helper.mkdir()
    correct=(HERE/"rollback-expected_flags.json").read_bytes()
    (helper/"expected_flags.json").write_bytes((ROOT/"ops/health/expected_flags.json").read_bytes() if old else correct)
    numeric=(FIX/"restart_evidence/expected_numeric.json").read_bytes(); (helper/"expected_numeric.json").write_bytes(numeric)
    monkeypatch.setattr(runner,"HELPERS",helper)
    release=dict(catalog_hashes=dict(flags=p.digest(correct),numeric=p.digest(numeric)),
        catalog_counts=dict(boolean=147,numeric=10,total=157))
    fx=runner.Real(tmp_path,release,tmp_path)
    if old:
        with pytest.raises(p.Stop): fx.catalog()
    else: fx.catalog()


def test_actual_official_collector_nonrestarted_flag_refuses_and_new_gate_flags_pass(monkeypatch,tmp_path,capsys):
    from test_runner import load
    import re
    module=load("controlled_existing_collector_tests",ROOT/"tests/unit/test_v2_restart_evidence.py")
    args,current,logs=module._report_fixture(monkeypatch,tmp_path)
    vre=module.vre
    gate=p.gate_candidate((FIX/"preopen.sh").read_bytes(),new_v2(),"S","R",p.rollback_baseline()["fleet_before"]["oms"]).decode()
    flags=re.findall(r"--expect-flag '([^']+)'",gate)
    args.expect_flag=flags
    values={vre._parse_expected_flag(value)[1]:vre._parse_expected_flag(value)[2] for value in flags}
    monkeypatch.setattr(vre,"_process_environment",lambda pid,runner:values)
    assert vre.report(args,runner=lambda command:"")==0
    assert all(vre._parse_expected_flag(value)[0]==vre.V2_SERVICE for value in flags)
    args.expect_flag.append("oms:"+p.RETAINED_FLAG+"=false")
    with pytest.raises(vre.EvidenceUnknown,match="flag check names non-restarted service 'oms'"):
        vre.report(args,runner=lambda command:"")


def test_recut_unit_only_new_immutable_sibling_conditions_same_oneshot_and_timer():
    service=(HERE/"project-mai-tai-linesrc1-20261007.service").read_text()
    root="/home/trader/after-hours/2026-10-07/linesrc1-1a70da19/job-rollback-1002"
    assert "WorkingDirectory="+root in service and "ExecStart="+root+"/run.sh" in service
    assert "ConditionPathExists="+root+"/approval.json" in service
    assert "ConditionPathExists=!"+root+"/write-started.json" in service
    assert "Type=oneshot" in service and "Restart=no" in service
    assert "linesrc1-1a70da19/job/" not in service
    assert p.digest((HERE/"project-mai-tai-linesrc1-20261007.timer").read_bytes())=="0ce5a48081cd9e462483b3fb5c1bc3450e7c4d8af5f9dd73f9133f884e988fe6"

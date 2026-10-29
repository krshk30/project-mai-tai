"""Operational SHADOW policy generator: delivery accepts it, holidays get no
open window, output is byte-identical on rerun, operands are never invented."""

from datetime import date
import hashlib
import json

import pytest

from ops.smartalert1 import delivery as d
from ops.smartalert1 import operational_policy as op
from ops.smartalert1.producer import producer_review

HEAD = "e" * 40


def test_policy_passes_delivery_binding_and_ledger_init():
    policy, _ = op.build(source_head=HEAD, first=date(2026, 10, 29), last=date(2026, 11, 11))
    assert d.policy_binding(policy)
    assert policy["source_bindings"]["L2"]["file_sha256"] == d.L2_TRUE_RULE_SHA256
    assert policy["category_map"]["exact"]["L3"]["B_guard_STOP"] == {"live_guard_stop": "PAGE"}
    state = d.initial_state(campaign_id="c", ledger_epoch="e", policy=policy)
    assert state["revision"] == 0 and state["incidents"] == {}


def test_holiday_and_weekend_get_no_open_window():
    policy, _ = op.build(source_head=HEAD, first=date(2026, 11, 25), last=date(2026, 11, 29))
    windows = {w["trading_date"]: w for w in policy["close_calendar"]["windows"]}
    assert windows["2026-11-26"]["open_session"] is False  # Thanksgiving
    assert "start" not in windows["2026-11-26"]
    assert windows["2026-11-28"]["open_session"] is False  # Saturday
    open_day = windows["2026-11-25"]
    assert open_day["open_session"] is True and open_day["close_kind"] == "EXTENDED"
    assert open_day["start"] == "2026-11-25T04:00:00-05:00" and open_day["end"] == "2026-11-25T20:00:00-05:00"
    evidence = {policy["close_calendar"]["calendar_ref"]: "a" * 64, op.CATEGORY_MAP_REF: "b" * 64}
    window = dict(open_day, calendar_ref=policy["close_calendar"]["calendar_ref"])
    assert d.reviewed_close_window(window, {"evidence": evidence}, policy) == open_day


def test_rerun_is_byte_identical(tmp_path):
    outputs = []
    for name in ("a", "b"):
        target = tmp_path / name
        assert op.main(["--source-head", HEAD, "--trading-date", "2026-10-29", "--out", str(target),
                        "--init-out", str(target) + ".init"]) == 0
        outputs.append((target.read_bytes(), (tmp_path / (name + ".init")).read_bytes()))
    assert outputs[0] == outputs[1]


def test_unbound_operands_keep_the_producer_refusing():
    policy, _ = op.build(source_head=HEAD, first=date(2026, 10, 29), last=date(2026, 10, 29))
    assert policy["producer_review"]["status"].startswith("OPERANDS_UNBOUND:")
    index = {"schema_version": "smartalert1.producer.v1", "mode": "SHADOW", "as_of": "2026-10-29T20:00:00Z",
             "source_bindings": policy["source_bindings"],
             "manifest": {"complete": True, "overflow": False, "read_error": False,
                          "as_of": "2026-10-29T20:00:00Z", "evidence": {}}}
    with pytest.raises(d.Refused, match="producer_operands_needs_ruling"):
        producer_review(index, policy, as_of="2026-10-29T20:00:00Z")


def test_upstream_policyversion_mismatch_refused():
    upstream = {"L2": {"policyversion": "other"}, "L3": {"policyversion": op.UPSTREAM_POLICYVERSIONS["L3"]}}
    with pytest.raises(ValueError, match="policyversion mismatch"):
        op.build(source_head=HEAD, first=date(2026, 10, 29), last=date(2026, 10, 29), upstream=upstream,
                 acquisition_ref="a", schedule_ref="s", source_closure_ref="c")

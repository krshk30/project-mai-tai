"""Offline evidence controls; these fixtures are not trading replay populations."""
from datetime import datetime, timedelta, timezone
import importlib.util
import gzip
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location("wbquiet_history", Path(__file__).parents[2] / "scripts/wbquiet_history_replay.py")
history = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(history)
UTC = timezone.utc
AT = datetime(2026, 10, 7, 19, 12, tzinfo=UTC)


def event(line, at=AT, ordinal=1):
    return {"at": at.isoformat(), "path": "/fixture/oms.log", "line_number": ordinal, "line": line}


def facts(**changes):
    return {"configured": True, "known": True, "fresh": True, "held": False,
            "broker_held": False, "working": False, "fresh_fill": False, **changes}


def decision(**changes):
    return {"at": AT.isoformat(), "account": "live:orb", "symbol": "BIYA", "side": "sell",
            "client_order_id": "child-T", **changes}


def list_row(**changes):
    return {"acquired_at": (AT - timedelta(seconds=1)).isoformat(), "account": "live:orb", "symbol": "BIYA",
            "side": "sell", "client_order_id": "child-T", "status": "cancelled", "filled_quantity": 0,
            "source": "/trade/orders/list-today", "evidence_path": "/fixture/list-body.json",
            "evidence_sha256": "a" * 64, **changes}


def shadow(pass_id=1, at=AT, pid=1, **account_changes):
    account = {"account": "live:orb", "facts_at_pass_start": facts(), "actual_adapter_read": "ok",
               "covered_readers": [], "would": "read", **account_changes}
    return event("[WBQUIET-SHADOW] " + json.dumps({"process_pid": pid, "pass_id": pass_id,
        "as_of": at.isoformat(), "policy_applied": False, "dropped_observations": 0,
        "accounts": [account]}), at, pass_id)


def data(events=None, **changes):
    return {"as_of_utc": (AT + timedelta(minutes=2)).isoformat(),
        "window_start_utc": AT.replace(day=2, hour=4, minute=0).isoformat(), "source_files": [],
        "log_events": events or [], "cancel_417": [], "day_list_observations": [], **changes}


@pytest.mark.parametrize("key,state", [("held", "held"), ("broker_held", "held"), ("working", "working"), ("fresh_fill", "fresh-fill")])
def test_hot_state_never_reduces_fifteen_second_cadence(key, state):
    assert history.state_cadence(facts(**{key: True}), AT.isoformat()) == (state, 15)


def test_held_priority_over_working_and_fill():
    assert history.state_cadence(facts(held=True, working=True, fresh_fill=True), AT.isoformat()) == ("held", 15)


@pytest.mark.parametrize("changes", [{"known": False}, {"fresh": False}, {"known": None}, {"held": None}, {"working": "false"}])
def test_unknown_source_or_incomplete_books_cannot_be_quiet_flat(changes):
    assert history.state_cadence(facts(**changes), AT.isoformat()) == ("unknown", 15)


@pytest.mark.parametrize("hour,expected", [(0, ("overnight", 300)), (3, ("overnight", 300)),
    (4, ("flat", 60)), (19, ("flat", 60)), (20, ("overnight", 300))])
def test_rule_A_et_boundaries(hour, expected):
    local = AT.astimezone(history.ET).replace(hour=hour)
    assert history.state_cadence(facts(), local.isoformat()) == expected


def test_no_credentials_not_new_empty_snapshot():
    assert history.state_cadence(facts(configured=False), AT.isoformat()) == ("no_credentials", None)


def test_partial_endpoint_snapshots_not_summed_as_requests():
    rows = [event("[WEBULL-ENDPOINT-CALLS] minute=2026-10-07T19:12:00+00:00 endpoint=/trade/order/cancel success=1 failure=1 total=2 partial=1"),
            event("[WEBULL-ENDPOINT-CALLS] minute=2026-10-07T19:12:00+00:00 endpoint=/trade/order/cancel success=1 failure=2 total=3 partial=1"),
            event("[WEBULL-ENDPOINT-CALLS] minute=2026-10-07T19:12:00+00:00 endpoint=/trade/order/cancel success=2 failure=2 total=4 partial=0")]
    buckets, naive, malformed = history.counter_buckets(rows)
    bucket = next(iter(buckets.values()))
    assert bucket["row"]["total"] == 4
    assert naive[("2026-10-07", "/trade/order/cancel")]["total"] == 9
    assert not malformed


def test_conflicting_final_counters_epoch_is_reported_not_hidden():
    rows = [event(f"minute={AT.isoformat()} endpoint=/account/positions success={n} failure=0 total={n} partial=0") for n in (1, 2)]
    buckets, _, _ = history.counter_buckets(rows)
    assert next(iter(buckets.values()))["ambiguous_final_epochs"] is True


def test_counter_day_is_counter_minute_not_flush_time():
    counter = AT.replace(hour=3, minute=59)
    flush = counter + timedelta(minutes=1)
    report = history.analyze(data([event(f"[WEBULL-ENDPOINT-CALLS] minute={counter.isoformat()} endpoint=/account/positions success=4 failure=0 total=4 partial=0", flush)]))
    assert report["days"]["2026-10-06"]["endpoints"]["/account/positions"]["total"] == 4


def test_counter_success_failure_total_invariant():
    buckets, _, bad = history.counter_buckets([event(f"minute={AT.isoformat()} endpoint=/account/positions success=4 failure=1 total=4 partial=0")])
    assert not buckets
    assert len(bad) == 1


def test_cancel_counter_snapshots_not_error_event_population():
    rows = [event(f"minute={AT.isoformat()} endpoint=/trade/order/cancel success=0 failure={n} total={n} partial={partial}", ordinal=i)
            for i, (n, partial) in enumerate(((1, 1), (2, 1), (2, 0)), 1)]
    failures = [{"at": AT.isoformat(), "path": "/fixture/oms.log", "line_number": i,
                 "request_id": str(i), "client_order_id": None} for i in (4, 5)]
    b = history.analyze(data(rows, cancel_417=failures))["days"]["2026-10-07"]["rule_B"]
    assert b["corrected_endpoint_cancel_failures"] == 2
    assert b["naive_cumulative_cancel_failures"] == 5
    assert b["raw_HTTP417_exception_lines"] == b["distinct_cancel_417"] == 2
    assert b["terminal_membership_unmeasured"] == 2


def test_missing_rotated_interval_is_enumerated_not_complete_day_claim():
    sources = [{"path": "/var/log/project-mai-tai/oms.log-20261003.gz"},
               {"path": "/var/log/project-mai-tai/oms.log"}]
    report = history.analyze(data(source_files=sources))
    missing = report["source_coverage"]["missing_rotated_file_intervals"]
    assert missing[0]["start_utc"] == "2026-10-03T00:00:00+00:00"
    assert missing[0]["end_exclusive_utc"] == "2026-10-04T00:00:00+00:00"
    assert all(row["complete_ET_day_claim"] is False for row in report["days"].values())


def test_missing_counter_minutes_grouped_without_inventing_shutdown_cause():
    stamps = [AT.isoformat(), (AT + timedelta(minutes=1)).isoformat(), (AT + timedelta(minutes=3)).isoformat()]
    ranges = history.missing_ranges(stamps)
    assert [row["minutes"] for row in ranges] == [2, 1]
    assert ranges[0]["end_exclusive_utc"] == (AT + timedelta(minutes=2)).isoformat()


def test_fresh_terminal_exact_row_is_positive_evidence():
    result = history.terminal_list_match(decision(), [list_row()])
    assert result["verdict"] == "PROVEN_TERMINAL_ZERO_REMAINDER"
    assert result["age_seconds"] == 1


@pytest.mark.parametrize("changes", [{"status": "accepted"}, {"account": "foreign"}, {"client_order_id": "foreign"},
    {"symbol": "OTHER"}, {"side": "buy"}, {"source": "/trade/order/detail"}, {"evidence_sha256": ""},
    {"evidence_path": ""}, {"filled_quantity": "NaN"}, {"filled_quantity": -1},
    {"acquired_at": (AT - timedelta(seconds=2)).isoformat()}, {"acquired_at": (AT + timedelta(seconds=1)).isoformat()}])
def test_terminal_match_needs_each_identity_freshness_and_source_guard(changes):
    assert history.terminal_list_match(decision(), [list_row(**changes)])["verdict"] == "UNMEASURED"


def test_filled_or_partial_terminal_is_not_zero_quantity_absent():
    result = history.terminal_list_match(decision(), [list_row(status="filled", filled_quantity=3)])
    assert result["verdict"] == "TERMINAL_EXECUTION_REQUIRES_ATTRIBUTION"
    assert result["filled_quantity"] == 3


def test_417_or_final_order_without_day_body_never_positive_terminal_proof():
    assert history.terminal_list_match(decision(), [list_row(source="broker_orders.final_status")])["verdict"] == "UNMEASURED"
    assert history.terminal_list_match(decision(), [])["verdict"] == "UNMEASURED"


def test_conflicting_day_rows_fail_closed():
    assert history.terminal_list_match(decision(), [list_row(), list_row(status="filled", filled_quantity=3)])["verdict"] == "UNMEASURED"


def test_decision_identity_absent_does_not_adopt_latest_buy():
    assert history.terminal_list_match(decision(account=None), [list_row()])["verdict"] == "UNMEASURED"


def test_reader_window_sixty_included_sixtyone_excluded():
    retained, unknown = history.reader_window([{"reader": "tri-state", "elapsed_seconds": 60},
        {"reader": "later", "elapsed_seconds": 61}, {"reader": "bad", "elapsed_seconds": -1}])
    assert retained == [{"reader": "tri-state", "elapsed_seconds": 60}]
    assert unknown == 1


def test_shadow_hypothetical_clocks_process_and_account_bound():
    events = [shadow(1), shadow(2, AT + timedelta(seconds=15)), shadow(3, AT + timedelta(seconds=60)),
              shadow(1, AT + timedelta(seconds=61), pid=2)]
    rows = history.shadow_replay(events, AT + timedelta(minutes=3))["rows"]
    assert [row["would"] for row in rows] == ["read", "skip", "read", "read"]


def test_held_and_unknown_receipts_never_sixty_second_skip():
    events = [shadow(1, facts_at_pass_start=facts(held=True)),
              shadow(2, AT + timedelta(seconds=15), facts_at_pass_start=facts(held=True)),
              shadow(3, AT + timedelta(seconds=16), facts_at_pass_start=facts(fresh=False))]
    assert [row["would"] for row in history.shadow_replay(events, AT + timedelta(minutes=3))["rows"]] == ["read"] * 3


def test_unreadable_or_unreached_does_not_advance_quiet_clock():
    events = [shadow(1, actual_adapter_read="not_reached"), shadow(2, AT + timedelta(seconds=1)),
              shadow(3, AT + timedelta(seconds=2), actual_adapter_read="unreadable")]
    rows = history.shadow_replay(events, AT + timedelta(minutes=3))["rows"]
    assert [row["would"] for row in rows] == ["not_evaluated", "read", "read"]
    assert rows[-1]["state"] == "unknown"


def test_shadow_duplicates_and_censoring_not_zero_activity():
    report = history.shadow_replay([shadow(), shadow()], AT + timedelta(seconds=30))
    assert len(report["rows"]) == 1
    assert len(report["duplicate_receipts"]) == 1
    assert report["rows"][0]["right_censored_60s_window"] is True


def test_reader_invocation_on_nominal_skip_not_decision_change_claim():
    events = [shadow(1), shadow(2, AT + timedelta(seconds=15),
        covered_readers=[{"reader": "sync_account_positions", "elapsed_seconds": 4}])]
    report = history.analyze(data(events))
    a = report["days"]["2026-10-07"]["rule_A"]
    assert a["tagged_reader_invocations_on_nominal_skip"] == 1
    assert a["decision_changing_minutes"].startswith("UNMEASURED")


def test_missing_historical_body_stays_unmeasured_not_zero():
    failure = {"at": AT.isoformat(), "path": "/fixture/oms.log", "line_number": 3,
               "request_id": "r1", "client_order_id": "schwab_1m_v2-BIYA-protect-idT"}
    report = history.analyze(data(cancel_417=[failure]))
    b = report["days"]["2026-10-07"]["rule_B"]
    assert b["terminal_membership_unmeasured"] == 1
    assert b["proven_terminal_zero_remainder"] == 0  # Evidence count, not absence assertion.
    assert report["rule_C"]["detail_body_None"].startswith("UNMEASURED")


def test_retained_fill_cannot_be_used_as_flat_savings_proxy_minute():
    line = f"minute={AT.isoformat()} endpoint=/account/positions success=4 failure=0 total=4 partial=0"
    report = history.analyze(data([event(line)], fills=[{"account": "live:orb", "filled_at": (AT-timedelta(seconds=599)).isoformat()}]))
    proxy = report["days"]["2026-10-07"]["rule_A"]["flat_capacity_proxy"]
    assert proxy["known_recent_fill_minutes"] == 1
    assert proxy["fresh_fill_retained_suppressed_opportunity_proxy"] == 0
    assert proxy["nominal_suppressed_opportunities"] == 3  # Always-flat model is separately labelled.


def test_no_shadow_state_does_not_certify_hypothetical_census_zero():
    event_ = event("[BROKER-SYNC-CENSUS] live:orb: ok=20 failed=0 consecutive_now=0")
    report = history.analyze(data([event_]))
    receipt = report["days"]["2026-10-07"]["census_replay_receipts"][0]
    assert receipt["actual_ok"] == 20
    assert receipt["hypothetical_ok_zero"].startswith("UNMEASURED")


def test_repeatable_cli_hashes_raw_and_never_overwrites(tmp_path):
    source, target, md = (tmp_path / name for name in ("raw.json", "receipt.json", "receipt.md"))
    source.write_text(json.dumps(data()))
    history.main(["--input", str(source), "--output", str(target), "--markdown", str(md)])
    report = json.loads(target.read_text())
    assert len(report["raw_pull_sha256"]) == 64
    assert "UNMEASURED" in md.read_text()
    with pytest.raises(FileExistsError):
        history.main(["--input", str(source), "--output", str(target)])


def test_gzip_raw_receipt_replays_identical_hash(tmp_path):
    raw = json.dumps(data()).encode()
    source, target = tmp_path / "raw.json.gz", tmp_path / "result.json"
    with gzip.open(source, "wb") as stream:
        stream.write(raw)
    history.main(["--input", str(source), "--output", str(target)])
    assert json.loads(target.read_text())["raw_pull_sha256"] == history.hashlib.sha256(raw).hexdigest()

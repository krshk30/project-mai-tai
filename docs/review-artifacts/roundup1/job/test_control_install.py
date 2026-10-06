"""Controlled schema shaped by the merged JAGX fixture; no production calls."""
import copy
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import attended
import control_display_proof as control
import post_proof
import release_policy as policy
from retry_zero_readonly import FIELDS
from test_attended_release import NOW, fleet

PAGE = ('ORB Schwab Live live:schwab_1m_v2 LIVE/SCHWAB <strong>Mode:</strong> LIVE '
        '<strong>Provider:</strong> SCHWAB JAGX 6.6700 6.6001 $-0.14 SESSION COMPLETE '
        '<span>Trades</span>\n<strong>1</strong>')


def api():
    return dict(closed_today=[dict(ticker="JAGX", quantity=2, entry_price=6.67, exit_price=6.6001,
                                  entry_time="2026-10-06T13:30:14+00:00", exit_time="2026-10-06T13:31:07+00:00")],
                daily_pnl=-0.1398, positions=[], pending_open_symbols=[], pending_close_symbols=[],
                recent_fills=[{"controlled": "BUY"}, {"controlled": "SELL"}], recent_orders=[{}, {}])


def control_receipt(phase, pid, old_pid=None, state=None, oms_pid=1103):
    state = fleet()["control"] if state is None else state
    before = {key: str(state[key]) for key in FIELDS}
    before["MainPID"] = str(pid)
    token = dict(enabled=True, health="healthy", dead_token_retries=0,
                 expires_at=(NOW + timedelta(hours=1)).isoformat(), forced_refresh=False, alternate_writer_enabled=False)
    receipt = dict(verdict="PASS", phase=phase, pid=pid, old_pid=old_pid, token=token, before=before,
                   adapter=dict(pid=oms_pid, refresh_enabled=False, provenance="pinned-settings-default-false"),
                   after=copy.deepcopy(before), owners=[pid], old_pid_retired=phase == "after",
                   http_hashes={}, measured_at_utc=NOW.isoformat())
    if phase == "after":
        raw = json.dumps(api())
        receipt.update(page=control.page_proof(PAGE, api()), raw_page=PAGE, raw_orb_api=api(), raw_orb_api_text=raw)
        receipt["http_hashes"] = {"/bot/orb": policy.digest(PAGE.encode()), "/api/bot/orb-schwab": policy.digest(raw.encode())}
    return receipt


def control_log(pid=1100):
    return (f"INFO:     Started server process [{pid}]\nINFO:     Waiting for application startup.\n"
            "2026-10-06 20:10:00,000 [SCHWAB-TOKEN-REFRESHER] starting (check_interval=60s)\n"
            "INFO:     Application startup complete.\n"
            "INFO:     Uvicorn running on http://127.0.0.1:8100 (Press CTRL+C to quit)\n")


def test_control_page_exact_live_schwab_one_jagx_and_raw_receipt():
    assert control.page_proof(PAGE, api())["trades"] == 1
    receipt = control_receipt("after", 1100, 100)
    assert control.validate(receipt, "after", 1100, 100, NOW, NOW)["page"]["symbol"] == "JAGX"


@pytest.mark.parametrize("old,new", [("LIVE/SCHWAB", "PAPER"), ("Mode:</strong> LIVE", "Mode:</strong> PAPER"),
    ("Provider:</strong> SCHWAB", "Provider:</strong> WEBULL"), ("live:schwab_1m_v2", "paper:orb"),
    ("<strong>1</strong>", "<strong>0</strong>"), ("<strong>1</strong>", "<strong>2</strong>"),
    ("JAGX", "OTHER"), ("6.6700", "6.66"), ("6.6001", "6.12"), ("$-0.14", "$-1.08")])
def test_control_page_wrong_identity_paper_qty_or_count_blocks(old, new):
    with pytest.raises(policy.Stop):
        control.page_proof(PAGE.replace(old, new), api())


@pytest.mark.parametrize("change", [lambda d: d.update(closed_today=[]),
    lambda d: d["closed_today"].append(d["closed_today"][0]),
    lambda d: d["closed_today"][0].update(ticker="ATR_ONLY"),
    lambda d: d["closed_today"][0].update(quantity=132),
    lambda d: d["closed_today"][0].update(entry_price=6.66),
    lambda d: d["closed_today"][0].update(exit_time="2026-10-05T13:31:07Z"),
    lambda d: d.update(daily_pnl=-1.08), lambda d: d.update(positions=[{}]),
    lambda d: d.update(pending_open_symbols=["JAGX"]), lambda d: d.update(recent_fills=[]),
    lambda d: d.update(recent_fills={"BUY": {}, "SELL": {}}), lambda d: d.update(recent_orders="xx")])
def test_control_api_other_strategy_stale_missing_or_extra_trade_blocks(change):
    data = api()
    change(data)
    with pytest.raises(policy.Stop):
        control.page_proof(PAGE, data)


@pytest.mark.parametrize("change", [lambda r: r.update(owners=[1100, 100]),
    lambda r: r.update(old_pid_retired=False), lambda r: r["after"].update(MainPID="1111"),
    lambda r: r.update(measured_at_utc=(NOW - timedelta(seconds=1)).isoformat()),
    lambda r: r["token"].update(enabled=False), lambda r: r["token"].update(health="recovering"),
    lambda r: r["token"].update(dead_token_retries=1), lambda r: r["token"].update(forced_refresh=True),
    lambda r: r["token"].update(alternate_writer_enabled=True),
    lambda r: r["adapter"].update(refresh_enabled=True),
    lambda r: r["adapter"].update(provenance="unknown"),
    lambda r: r["token"].update(expires_at=(NOW + timedelta(seconds=179)).isoformat()),
    lambda r: r["http_hashes"].update({"/bot/orb": "a" * 64}),
    lambda r: r.update(raw_orb_api_text="{}")])
def test_control_owner_token_pid_time_and_raw_proof_negatives(change):
    receipt = control_receipt("after", 1100, 100)
    change(receipt)
    with pytest.raises(policy.Stop):
        control.validate(receipt, "after", 1100, 100, NOW, NOW)


def test_token_store_margin_health_no_tokens_returned():
    status = dict(enabled=True, health="healthy", dead_token_retries=0, last_error="")
    store = dict(access_token="CONTROLLED_SECRET", refresh_token="CONTROLLED_REFRESH",
                 expires_at=(NOW + timedelta(seconds=180)).isoformat())
    result = control.token_proof(status, store, NOW)
    assert "SECRET" not in json.dumps(result) and "REFRESH" not in json.dumps(result)
    store["expires_at"] = (NOW + timedelta(seconds=179)).isoformat()
    with pytest.raises(policy.Stop):
        control.token_proof(status, store, NOW)


def test_sole_process_census_complete_and_extra_owner_detected(tmp_path):
    for pid, raw in (("100", b"/home/trader/.venv/bin/mai-tai-control\0"), ("200", b"python\0other.py\0")):
        (tmp_path / pid).mkdir()
        (tmp_path / pid / "cmdline").write_bytes(raw)
    assert control.owners(tmp_path) == [100]
    (tmp_path / "200/cmdline").write_bytes(b"uvicorn\0project_mai_tai.services.control_plane:app\0")
    assert control.owners(tmp_path) == [100, 200]
    (tmp_path / "200/cmdline").write_bytes(b"x" * 4097)
    with pytest.raises(policy.Stop):
        control.owners(tmp_path)


@pytest.mark.parametrize("change", [lambda s: s.replace("[1100]", "[100]"),
    lambda s: s + "INFO:     Started server process [1100]\n", lambda s: s.replace("Application startup complete.", ""),
    lambda s: s.replace("[SCHWAB-TOKEN-REFRESHER] starting ", ""),
    lambda s: s.replace(":8100", ":8101"), lambda s: s + "Traceback error\n",
    lambda s: s.replace("20:10:00,000", "20:09:59,999")])
def test_new_control_log_exact_pid_startup_refresher_port_failure_negatives(change):
    state = dict(fleet()["control"], MainPID=1100, ExecMainStartTimestamp="Tue 2026-10-06 20:10:00 UTC")
    with pytest.raises(policy.Stop):
        post_proof.control_logs(dict(text=change(control_log()), ranges=[]), state)


def test_new_control_log_accepts_real_uvicorn_unstamped_headers():
    state = dict(fleet()["control"], MainPID=1100, ExecMainStartTimestamp="Tue 2026-10-06 20:10:00 UTC")
    assert post_proof.control_logs(dict(text=control_log(), ranges=[]), state)["pid"] == 1100


@pytest.mark.parametrize("action", ["stop", "start", "reload"])
def test_control_only_atomic_restart_other_actions_still_block(tmp_path, action):
    with pytest.raises(policy.Stop, match="out-of-scope"):
        attended.Real(tmp_path, {}, tmp_path).action(action, "control")


def test_literal_control_page_failure_aborts_after_application_proof_no_repeat(monkeypatch, tmp_path):
    from test_literal_rehearsal import setup
    fx, _, env = setup(monkeypatch, tmp_path)
    old_env = env.read_bytes()
    original = fx.command
    def command(args, **kwargs):
        value = original(args, **kwargs)
        if any(str(arg).endswith("control_display_proof.py") for arg in args):
            assert "--page-only" in args and "--phase" not in args
            value.returncode = 1
        return value
    monkeypatch.setattr(fx, "command", command)
    with pytest.raises(policy.Stop):
        attended.Sequence(fx).run()
    actions = [args for args in fx.calls if args[:2] == ["systemctl", "restart"]]
    assert len(actions) == 1
    assert env.read_bytes() != old_env and fx.head == policy.APP
    assert any(path.name.endswith("post-start-proof.json") for path in fx.attempt.iterdir())
    assert (fx.attempt / "STOP.json").exists() and not (fx.attempt / "COMPLETE.json").exists()
    assert not (tmp_path / "daily").exists()


@pytest.mark.parametrize("fault", [None, "status", "redirect", "encoding", "type", "cache", "empty", "overflow", "timeout"])
def test_control_http_only_four_readonly_uncached_bounded_gets(monkeypatch, fault):
    url = "http://127.0.0.1:8100/health"
    reply = SimpleNamespace(status=200, url=url,
                            headers={"Content-Type": "application/json", "Cache-Control": "no-store"})
    raw = b"{}"
    if fault == "status":
        reply.status = 503
    elif fault == "redirect":
        reply.url += "/redirect"
    elif fault in {"encoding", "type", "cache"}:
        key, value = {"encoding": ("Content-Encoding", "gzip"), "type": ("Content-Type", "text/html"),
                      "cache": ("Cache-Control", "public")}[fault]
        reply.headers[key] = value
    elif fault == "empty":
        raw = b""
    elif fault == "overflow":
        raw = b"x" * (control.MAX + 1)
    class Reply:
        def __enter__(self):
            return reply
        def __exit__(self, *args):
            return False
    def read(count):
        assert count == control.MAX + 1
        return raw
    reply.read = read
    def open_request(request, timeout):
        assert request.full_url == url and request.get_method() == "GET" and timeout == 10
        assert request.get_header("Cache-control") == "no-cache"
        if fault == "timeout":
            raise TimeoutError("controlled read timeout")
        return Reply()
    def opener(*handlers):
        assert handlers[0].proxies == {} and isinstance(handlers[1], control.NoRedirect)
        return SimpleNamespace(open=open_request)
    monkeypatch.setattr(control, "build_opener", opener)
    if fault is None:
        assert control.get("/health") == b"{}"
    else:
        with pytest.raises((policy.Stop, TimeoutError)):
            control.get("/health")
    with pytest.raises(policy.Stop, match="unapproved"):
        control.get("/auth/schwab/refresh")
    assert control.NoRedirect().redirect_request(None, None, 302, "", {}, "other") is None


@pytest.mark.parametrize("raw", [b"OTHER=value\0", b"MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=false\0",
    b"MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=true\0", b"mai_tai_schwab_adapter_token_refresh_enabled=false\0",
    b"MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=false\0MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=false\0",
    b"bad\0", b"x" * 262145, None])
def test_adapter_owner_bracket_canonical_false_or_pinned_default_only(monkeypatch, tmp_path, raw):
    state = {key: str(fleet()["oms"][key]) for key in FIELDS}
    path = tmp_path / "environ"
    if raw is not None:
        path.write_bytes(raw)
    monkeypatch.setattr(control, "state", lambda service: state)
    monkeypatch.setattr(control, "Path", lambda value: SimpleNamespace(open=path.open))
    allowed = raw in (b"OTHER=value\0", b"MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=false\0")
    if allowed:
        result = control.adapter_owner()
        assert result["pid"] == int(state["MainPID"]) and result["refresh_enabled"] is False
    else:
        with pytest.raises((policy.Stop, FileNotFoundError)):
            control.adapter_owner()


def test_adapter_owner_drift_blocks(monkeypatch, tmp_path):
    path = tmp_path / "environ"
    path.write_bytes(b"OTHER=value\0")
    state = {key: str(fleet()["oms"][key]) for key in FIELDS}
    replies = iter([state, dict(state, MainPID="9999")])
    monkeypatch.setattr(control, "state", lambda service: next(replies))
    monkeypatch.setattr(control, "Path", lambda value: SimpleNamespace(open=path.open))
    with pytest.raises(policy.Stop, match="identity moved"):
        control.adapter_owner()


@pytest.mark.parametrize("fault", [None, "extra-owner", "owner-drift", "pid-drift", "old-pid", "health", "token", "json", "writer"])
def test_collect_exact_readonly_boundaries_failclosed(monkeypatch, tmp_path, fault):
    import project_mai_tai.settings as settings_module
    store = tmp_path / "tokens.json"
    store.write_text(json.dumps(dict(access_token="CONTROLLED_ACCESS", refresh_token="CONTROLLED_REFRESH",
                                    expires_at=(NOW + timedelta(hours=1)).isoformat())))
    settings = SimpleNamespace(schwab_adapter_token_refresh_enabled=fault == "writer", schwab_token_store_path=str(store))
    monkeypatch.setattr(settings_module, "Settings", lambda **kwargs: settings)
    class Clock:
        @staticmethod
        def now(zone):
            return NOW
    monkeypatch.setattr(control, "datetime", Clock)
    state = {key: str(fleet()["control"][key]) for key in FIELDS}
    state["MainPID"] = "1100"
    states = iter([state, dict(state, MainPID="9999") if fault == "pid-drift" else state])
    monkeypatch.setattr(control, "state", lambda: next(states))
    owner_rows = iter([[1100, 100] if fault == "extra-owner" else [1100],
                       [1100, 100] if fault == "owner-drift" else [1100]])
    monkeypatch.setattr(control, "owners", lambda: next(owner_rows))
    monkeypatch.setattr(control, "adapter_owner", lambda: dict(pid=1103, refresh_enabled=False,
                                                              provenance="pinned-settings-default-false"))
    def path(value):
        if str(value) == "/proc/100":
            return SimpleNamespace(exists=lambda: fault == "old-pid")
        return Path(value)
    monkeypatch.setattr(control, "Path", path)
    status = dict(enabled=True, health="healthy", dead_token_retries=0, last_error="")
    health = dict(service="control-plane", database_connected=fault != "health", redis_connected=True, errors=[])
    if fault == "token":
        status["dead_token_retries"] = 1
    calls = []
    def get(path):
        calls.append(path)
        if fault == "json":
            return b"not-json"
        data = {"/health": json.dumps(health), "/api/overview": json.dumps(dict(schwab_token_refresher=status)),
                "/bot/orb": PAGE, "/api/bot/orb-schwab": json.dumps(api())}[path]
        return data.encode()
    monkeypatch.setattr(control, "get", get)
    if fault is None:
        result = control.collect("after", 1100, 100)
        assert control.validate(result, "after", 1100, 100, NOW, NOW)["page"]["trades"] == 1
        assert calls == list(control.PATHS) and "CONTROLLED_ACCESS" not in json.dumps(result)
    else:
        with pytest.raises((policy.Stop, json.JSONDecodeError)):
            control.collect("after", 1100, 100)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "stderr", "overflow", "readfailure"])
def test_control_systemd_state_unreadable_never_defaults(monkeypatch, fault):
    state = {key: str(fleet()["control"][key]) for key in FIELDS}
    raw = "\n".join(key + "=" + value for key, value in state.items()).encode()
    if fault == "missing":
        raw = b"MainPID=1100\n"
    elif fault == "duplicate":
        raw += b"\nMainPID=1100\n"
    elif fault == "overflow":
        raw = b"x" * 4097
    def run(args, **kwargs):
        assert kwargs == dict(check=True, capture_output=True, timeout=5)
        if fault == "readfailure":
            raise OSError("controlled read failure")
        return SimpleNamespace(stdout=raw, stderr=b"error" if fault == "stderr" else b"")
    monkeypatch.setattr(control.subprocess, "run", run)
    with pytest.raises((policy.Stop, OSError)):
        control.state()


@pytest.mark.parametrize("change", [lambda old, new: new.update(MainPID=old["MainPID"]),
    lambda old, new: new.update(NRestarts=1), lambda old, new: new.update(ActiveState="inactive"),
    lambda old, new: new.update(SubState="failed"), lambda old, new: new.update(Result="exit-code"),
    lambda old, new: new.update(ExecMainStartTimestampMonotonic=old["ExecMainStartTimestampMonotonic"]),
    lambda old, new: new.update(InvocationID=old["InvocationID"]), lambda old, new: new.update(InvocationID="unknown")])
def test_final_control_new_identity_and_invocation_each_required(change):
    before, current = fleet(), fleet()
    for name in policy.CHANGED:
        current[name].update(MainPID=before[name]["MainPID"] + 1000,
                             ExecMainStartTimestampMonotonic=before[name]["ExecMainStartTimestampMonotonic"] + 1000)
    current["control"]["InvocationID"] = "f" * 32
    policy.states(before, current, len(policy.PHASES))
    change(before["control"], current["control"])
    with pytest.raises(policy.Stop):
        policy.states(before, current, len(policy.PHASES))


def test_literal_control_has_no_owner_gate_and_page_follows_application_proof(monkeypatch, tmp_path):
    from test_literal_rehearsal import setup
    fx, _, _ = setup(monkeypatch, tmp_path)
    original = fx.command
    def command(args, **kwargs):
        value = original(args, **kwargs)
        if any(str(arg).endswith("control_display_proof.py") for arg in args):
            assert "--page-only" in args and "--phase" not in args
            assert any(path.name.endswith("post-start-proof.json") for path in fx.attempt.iterdir())
            assert fx.started.keys() == {"oms", "orb-schwab", "schwab-1m-v2"}
        return value
    monkeypatch.setattr(fx, "command", command)
    attended.Sequence(fx).run()
    assert sum(args[:2] == ["systemctl", "restart"] for args in fx.calls) == 1
    assert (fx.attempt / "COMPLETE.json").exists()


@pytest.mark.parametrize("page,rc", [(PAGE, 0), (PAGE.replace("LIVE/SCHWAB", "PAPER"), 1)])
def test_page_only_mode_never_reads_token_owner_or_overview(monkeypatch, capsys, page, rc):
    monkeypatch.setattr(control.sys, "argv", ["control_display_proof.py", "--page-only"])
    paths = []
    def get(path):
        paths.append(path)
        return page.encode()
    monkeypatch.setattr(control, "get", get)
    monkeypatch.setattr(control, "collect", lambda *args: pytest.fail("withdrawn control gate invoked"))
    assert control.main() == rc
    assert paths == ["/bot/orb"]
    if rc == 0:
        assert json.loads(capsys.readouterr().out)["provider"] == "SCHWAB"


@pytest.mark.parametrize("size,allowed", [(1538579, True), (2000001, False)])
def test_recorded_overview_size_uses_two_mb_thirty_second_bound(monkeypatch, size, allowed):
    url = "http://127.0.0.1:8100/api/overview"
    class Reply:
        status = 200
        headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
        def __enter__(self):
            self.url = url
            return self
        def __exit__(self, *args):
            return False
        def read(self, count):
            assert count == 2000001
            return b"x" * size
    def open_request(request, timeout):
        assert request.full_url == url and timeout == 30
        return Reply()
    monkeypatch.setattr(control, "build_opener", lambda *handlers: SimpleNamespace(open=open_request))
    if allowed:
        assert len(control.get("/api/overview")) == size
    else:
        with pytest.raises(policy.Stop, match="missing/overflow"):
            control.get("/api/overview")


@pytest.mark.parametrize("fault,rc,label", [(policy.Stop("control sole process owner unproven"), 1, "STOP"),
                                          (TimeoutError("controlled timeout"), 2, "UNKNOWN")])
def test_control_cli_distinguishes_measured_blocker_from_unreadable(monkeypatch, capsys, fault, rc, label):
    monkeypatch.setattr(control.sys, "argv", ["control_display_proof.py", "--phase", "before", "--pid", "2916"])
    def collect(*args):
        raise fault
    monkeypatch.setattr(control, "collect", collect)
    assert control.main() == rc
    assert capsys.readouterr().err.startswith(label + " control proof")

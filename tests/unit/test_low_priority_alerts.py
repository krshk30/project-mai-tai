from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


OPS = Path(__file__).resolve().parents[2] / "ops" / "health"
PREOPEN_URL = "https://ntfy.sh/mai-tai-preopen-28806a5a97b7"
LOW_URL = "https://ntfy.sh/mai-tai-routine-112964cc8f26787132a29538"
spec = importlib.util.spec_from_file_location("low_priority_alerts", OPS / "low_priority_alerts.py")
assert spec and spec.loader
alerts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(alerts)


def test_low_sender_preserves_body_and_records_only_accepted_send(monkeypatch, tmp_path) -> None:
    calls = []

    def transport(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setenv("MAI_TAI_LOW_ALERT_SPOOL", str(tmp_path))
    monkeypatch.setattr(alerts.subprocess, "run", transport)
    body = "PAGE PMAX 14:24\nSchwab policy rejected\nWebull one-share leg remains"
    assert alerts.send_low("reject-watch", "Intent refusal class - our defect", body)
    command, kwargs = calls[0]
    assert alerts.LOW_URL == command[-1]
    assert "Priority: low" in command
    assert kwargs["input"] == body
    record = json.loads(next(tmp_path.glob("*.jsonl")).read_text().strip())
    assert record["sender"] == "reject-watch"
    assert record["last_line"] == "Webull one-share leg remains"

    monkeypatch.setattr(
        alerts.subprocess, "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 22, "", ""),
    )
    assert not alerts.send_low("reject-watch", "failed", "not accepted")
    assert len(next(tmp_path.glob("*.jsonl")).read_text().splitlines()) == 1


def test_digest_lists_all_senders_once_at_2000_et(monkeypatch, tmp_path) -> None:
    calls = []

    def transport(command, **kwargs):
        calls.append((command, kwargs["input"]))
        return subprocess.CompletedProcess(command, 0, stdout="{}", stderr="")

    monkeypatch.setenv("MAI_TAI_LOW_ALERT_SPOOL", str(tmp_path))
    monkeypatch.setattr(alerts.subprocess, "run", transport)
    et = ZoneInfo("America/New_York")
    now = datetime(2026, 9, 23, 20, 0, tzinfo=et)
    (tmp_path / "2026-09-23.jsonl").write_text(
        "\n".join(
            json.dumps({"sender": sender, "last_line": line})
            for sender, line in [
                ("seed-exposure", "first"),
                ("reject-watch", "PMAX"),
                ("seed-exposure", "last"),
            ]
        ) + "\n"
    )
    assert alerts.send_digest(now.replace(hour=19, minute=59))
    assert calls == []
    assert alerts.send_digest(now)
    assert alerts.send_digest(now)
    assert len(calls) == 1
    command, body = calls[0]
    assert command[-1] == alerts.DIGEST_URL
    assert "Priority: default" in command
    for line in (
        "reject-watch: count=1 last=PMAX",
        "seed-exposure: count=2 last=last",
        "entry-fix: count=0 last=(none)",
        "d6-outcome: count=0 last=(none)",
        "bar-gap: count=0 last=(none)",
        "eod: count=0 last=(none)",
    ):
        assert line in body


def test_digest_install_plan_guards_both_sources_and_dst_candidates() -> None:
    installer = OPS / "install_low_priority_digest.sh"
    result = subprocess.run(
        ["bash", str(installer), "--print-cron"],
        env={**os.environ, "ALERT_SPLIT_REPO_ROOT": str(OPS.parents[1])},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "0 0,1 * * *" in result.stdout
    assert result.stdout.count("sha256sum") == 2
    assert "# BEGIN mai-tai-low-priority-digest" in result.stdout
    assert "# END mai-tai-low-priority-digest" in result.stdout


def test_digest_guard_refusal_logs_and_sends_one_low_alert(monkeypatch, tmp_path) -> None:
    fake_date = tmp_path / "date"
    fake_date.write_text(
        '#!/bin/bash\nif [[ "$1" == +%H%M ]]; then echo "$FAKE_ET_TIME"; '
        'else echo test-stamp; fi\n',
        encoding="utf-8",
    )
    fake_date.chmod(0o755)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" >> "$GUARD_CURL_ARGS"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    fake_sha = tmp_path / "sha256sum"
    real_sha = shutil.which("sha256sum")
    assert real_sha
    fake_sha.write_text(
        f'#!/bin/bash\nif [[ "$1" == *"$BAD_GUARD_PATH" ]]; then echo bad; '
        f'else {real_sha} "$@"; fi\n',
        encoding="utf-8",
    )
    fake_sha.chmod(0o755)
    log = tmp_path / "digest.log"
    args = tmp_path / "curl.args"
    env = {
        **os.environ,
        "ALERT_SPLIT_REPO_ROOT": str(OPS.parents[1]),
        "ALERT_SPLIT_DIGEST_LOG": str(log),
        "ALERT_SPLIT_CURL": str(fake_curl),
        "GUARD_CURL_ARGS": str(args),
        "FAKE_ET_TIME": "2000",
    }
    plan = subprocess.run(
        ["bash", str(OPS / "install_low_priority_digest.sh"), "--print-cron"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert plan.returncode == 0
    command = plan.stdout.splitlines()[1].split(" * * * ", 1)[1]
    for filename in ("low_priority_alerts.py", "low_priority_digest_cron.sh"):
        result = subprocess.run(
            ["bash", "-c", command],
            env={
                **env,
                "PATH": f"{tmp_path}:{os.environ['PATH']}",
                "BAD_GUARD_PATH": filename,
            },
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0
        assert log.read_text().count("digest guard refused: ") == 1
        sent = args.read_text()
        assert sent.count("https://ntfy.sh/") == 1
        assert LOW_URL in sent
        assert "Priority: low" in sent
        assert f"digest guard refused: {OPS / filename}" in sent
        log.unlink()
        args.unlink()

    result = subprocess.run(
        ["bash", "-c", command],
        env={
            **env,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "BAD_GUARD_PATH": "low_priority_alerts.py",
            "FAKE_ET_TIME": "1900",
        },
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert not log.exists() and not args.exists()


def test_seed_exposure_moves_low_without_changing_body_or_urgent_preopen(monkeypatch, tmp_path) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$FAKE_CURL_ARGS"\n'
        'cat > "$FAKE_CURL_BODY"\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    env = {
        **os.environ,
        "PREOPEN_ALERT_OUT": str(tmp_path),
        "PREOPEN_ALERT_CURL": str(fake_curl),
        "PREOPEN_LOW_ALERT_PYTHON": sys.executable,
        "PREOPEN_LOW_ALERT_SCRIPT": str(OPS / "low_priority_alerts.py"),
        "MAI_TAI_LOW_ALERT_CURL": str(fake_curl),
        "MAI_TAI_LOW_ALERT_SPOOL": str(tmp_path / "spool"),
        "FAKE_CURL_ARGS": str(tmp_path / "args"),
        "FAKE_CURL_BODY": str(tmp_path / "body"),
    }
    script = OPS / "preopen_alert.sh"
    result = subprocess.run(
        ["bash", str(script), "AMBER", "SEED-EXPOSURE 3 exposed", "/tmp/report"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    args = (tmp_path / "args").read_text()
    assert "Title: AMBER mai-tai readiness warnings" in args
    assert "Priority: low" in args
    assert LOW_URL in args
    assert (tmp_path / "body").read_text() == "SEED-EXPOSURE 3 exposed"

    result = subprocess.run(
        ["bash", str(script), "RED", "OMS unavailable", "/tmp/report"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    args = (tmp_path / "args").read_text()
    assert "Priority: urgent" in args
    assert PREOPEN_URL in args
    assert "RED mai-tai NOT READY" in args

    result = subprocess.run(
        ["bash", str(script), "RED", "SEED-EXPOSURE RED", "/tmp/report"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    args = (tmp_path / "args").read_text()
    assert PREOPEN_URL in args
    assert "Priority: urgent" in args
    assert LOW_URL not in args
    assert "SEED-EXPOSURE RED" in args


def test_urgent_inc1_and_oco_0923_routes_stay_unchanged(monkeypatch) -> None:
    inc_spec = importlib.util.spec_from_file_location("unexercised_watch", OPS / "unexercised_watch.py")
    oco_spec = importlib.util.spec_from_file_location("oco_capture_check", OPS / "oco_capture_check.py")
    assert inc_spec and inc_spec.loader and oco_spec and oco_spec.loader
    inc = importlib.util.module_from_spec(inc_spec)
    oco = importlib.util.module_from_spec(oco_spec)
    monkeypatch.setitem(sys.modules, inc_spec.name, inc)
    monkeypatch.setitem(sys.modules, oco_spec.name, oco)
    monkeypatch.setitem(sys.modules, "requests", SimpleNamespace(post=lambda *args, **kwargs: None))
    inc_spec.loader.exec_module(inc)
    oco_spec.loader.exec_module(oco)
    sent = []

    def inc_runner(command, **kwargs):
        sent.append(command)
        return subprocess.CompletedProcess(
            command, 0, stdout='{"id":"inc1-fixture"}\n__HTTP_STATUS__:200', stderr="",
        )

    assert inc.page(
        "UNCOVERED: BENF on live:orb has NO broker stop (31s); check now",
        "09-23 INC1 replay",
        runner=inc_runner,
    )
    assert "Priority: high" in sent[-1]
    assert sent[-1][-1] == PREOPEN_URL

    class Response:
        status_code = 200

    monkeypatch.setattr(
        oco.requests,
        "post",
        lambda url, **kwargs: (sent.append((url, kwargs)), Response())[1],
    )
    oco.push(
        "OCO capture RED - 11:15 ET",
        "09-23 entry fill closed with NO exit fill recorded",
        "urgent",
        "rotating_light",
    )
    url, kwargs = sent[-1]
    assert url == PREOPEN_URL
    assert kwargs["headers"]["Priority"] == "urgent"


def _route_function(path: Path, name: str) -> str:
    source = path.read_text(encoding="utf-8")
    start = source.index(f"{name}() {{")
    return source[start:source.index("\n}\n", start) + 3]


def test_bar_gap_red_is_urgent_and_amber_stays_low(tmp_path) -> None:
    python = tmp_path / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$ROUTE_ARGS"\n'
        'if [[ " $* " == *"@-"* ]]; then cat > "$ROUTE_BODY"; fi\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    source = _route_function(OPS / "bar_gap_watch_cron.sh", "send_ntfy")
    command = (
        f"REPO={shlex.quote(str(tmp_path))}; "
        f"LOW_ALERT={shlex.quote(str(OPS / 'low_priority_alerts.py'))}; "
        f"OUT={shlex.quote(str(tmp_path))}; NTFY_URL={shlex.quote(PREOPEN_URL)}; "
        f"{source}\nsend_ntfy \"$1\" \"$2\" warning \"$3\""
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "MAI_TAI_LOW_ALERT_CURL": str(fake_curl),
        "MAI_TAI_LOW_ALERT_SPOOL": str(tmp_path / "spool"),
        "ROUTE_ARGS": str(tmp_path / "args"),
        "ROUTE_BODY": str(tmp_path / "body"),
    }
    for title, priority, expected_url, expected_priority in (
        ("RED v2 BAR HOLE", "urgent", PREOPEN_URL, "urgent"),
        ("AMBER v2 bar gap", "default", LOW_URL, "low"),
    ):
        result = subprocess.run(
            ["bash", "-c", command, "route-test", title, priority, "same body"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        args = (tmp_path / "args").read_text()
        assert expected_url in args
        assert f"Priority: {expected_priority}" in args
        assert "same body" in args or (tmp_path / "body").read_text() == "same body"


def test_entry_cap_and_p0a_red_are_urgent_other_entry_alert_stays_low(tmp_path) -> None:
    fake_curl = tmp_path / "curl"
    fake_curl.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$@" > "$ROUTE_ARGS"\n'
        'if [[ " $* " == *"@-"* ]]; then cat > "$ROUTE_BODY"; fi\n',
        encoding="utf-8",
    )
    fake_curl.chmod(0o755)
    source = _route_function(OPS / "v2_entry_fix_watch_cron.sh", "push")
    command = (
        f"PY={shlex.quote(sys.executable)}; "
        f"LOW_ALERT={shlex.quote(str(OPS / 'low_priority_alerts.py'))}; "
        f"NTFY_URL={shlex.quote(PREOPEN_URL)}; "
        f"LOG={shlex.quote(str(tmp_path / 'watch.log'))}; STAMP=test; "
        f"{source}\npush \"$1\" \"$2\" \"$3\""
    )
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "MAI_TAI_LOW_ALERT_CURL": str(fake_curl),
        "MAI_TAI_LOW_ALERT_SPOOL": str(tmp_path / "spool"),
        "ROUTE_ARGS": str(tmp_path / "args"),
        "ROUTE_BODY": str(tmp_path / "body"),
    }
    for title, priority, expected_url, expected_priority in (
        ("V2 ENTRY CAP BREACHED", "urgent", PREOPEN_URL, "urgent"),
        ("P0a NOT HOLDING - KUST signature", "urgent", PREOPEN_URL, "urgent"),
        ("V2 first live cross 2026-09-23", "default", LOW_URL, "low"),
    ):
        result = subprocess.run(
            ["bash", "-c", command, "route-test", title, priority, "same body"],
            env=env, capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        args = (tmp_path / "args").read_text()
        assert expected_url in args
        assert f"Priority: {expected_priority}" in args
        assert "same body" in args or (tmp_path / "body").read_text() == "same body"

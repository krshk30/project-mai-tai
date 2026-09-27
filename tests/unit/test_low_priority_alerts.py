from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


OPS = Path(__file__).resolve().parents[2] / "ops" / "health"
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
    assert "Priority: low" in command
    assert "seed-exposure: count=2 last=last" in body
    assert "reject-watch: count=1 last=PMAX" in body
    assert "d6-outcome: count=0 last=(none)" in body


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
    assert alerts.LOW_URL in args
    assert (tmp_path / "body").read_text() == "SEED-EXPOSURE 3 exposed"

    result = subprocess.run(
        ["bash", str(script), "RED", "OMS unavailable", "/tmp/report"],
        env=env, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    args = (tmp_path / "args").read_text()
    assert "Priority: urgent" in args
    assert alerts.DIGEST_URL in args
    assert "RED mai-tai NOT READY" in args


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
    assert sent[-1][-1] == inc.NTFY_URL

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
    assert url.endswith(oco.TOPIC)
    assert kwargs["headers"]["Priority"] == "urgent"

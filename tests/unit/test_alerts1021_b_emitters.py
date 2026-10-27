"""Emitter integration with synthetic local stubs; no real DB/provider/phone."""

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["manual", "bot", "failed", "partial", "missing_native", "unknown_alias"])
async def test_orphan_phone_filter_keeps_raw_reports_and_failed_lookup_risk(monkeypatch, capsys, mode):
    from project_mai_tai.broker_adapters import schwab

    path = ROOT / "ops/health/orphan_order_check.py"
    spec = importlib.util.spec_from_file_location("alerts_b_orphan", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    account = "live:schwab_1m_v2"
    queries, pages = [], []
    order = {"orderId": "" if mode == "missing_native" else "NATIVE1", "status": "WORKING",
             "price": 2, "enteredTime": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
             "orderType": "LIMIT", "orderLegCollection": [{"instruction": "BUY",
                 "quantity": 1, "instrument": {"symbol": "SYNTHETIC"}}]}

    class Broker:
        def __init__(self, settings, accounts_by_name):
            self.accounts_by_name = accounts_by_name

        async def _authorized_request_json(self, *args):
            return 200, {}, [order]

        @staticmethod
        def _parse_datetime(value):
            return datetime.fromisoformat(value)

    class Result:
        def __init__(self, values=()):
            self.values = values

        def mappings(self):
            if mode == "partial" and self.values:
                def partial():
                    yield self.values[0]
                    raise RuntimeError("SYNTHETIC incomplete cursor")
                return partial()
            return self.values

        def first(self):
            return SimpleNamespace(bid_price=1, ask_price=1)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, values=None):
            queries.append((str(sql), values))
            if mode == "failed":
                raise RuntimeError("SYNTHETIC database unavailable")
            if "SELECT name FROM broker_accounts" in str(sql):
                return Result([{"name": "legacy_unmapped"}] if mode == "unknown_alias" else [])
            if "FROM broker_orders" in str(sql):
                assert set(values["accounts"]) == {account, "live:alias_same_physical"}
                assert values["ids"] == ["NATIVE1"]
                assert "ba.provider = 'schwab'" in str(sql)
                rows = [] if mode in {"manual", "unknown_alias"} else [{"bid": "NATIVE1", "status": "cancelled", "failed_cancel_at": None}]
                return Result(rows)
            return Result()

    monkeypatch.setattr(schwab, "SchwabBrokerAdapter", Broker)
    monkeypatch.setattr(schwab, "configured_schwab_accounts", lambda settings: {
        account: SimpleNamespace(account_hash="SAME-PHYSICAL"),
        "live:alias_same_physical": SimpleNamespace(account_hash="SAME-PHYSICAL"),
        "live:foreign": SimpleNamespace(account_hash="DIFFERENT")})
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        strategy_schwab_1m_v2_account_name=account, redis_url="SYNTHETIC"))
    monkeypatch.setattr(module, "build_session_factory", lambda settings: Session)
    monkeypatch.setattr(module, "bot_watchlist", lambda url: set())
    monkeypatch.setattr(module, "push", lambda *args: pages.append(args))
    monkeypatch.setattr(module.sys, "argv", ["orphan_order_check.py"])
    assert await module.main() == 2  # raw detector status remains RED, even manual-only.
    printed = capsys.readouterr().out
    assert "RED:" in printed and "POLA shape" in printed
    if mode == "manual":
        assert not pages and "PHONE-ONLY EXCLUDED" in printed
    else:
        assert len(pages) == 1 and pages[0][0].startswith("ORPHAN ORDER RED")
        if mode in {"failed", "partial", "unknown_alias"}:
            assert "UNKNOWN" in pages[0][1]


@pytest.mark.parametrize("code,line,phone", [
    (2, "SCANNER_CAPTURE COULD_NOT_TELL database_read=FAILED", False),
    (2, "SCANNER_CAPTURE COULD_NOT_TELL feed_age_minutes=UNKNOWN", False),
    (1, "SCANNER_CAPTURE LOW_VOLUME rows=0 feed_age_minutes=0", True),
    (2, "UNCLASSIFIED UNKNOWN", True),
    (9, "broken checker", False),
])
def test_scanner_phone_only_gate_preserves_checker_status_and_raw_log(tmp_path, code, line, phone):
    check, curl, envfile = tmp_path / "check.py", tmp_path / "curl", tmp_path / "env"
    check.write_text(f"print({line!r})\nraise SystemExit({code})\n")
    curl.write_text('#!/bin/sh\nprintf "called\\n" >> "$CAPTURED_SEND"\n')
    curl.chmod(0o755)
    envfile.write_text("# synthetic no credentials\n")
    calls, log = tmp_path / "sends", tmp_path / "logs/cron.log"
    result = subprocess.run(["bash", str(ROOT / "ops/health/scanner_capture_verify_cron.sh")],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "SCANNER_CAPTURE_CHECK": str(check),
             "SCANNER_CAPTURE_PYTHON": sys.executable, "SCANNER_CAPTURE_CURL": str(curl),
             "SCANNER_CAPTURE_ENV_FILE": str(envfile), "SCANNER_CAPTURE_LOG": str(log),
             "SCANNER_CAPTURE_PHONE_GATE": str(ROOT / "ops/health/phone_alert_gate.py"),
             "CAPTURED_SEND": str(calls)}, capture_output=True, text=True, check=False)
    assert result.returncode == (1 if code == 1 else 2)
    assert line in log.read_text() and line in log.with_name("STATUS.txt").read_text()
    assert calls.exists() is phone

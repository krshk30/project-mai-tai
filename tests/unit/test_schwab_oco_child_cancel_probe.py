from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _load_probe():
    path = Path(__file__).parents[2] / "scripts" / "schwab_oco_child_cancel_probe.py"
    spec = importlib.util.spec_from_file_location("schwab_oco_child_cancel_probe_under_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry_tree(symbol: str = "PLUG") -> dict:
    return {
        "filledQuantity": 1,
        "orderLegCollection": [
            {
                "instruction": "BUY",
                "quantity": 1,
                "instrument": {"symbol": symbol, "assetType": "EQUITY"},
            }
        ],
        "childOrderStrategies": [
            {
                "childOrderStrategies": [
                    {
                        "orderId": 11,
                        "status": "CANCELED",
                        "orderType": "LIMIT",
                        "quantity": 1,
                        "orderLegCollection": [{"instruction": "SELL"}],
                    },
                    {
                        "orderId": 12,
                        "status": "CANCELED",
                        "orderType": "STOP",
                        "quantity": 1,
                        "orderLegCollection": [{"instruction": "SELL"}],
                    },
                ]
            }
        ],
    }


def test_exit_pm_cli_requires_and_passes_entry_id(monkeypatch):
    probe = _load_probe()
    seen = {}
    monkeypatch.setattr(probe, "cmd_exit_pm", lambda args: seen.update(vars(args)))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "probe",
            "exit-pm",
            "--symbol",
            "PLUG",
            "--entry",
            "entry-123",
            "--limit",
            "2.10",
            "--i-have-go",
        ],
    )

    probe.main()

    assert seen["entry"] == "entry-123"


def test_exit_pm_refuses_entry_symbol_mismatch_before_sell(monkeypatch, capsys):
    probe = _load_probe()
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        if method == "POST":
            pytest.fail("mismatched entry reached the live sell")
        return 200, json.dumps(_entry_tree("PLUG"))

    monkeypatch.setattr(probe, "account_hash", lambda: "account")
    monkeypatch.setattr(probe, "call", fake_call)

    with pytest.raises(SystemExit):
        probe.cmd_exit_pm(
            SimpleNamespace(entry="plug-entry", symbol="CHPT", limit=8.0, i_have_go=True)
        )

    assert "entry belongs to PLUG, not CHPT" in capsys.readouterr().out
    assert all(method != "POST" for method, _path, _body in calls)


def test_exit_pm_matching_entry_reaches_one_sell(monkeypatch):
    probe = _load_probe()
    calls = []

    def fake_call(method, path, body=None):
        calls.append((method, path, body))
        return (200, json.dumps(_entry_tree("PLUG"))) if method == "GET" else (201, "")

    monkeypatch.setattr(probe, "account_hash", lambda: "account")
    monkeypatch.setattr(probe, "call", fake_call)
    monkeypatch.setattr(probe, "held_quantity", lambda _account, _symbol: 1.0)
    monkeypatch.setattr(probe, "quote_px", lambda _symbol: (2.10, 2.11))

    probe.cmd_exit_pm(
        SimpleNamespace(entry="plug-entry", symbol="PLUG", limit=2.10, i_have_go=True)
    )

    posts = [body for method, _path, body in calls if method == "POST"]
    assert len(posts) == 1
    assert posts[0]["orderLegCollection"][0]["instrument"]["symbol"] == "PLUG"

"""Pin census rc1/rc2 without broker/DB calls or allowance-policy edits."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys

import pytest


@pytest.mark.parametrize("kind,message,expected", (
    ("Stop", "SQL nonterminal orders/intents present", 1),
    ("Stop", "outside/missing reviewed 14-ticket population: STOP", 1),
    ("Stop", "linked exact parent is not terminal", 1),
    ("Stop", "Webull exact parent symbol/side/quantity mismatch", 1),
    ("Unreadable", "Webull exact parent GET unreadable: client error_class=ServerException", 2),
    ("Unreadable", "Schwab GET HTTP 503; no internal retry/refresh", 2),
    ("RuntimeError", "driver failure", 2),
))
def test_census_measured_blocker_one_unreadable_two(monkeypatch, capsys, kind, message, expected):
    path = Path(__file__).with_name("census_readonly.py")
    spec = importlib.util.spec_from_file_location("exit_census", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", [str(path), "--require-reviewed"])
    monkeypatch.setattr(module, "Settings", lambda **kwargs: object())
    monkeypatch.setattr(module, "source_bound", lambda result: None)

    def refuse(config, result):
        exception = RuntimeError if kind == "RuntimeError" else getattr(module, kind)
        raise exception(message)

    monkeypatch.setattr(module, "sql_census", refuse)
    assert asyncio.run(module.main()) == expected
    result = json.loads(capsys.readouterr().out)
    assert result["rc"] == expected and result["readonly"] is True
    assert result["install_authority"] is False
    assert result["stop"] == ("unreadable evidence: RuntimeError" if kind == "RuntimeError" else message)

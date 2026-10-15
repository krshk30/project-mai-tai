import json
import sys

import pytest

from project_mai_tai.services import momentum_live_sec_prepare as prepare


@pytest.mark.parametrize("status,http_errors,failed_count,expected", [
    ("FULL", {"403": 0, "429": 0}, 1, 0),
    ("PARTIAL", {"403": 0, "429": 0}, 1, 2),
    ("FULL", {"403": 1, "429": 0}, 1, 2),
    ("FULL", {"403": 0, "429": 1}, 1, 2),
    ("FULL", {"403": 0, "429": 0, "500": 1}, 1, 2),
    ("FULL", {"403": 0, "429": 0}, False, 2),
    ("FULL", {"403": 0, "429": 0}, -1, 2),
    ("FULL", {"403": 0, "429": 0}, None, 2),
    ("FULL", {"403": 0, "429": 0}, "1", 2),
])
def test_prepare_retains_fetch_failed_diagnostics_and_transport_refusals(
    monkeypatch, capsys, status, http_errors, failed_count, expected,
):
    report = {"status": status, "http_errors": http_errors,
              "counts": {"US": 1, "UNKNOWN": 1, "FETCH_FAILED": failed_count}}
    class Client:
        def nightly(self, **kwargs):
            assert kwargs["cohort"] == "SEC_INDEX"
            assert kwargs["security_type"] == "TYPE_UNKNOWN"
            return report
    monkeypatch.setattr(sys, "argv", ["prepare", "--cohort", "SEC_INDEX",
        "--security-type", "TYPE_UNKNOWN", "--lookup-day", "2026-10-19"])
    monkeypatch.setenv("MAI_TAI_SEC_USER_AGENT", "controlled@example.test")
    monkeypatch.setattr(prepare, "get_settings", lambda: object())
    monkeypatch.setattr(prepare, "build_timed_session_factory", lambda *a, **k: object())
    monkeypatch.setattr(prepare, "CountryStore", lambda *a, **k: object())
    monkeypatch.setattr(prepare, "SecClient", lambda *a, **k: Client())
    assert prepare.main() == expected
    assert json.loads(capsys.readouterr().out) == report

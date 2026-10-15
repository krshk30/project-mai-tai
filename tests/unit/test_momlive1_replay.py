from datetime import UTC, datetime
import json

from project_mai_tai.services.momentum_live_replay import replay


def retained(quote):
    now = datetime(2026, 10, 14, 14, tzinfo=UTC)
    return json.dumps({"signals": [{"logical_id": "retained-detection", "symbol": "SCNI",
        "observed_decision_ns": int(now.timestamp()*1e9), "signal": {"price": "3"},
        "historical_massive_quote_candidate": quote}]}).encode()


def test_retained_proxy_uses_actual_ceiling_wire_formatter_but_never_mints_volume_book_or_trade():
    result = replay(retained({"ask_price": "3.001", "provider": "massive",
        "received_at": "2026-10-14T13:59:59+00:00", "event_ts": "2026-10-14T13:59:59+00:00"}))
    row = result["rows"][0]
    assert row["conditional_arithmetic"]["limit"] == "3.01"
    assert row["conditional_arithmetic"]["quantity"] == "33"
    assert row["entry_decision"] == "SKIP_UNPROVEN_VOLUME_AND_OMS_BOOK" and row["wire_count"] == 0
    assert row["historical_country_known_at_decision"] == "UNKNOWN_NOT_RETAINED"
    assert result["actual_wire_count"] == 0 and result["unknown_volume_count"] == 1


def test_absent_future_and_cap_exceeding_quotes_never_become_qualified_replay_entries():
    for quote in (None, {"ask_price": "3", "received_at": "2026-10-14T14:00:01+00:00",
                        "event_ts": "2026-10-14T14:00:01+00:00"},
                  {"ask_price": "3.05", "received_at": "2026-10-14T13:59:59+00:00",
                   "event_ts": "2026-10-14T13:59:59+00:00"}):
        row = replay(retained(quote))["rows"][0]
        assert row["conditional_arithmetic"] is None and row["wire_count"] == 0

"""L3 defaults and recorded provider metadata, without live I/O."""

import json
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from project_mai_tai.momentum_paper.conditions import build_condition_snapshot
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings


FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/l3_wff_20261009.json").read_text())
FLAG = "strategy_schwab_1m_v2_gap_trade_conditions_filter_enabled"


def query(enabled):
    bot = object.__new__(SchwabV2BotService)
    bot.settings = Settings(**{FLAG: enabled})
    session = MagicMock()
    session.execute.return_value = []
    bot.session_factory = MagicMock()
    bot.session_factory.return_value.__enter__.return_value = session
    bot._read_line_gap_trades("WFF", ((1791572940000, 1791573240000),))
    assert session.execute.call_count == 3  # Two SET LOCALs and the original single SELECT.
    return session.execute.call_args.args


def test_l3_default_and_catalog_are_on_and_v2_owned():
    assert Settings.model_fields[FLAG].default is True
    catalog = json.loads((Path(__file__).parents[2] / "ops/health/expected_flags.json").read_text())
    entries = [row for row in catalog["flags"] if row["name"] == FLAG]
    assert len(entries) == 1
    assert entries[0]["expected"] is True
    assert entries[0]["owning_service"] == "schwab-1m-v2"
    assert "also_check_services" not in entries[0]


def test_l3_bar_creation_rules_match_full_recorded_reference_not_one_wff_code():
    statement, params = query(True)
    snapshot = build_condition_snapshot(FIXTURE["condition_rules"],
        retrieved_at=datetime.fromisoformat(FIXTURE["conditions_read_at"]))
    known = {row["id"] for row in FIXTURE["condition_rules"]
             if (row.get("update_rules") or {}).get("consolidated")}
    excluded = {code for code in known
                if code != 12 and not snapshot.rules[code].updates_open_close}
    assert set(map(int, params["known_conditions"])) == known
    assert set(map(int, params["non_bar_conditions"])) == excluded
    # Updating an existing high/low is different from creating a missing bar.
    assert snapshot.classify((32,)) == (True, "aggregate_eligible")
    assert not snapshot.rules[32].updates_open_close
    assert FIXTURE["provider_response"]["bars"] == []
    assert FIXTURE["provider_response"]["trades"][0]["conditions"] == [32]
    assert "market_capture_trades" in str(statement) and "market_trade_ticks" in str(statement)


def test_l3_off_restores_original_query_and_parameters_exactly():
    statement, params = query(False)
    expected = """
        WITH gaps(lo, hi) AS (VALUES (:left_0, :right_0))
        SELECT lo, hi FROM gaps WHERE
        EXISTS (SELECT 1 FROM market_capture_trades t
                WHERE t.symbol=:symbol
                AND t.event_ts >= to_timestamp((lo+60000)/1000.0)
                AND t.event_ts < to_timestamp(hi/1000.0))
        OR EXISTS (SELECT 1 FROM market_trade_ticks t
                WHERE t.symbol=:symbol
                AND t.event_ts >= to_timestamp((lo+60000)/1000.0)
                AND t.event_ts < to_timestamp(hi/1000.0))
    """
    assert "".join(str(statement).split()) == "".join(expected.split())
    assert params == {"symbol": "WFF", "left_0": 1791572940000, "right_0": 1791573240000}

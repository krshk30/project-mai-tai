"""⛔⭐⭐ THE OPEN POSITIONS TABLE MUST SHOW ONE ROW PER BROKER LEG.

Operator 2026-09-10, looking at TNON on the v2 bot page: Order History showed BOTH legs (Schwab
qty 2 and the Webull fan-out qty 1) while Open Positions showed a SINGLE row.

Three defects compounded in `_build_bot_position_rows`:
  1. `virtual_positions` was keyed by SYMBOL alone. A v2 fan-out puts both legs under the same
     strategy_code, so both matched the filter and the second silently OVERWROTE the first.
  2. `account_positions` was additionally filtered to the bot's OWN account, excluding the Webull
     broker row outright.
  3. The render loop iterated symbols, so there was structurally nowhere to put a second leg.

⇒ A MISSING FAN-OUT LEG WAS INVISIBLE ON THIS PAGE BY CONSTRUCTION - the same account-collapsing
shape as F1, and the reason a one-sided entry went unnoticed. The operator's own words:
"I'm suspecting this is why we don't recognize that issue."
"""
from __future__ import annotations

from project_mai_tai.services.control_plane import _build_bot_position_rows


def _v2_bot(positions: list[dict]) -> dict:
    return {
        "strategy_code": "schwab_1m_v2",
        "account_name": "live:schwab_1m_v2",
        "runtime_kind": "schwab_1m_v2",
        "positions": positions,
    }


# The real TNON shape, 2026-09-10 07:48 ET.
_TNON_DATA = {
    "virtual_positions": [
        {"strategy_code": "schwab_1m_v2", "broker_account_name": "live:schwab_1m_v2",
         "symbol": "TNON", "quantity": 2, "average_price": 3.5582, "updated_at": "2026-09-10 07:48:07"},
        {"strategy_code": "schwab_1m_v2", "broker_account_name": "live:orb",
         "symbol": "TNON", "quantity": 1, "average_price": 3.56, "updated_at": "2026-09-10 07:48:10"},
    ],
    "account_positions": [
        {"broker_account_name": "live:schwab_1m_v2", "symbol": "TNON", "quantity": 2,
         "market_value": 7.14, "updated_at": "2026-09-10 07:49:40"},
        {"broker_account_name": "live:orb", "symbol": "TNON", "quantity": 1,
         "market_value": 3.57, "updated_at": "2026-09-10 07:49:40"},
    ],
}


def test_a_dual_broker_position_renders_BOTH_legs() -> None:
    """THE CONTROL THAT WOULD HAVE CAUGHT IT. Before the fix this rendered ONE row."""
    rendered = _build_bot_position_rows(_TNON_DATA, _v2_bot([
        {"ticker": "TNON", "quantity": 2, "entry_price": 3.5582,
         "current_price": 3.57, "entry_time": "2026-09-10T07:48:06-04:00"},
    ]))
    assert rendered.count("<tr") == 2, "one row per broker leg, not one row per symbol"
    assert "Schwab" in rendered and "Webull" in rendered
    # ⛔ `live:orb` is the WEBULL account the v2 fan-out routes through - NOT the ORB bot.
    assert "live:orb" not in rendered, "the raw account name reads as an ORB position"


def test_the_WEBULL_leg_shows_its_own_quantity_not_the_schwab_one() -> None:
    """The whole point: a 1-share Webull leg beside a 2-share Schwab leg. Collapsing them is what
    made a MISSING leg indistinguishable from a present one."""
    rendered = _build_bot_position_rows(_TNON_DATA, _v2_bot([
        {"ticker": "TNON", "quantity": 2, "entry_price": 3.5582,
         "current_price": 3.57, "entry_time": "2026-09-10T07:48:06-04:00"},
    ]))
    webull_row = [r for r in rendered.split("<tr") if "Webull" in r]
    assert len(webull_row) == 1
    assert ">1<" in webull_row[0].replace(" ", "") or "1<" in webull_row[0], "Webull leg must show qty 1"


def test_the_WEBULL_leg_carries_BROKER_TRUTH_not_just_our_own_book() -> None:
    """⛔ THE OTHER HALF OF THE FIX, and the one that matters for a phantom.

    Showing the leg from `virtual_positions` alone proves only that WE think we hold it. The
    account row is broker truth, and it was being filtered out because it belonged to a different
    account than the bot's. Without this assertion, re-adding that filter still renders a Webull
    row - just one with Broker Qty 0 - which reads as a position the broker has never confirmed.
    [[project_mai_tai_virtual_positions_false_zero]] is the same family pointed the other way.
    """
    rendered = _build_bot_position_rows(_TNON_DATA, _v2_bot([
        {"ticker": "TNON", "quantity": 2, "entry_price": 3.5582,
         "current_price": 3.57, "entry_time": "2026-09-10T07:48:06-04:00"},
    ]))
    webull_row = next(r for r in rendered.split("<tr") if "Webull" in r)
    # The broker-qty cell renders the account row's price ($3.57); a filtered-out account row
    # renders $0.00 there, so this fails the moment broker truth stops reaching the leg.
    assert "$3.57" in webull_row, "the Webull leg must show BROKER quantity and price, not $0.00"
    assert "$0.00" not in webull_row


def test_a_MISSING_webull_leg_is_now_VISIBLE_as_a_single_row() -> None:
    """⛔ THE OPERATOR'S ACTUAL PROBLEM. When the fan-out fires one-sided, the page must show ONE
    row - so the absence is legible - rather than one row that looks identical to a healthy pair."""
    schwab_only = {
        "virtual_positions": [_TNON_DATA["virtual_positions"][0]],
        "account_positions": [_TNON_DATA["account_positions"][0]],
    }
    rendered = _build_bot_position_rows(schwab_only, _v2_bot([
        {"ticker": "TNON", "quantity": 2, "entry_price": 3.5582,
         "current_price": 3.57, "entry_time": "2026-09-10T07:48:06-04:00"},
    ]))
    assert rendered.count("<tr") == 1
    assert "Webull" not in rendered, "a one-sided entry must READ as one-sided"


def test_a_single_account_bot_is_unchanged() -> None:
    """⛔ REGRESSION GUARD. Re-keying by (account, symbol) must not alter single-broker bots."""
    data = {
        "virtual_positions": [
            {"strategy_code": "polygon_30s", "broker_account_name": "paper:polygon_30s",
             "symbol": "ABCD", "quantity": 5, "average_price": 2.0, "updated_at": "2026-09-10 07:00:00"},
        ],
        "account_positions": [
            {"broker_account_name": "paper:polygon_30s", "symbol": "ABCD", "quantity": 5,
             "market_value": 10.5, "updated_at": "2026-09-10 07:00:01"},
        ],
    }
    bot = {"strategy_code": "polygon_30s", "account_name": "paper:polygon_30s",
           "runtime_kind": "paper_exit",
           "positions": [{"ticker": "ABCD", "quantity": 5, "entry_price": 2.0,
                          "current_price": 2.1, "entry_time": "2026-09-10T07:00:00-04:00"}]}
    rendered = _build_bot_position_rows(data, bot)
    assert rendered.count("<tr") == 1


def test_no_open_positions_still_renders_the_empty_state() -> None:
    rendered = _build_bot_position_rows({"virtual_positions": [], "account_positions": []},
                                        _v2_bot([]))
    assert "No open positions" in rendered

"""Held-book cursor controls through main(); synthetic local stubs, author-only."""

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,held", [
    ("complete", 0), ("complete", 2), ("complete", 10),
    ("partial", 2), ("failed", None),
])
async def test_sell_oversell_requires_complete_held_book_and_unknown_still_pages(
    monkeypatch, capsys, mode, held,
):
    from project_mai_tai.broker_adapters import schwab

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "alerts_b_heldbook", root / "ops/health/orphan_order_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    account, native_id = "live:schwab_1m_v2", "SYNTHETIC-SELL-1"
    pages, queries, wire = [], [], []
    order = {"orderId": native_id, "status": "WORKING", "orderType": "MARKET",
             "enteredTime": (datetime.now(UTC) - timedelta(minutes=30)).isoformat(),
             "orderLegCollection": [{"instruction": "SELL", "quantity": 8,
                                     "instrument": {"symbol": "SYNTHETIC"}}]}

    class Broker:
        def __init__(self, settings, accounts_by_name):
            self.accounts_by_name = accounts_by_name

        async def _authorized_request_json(self, method, path):
            wire.append((method, path))
            assert method == "GET"
            return 200, {}, [order]

        @staticmethod
        def _parse_datetime(value):
            return datetime.fromisoformat(value)

    class Result:
        def __init__(self, rows, *, partial=False):
            self.rows, self.partial = rows, partial

        def mappings(self):
            if not self.partial:
                return self.rows

            def incomplete():
                yield self.rows[0]
                raise RuntimeError("SYNTHETIC held cursor failed after first row")

            return incomplete()

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, values=None):
            query = str(sql)
            queries.append((query, values))
            if "SELECT name FROM broker_accounts" in query:
                return Result([{"name": account}])
            if "FROM broker_orders" in query:
                assert values == {"ids": [native_id], "accounts": [account]}
                return Result([{"bid": native_id, "status": "accepted", "failed_cancel_at": None}])
            if "FROM account_positions" in query:
                assert values == {"acct": account}
                if mode == "failed":
                    raise RuntimeError("SYNTHETIC held lookup unavailable")
                return Result([{"symbol": "SYNTHETIC", "qty": held}], partial=mode == "partial")
            raise AssertionError("unexpected query: " + query)

    monkeypatch.setattr(schwab, "SchwabBrokerAdapter", Broker)
    monkeypatch.setattr(schwab, "configured_schwab_accounts", lambda settings: {
        account: SimpleNamespace(account_hash="SYNTHETIC-PHYSICAL")})
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace(
        strategy_schwab_1m_v2_account_name=account, redis_url="SYNTHETIC"))
    monkeypatch.setattr(module, "build_session_factory", lambda settings: Session)
    monkeypatch.setattr(module, "bot_watchlist", lambda url: set())
    monkeypatch.setattr(module, "push", lambda *args: pages.append(args))
    monkeypatch.setattr(module.sys, "argv", ["orphan_order_check.py"])

    unknown = mode != "complete"
    risk = unknown or held < 8
    assert await module.main() == (2 if risk else 0)
    printed = capsys.readouterr().out
    assert len(wire) == 1 and len(queries) == 3
    assert not any("market_capture_quotes" in sql for sql, _ in queries)
    if not risk:
        assert not pages and "GREEN:" in printed
        assert "oversell" not in printed and "UNKNOWN" not in printed
        return
    assert len(pages) == 1 and pages[0][0].startswith("ORPHAN ORDER RED")
    assert pages[0][2:] == ("urgent", "rotating_light")
    for output in (printed, pages[0][1]):
        assert account in output and "SYNTHETIC" in output
        if unknown:
            assert native_id in output and "working_sell_qty=8" in output
            assert "Held inventory UNKNOWN" in output and "oversell check UNKNOWN" in output
            assert "oversell setup" not in output and "against 0 shares HELD" not in output
            assert "PHONE-ONLY EXCLUDED" not in output
        else:
            assert "oversell setup" in output and f"against {held} shares HELD" in output
            assert "Held inventory UNKNOWN" not in output

"""The retry horizon must outlive the settle window, and a refusal must be diagnosable.

⛔⭐ THE MEASUREMENT THAT DROVE THIS (live 2026-08-14, live:orb):

    12:30:13.503  MANAGED-OPEN CGTL entry=5.4100
    12:30:13.747  attempt 1/3 refused
    12:30:15.877  attempt 2/3 refused
    12:30:18.001  attempt 3/3 refused -> FAILED
    12:30:26.204  SETTLE-LAG: VISIBLE after 12.7s      <- the position appears HERE

The whole sequence was spent 8 seconds BEFORE the broker would admit the position existed. A
protective SELL for shares Webull cannot see is a naked short to it, so no price could have saved
it. One episode also logged `NEVER VISIBLE after 300s`.

⛔ THIS IS PLAUSIBLE, NOT PROVEN. In 4 of 6 bare-fill episodes attempts 2-3 fired AFTER
`SETTLE-LAG: VISIBLE` and were still refused, and position visibility
(`list_account_positions`) is not the same surface as order-side available-to-sell. If a wider
horizon lands and refusals persist, the settle window is exonerated and the instrumentation below
is what tells us the real reason.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from project_mai_tai.broker_adapters.webull import WebullBrokerAdapter
from project_mai_tai.oms import service as svc

RTH_NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET
POST_CLOSE_NOW = datetime(2026, 9, 1, 21, 0, tzinfo=UTC)  # Tuesday 17:00 ET


@pytest.fixture(autouse=True)
def _inject_rth_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry geometry tests own an RTH clock instead of inheriting the CI run hour."""
    monkeypatch.setattr(svc, "utcnow", lambda: RTH_NOW)


class _Adapter:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def submit_order(self, request):
        self.calls.append(request)
        return [SimpleNamespace(event_type="rejected", reason="scripted refusal")]


def _svc(adapter, **overrides):
    s = object.__new__(svc.OmsRiskService)
    cfg = dict(
        oms_v2_cw_target_pct=2.0, oms_v2_cw_hard_stop_pct=5.0,
        oms_webull_protect_attempts=5, oms_webull_protect_interval_seconds=2.0,
        oms_webull_protect_backoff_multiplier=2.0,
        oms_webull_protect_max_interval_seconds=15.0,
    )
    cfg.update(overrides)
    s.settings = SimpleNamespace(**cfg)
    s.logger = logging.getLogger("test-retry-horizon")
    s.broker_adapter = adapter
    s._webull_protect_base = {}

    async def _state(_a, _b):
        return svc._PositionRead.UNKNOWN

    async def _quote(*, broker_account_name, symbol):  # noqa: ARG001
        return {}

    s._broker_symbol_position_state = _state
    s._fetch_quote_for_order = _quote
    return s


def _run_capturing_sleeps(s) -> list[float]:
    """Run the attach with asyncio.sleep stubbed, returning the delays it asked for."""
    slept: list[float] = []
    real_sleep = asyncio.sleep

    async def _fake_sleep(secs, *a, **kw):
        slept.append(secs)
        return await real_sleep(0)

    async def _drive():
        asyncio.sleep = _fake_sleep
        try:
            await s._attach_webull_protection(
                broker_account_name="live:orb", symbol="TEST", quantity=1,
                entry_price=5.0, strategy_code="schwab_1m_v2")
        finally:
            asyncio.sleep = real_sleep

    asyncio.run(_drive())
    return slept


def test_the_retry_horizon_outlives_the_measured_settle_window() -> None:
    """⭐ THE LOAD-BEARING NUMBER. The old 3x2s schedule spanned 4.0s against a settle lag measured
    at 12.7s. Total span must now comfortably exceed it."""
    slept = _run_capturing_sleeps(_svc(_Adapter()))
    assert sum(slept) > 12.7, (
        f"total retry span {sum(slept)}s must outlast the 12.7s settle lag measured live"
    )


def _svc_on_CODE_DEFAULTS(adapter):
    """A settings object carrying NONE of the protect knobs, so every `getattr` fallback runs.

    ⛔⭐⭐ THIS IS THE CONFIGURATION THAT ACTUALLY RUNS. Verified on the box 2026-08-17:
    `/etc/project-mai-tai/project-mai-tai.env` sets no `WEBULL_PROTECT_*` override at all, so the
    CODE DEFAULTS are live. A fixture that passes attempts/interval explicitly is testing a
    configuration that exists nowhere — and it let a mutant reverting the default to the old
    3-attempts SURVIVE, because the fixture overrode the very thing under test.
    """
    s = object.__new__(svc.OmsRiskService)
    s.settings = SimpleNamespace(oms_v2_cw_target_pct=2.0, oms_v2_cw_hard_stop_pct=5.0)
    s.logger = logging.getLogger("test-retry-horizon")
    s.broker_adapter = adapter
    s._webull_protect_base = {}

    async def _state(_a, _b):
        return svc._PositionRead.UNKNOWN

    async def _quote(*, broker_account_name, symbol):  # noqa: ARG001
        return {}

    s._broker_symbol_position_state = _state
    s._fetch_quote_for_order = _quote
    return s


def test_THE_SHIPPED_DEFAULTS_outlive_the_settle_window() -> None:
    """⭐ The one that pins production. No env override exists, so these defaults are what a live
    Webull fill will actually get."""
    a = _Adapter()
    slept = _run_capturing_sleeps(_svc_on_CODE_DEFAULTS(a))
    assert len(a.calls) >= 5, f"default attempts too few: {len(a.calls)}"
    assert sum(slept) > 12.7, (
        f"DEFAULT retry span {sum(slept)}s must outlast the 12.7s settle lag measured live — "
        "the old 3x2s default spanned only 4.0s"
    )


def test_the_delays_back_off_rather_than_staying_flat() -> None:
    slept = _run_capturing_sleeps(_svc(_Adapter()))
    assert slept == [2.0, 4.0, 8.0, 15.0], slept
    assert slept == sorted(slept), "each wait must be at least as long as the last"


def test_the_backoff_is_CAPPED_so_it_cannot_run_away() -> None:
    """Without a cap, more attempts would push the tail into minutes and the task would outlive
    the position it is protecting."""
    slept = _run_capturing_sleeps(
        _svc(_Adapter(), oms_webull_protect_attempts=8, oms_webull_protect_max_interval_seconds=15.0)
    )
    assert max(slept) == 15.0
    assert all(d <= 15.0 for d in slept)


def test_attempt_ONE_is_still_immediate() -> None:
    """⛔ Settle is usually 0.3-0.7s. Delaying every attach to survive the rare slow case would
    leave the common case unprotected for longer, which is the wrong trade."""
    a = _Adapter()
    slept = _run_capturing_sleeps(_svc(a))
    # 5 attempts, 4 gaps -> the first submit happened before any sleep at all.
    assert len(a.calls) == 5
    assert len(slept) == 4


def test_it_still_gives_up_rather_than_retrying_forever() -> None:
    a = _Adapter()
    _run_capturing_sleeps(_svc(a))
    assert len(a.calls) == 5, "the attempt cap must still bound the loop"


def test_post_close_gate_still_refuses_the_attach(monkeypatch: pytest.MonkeyPatch) -> None:
    """Opposite polarity: clock injection must not bypass the production RTH gate."""
    monkeypatch.setattr(svc, "utcnow", lambda: POST_CLOSE_NOW)
    adapter = _Adapter()

    slept = _run_capturing_sleeps(_svc(adapter))

    assert adapter.calls == []
    assert slept == []


def test_the_broker_reason_is_NOT_truncated_to_200(caplog: pytest.LogCaptureFixture) -> None:
    """⛔ At 200 chars the live reject read `...should be lower than the cu` — cut off exactly where
    it became useful. That truncation is how the error CODE got glossed as its own opposite."""
    long_reason = (
        "Webull order rejected: OAUTH_OPENAPI_TRADE_STOP_LOSS_PRICE_LT_MARKETPRICE "
        "The stop price of the stop-loss order should be lower than the current market price. "
        + "x" * 150 + " TAIL_MARKER (http 417)"
    )

    class _Long(_Adapter):
        async def submit_order(self, request):
            self.calls.append(request)
            return [SimpleNamespace(event_type="rejected", reason=long_reason)]

    s = _svc(_Long(), oms_webull_protect_attempts=1)
    with caplog.at_level(logging.WARNING):
        _run_capturing_sleeps(s)
    assert "should be lower than the current market price" in caplog.text
    assert "TAIL_MARKER" in caplog.text, "the tail of the broker's message must survive"


# ------------------------------------------------------------------ adapter-side instrumentation
def test_a_refused_pair_logs_WHAT_WE_SENT_and_WHAT_CAME_BACK(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """⛔⭐ The whole point: one refused episode should settle the cause, not start another week of
    inference. Three hypotheses were argued and killed without this."""
    adapter = object.__new__(WebullBrokerAdapter)
    request = SimpleNamespace(
        client_order_id="coid-1", symbol="TEST", side="sell", quantity=Decimal("2"),
        broker_account_name="live:orb", time_in_force="day",
        metadata={"bracket_target_price": "5.10", "bracket_stop_price": "4.75",
                  "webull_exit_only_pair": "true"},
    )
    exc = RuntimeError("OAUTH_OPENAPI_TRADE_STOP_LOSS_PRICE_LT_MARKETPRICE (http 417)")
    with caplog.at_level(logging.WARNING):
        adapter._log_exit_pair_refusal(request, exc)

    assert "[WEBULL-EXIT-PAIR-REFUSED]" in caplog.text
    # the payload we sent, in full
    assert "STOP_PROFIT" in caplog.text and "STOP_LOSS" in caplog.text
    assert "4.75" in caplog.text and "5.10" in caplog.text
    # and the broker's own words
    assert "STOP_LOSS_PRICE_LT_MARKETPRICE" in caplog.text


def test_the_diagnostic_can_NEVER_break_the_order_path(caplog: pytest.LogCaptureFixture) -> None:
    """⛔ A payload we cannot rebuild must still produce a log line, not an exception. Diagnostics
    are never allowed to be load-bearing on a live order path."""
    adapter = object.__new__(WebullBrokerAdapter)
    broken = SimpleNamespace(
        client_order_id="coid-1", symbol="TEST", side="sell", quantity=Decimal("2"),
        broker_account_name="live:orb", time_in_force="day",
        metadata={},  # missing both prices -> the builder raises
    )
    with caplog.at_level(logging.WARNING):
        adapter._log_exit_pair_refusal(broken, RuntimeError("boom"))
    assert "[WEBULL-EXIT-PAIR-REFUSED]" in caplog.text
    assert "could not be rebuilt" in caplog.text


def test_the_logged_payload_is_the_REAL_builders_output_not_a_copy() -> None:
    """⛔ A hand-written approximation in the log would be worse than nothing — it would send the
    next investigation after a payload we never actually sent."""
    import inspect
    src = inspect.getsource(WebullBrokerAdapter._log_exit_pair_refusal)
    assert "_build_exit_only_pair_payload(request)" in src

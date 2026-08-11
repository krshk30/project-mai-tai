"""NAME THE SUPPRESSION — an intent that dies AFTER risk PASSES must say so on the tape.

⛔⭐ THE DEFECT (open thread 4). Two branches drop an `open` intent after `_evaluate_risk` returned
PASS: the Schwab and Webull `*_ineligible_cached` short-circuits. Each marks the intent rejected and
publishes a rejected order event, and **neither logged anything**. From the log, "a gate ate this
intent" and "the intent never arrived" were the same observation.

The behaviour is currently PROTECTIVE — it saves a doomed broker round-trip on a name the broker
already refused today. But it is the same SHAPE as #580 (resting-order orphan latch) and #608 (the
close-retry sawtooth), both of which cost real money while invisible. And a missing reason is only
half the cost: it stops the investigation before it starts.
[[feedback_a_wrong_reason_is_worse_than_a_missing_one]]

⛔⭐ THE BRANCHES MUST STAY SYMMETRIC. One broker instrumented and the other silent produces a reject
query that reads CLEAN on the blind side *by construction* —
[[feedback_reject_query_states_account_visibility]]. Both are pinned here, in one file.

⚠️ WHAT THIS FILE CANNOT SEE, stated rather than implied. The Schwab branch is proved
BEHAVIOURALLY (the service really runs, the line is really captured). The Webull branch has no
end-to-end fixture in this suite yet — no `FakeRejectWebullIneligible…` adapter exists — so it is
pinned STRUCTURALLY. A structural assertion cannot prove the branch is reachable at runtime; it can
only prove the line is present and symmetric. Building the Webull harness is the follow-up that
would upgrade it. [[feedback_a_watch_that_fails_to_a_false_clean]]
"""
from __future__ import annotations

import inspect
from decimal import Decimal

import pytest

from project_mai_tai.events import TradeIntentEvent, TradeIntentPayload
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

from test_oms_risk_service import (  # noqa: E402 — same-dir test module, rootdir on sys.path
    FakeRedis,
    FakeRejectSchwabIneligibleBrokerAdapter,
    build_test_session_factory,
)

MARKER = "[OMS-INTENT-DROPPED]"

# Verbatim from /etc/project-mai-tai/project-mai-tai.env, read 2026-08-10.
PRODUCTION_ENV = {
    "strategy_schwab_1m_v2_dual_broker_fanout_enabled": True,
    "oms_hold_marketable_managed_exit": True,
    "oms_broker_sync_interval_seconds": 15,
    "strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled": True,
}


class _CapturingLogger:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def info(self, msg, *args) -> None:
        self.lines.append(msg % args if args else msg)

    warning = exception = debug = error = info


def _open_intent(symbol: str = "AEHL") -> TradeIntentEvent:
    return TradeIntentEvent(
        source_service="strategy-engine",
        payload=TradeIntentPayload(
            strategy_code="macd_30s",
            broker_account_name="paper:macd_30s",
            symbol=symbol,
            side="buy",
            quantity=Decimal("10"),
            intent_type="open",
            reason="ENTRY_P1_MACD_CROSS",
            metadata={},
        ),
    )


def test_production_flag_values_are_what_these_fixtures_assume() -> None:
    """⛔⭐⭐ THE GUARD ON EVERY OTHER TEST HERE.

    Three settings DIFFER between code default and production:
      dual_broker_fanout_enabled          False  -> true
      cw_v2_eh_resting_entry_enabled      False  -> true
      oms_broker_sync_interval_seconds        5  -> 15

    A bare `Settings()` fixture would make any Webull-branch assertion pass BY NEVER RUNNING THE
    BRANCH — the exact failure in [[feedback_fixture_must_match_production_config]], where
    `oms_v2_exit_management_enabled` defaulted False and 'mandatory' tests passed by never
    executing. If a default is flipped underneath these tests, this fails FIRST and loudly."""
    defaults = Settings()
    assert defaults.strategy_schwab_1m_v2_dual_broker_fanout_enabled is False, (
        "code default changed; re-check every fan-out fixture before trusting it"
    )
    assert defaults.strategy_schwab_1m_v2_cw_v2_eh_resting_entry_enabled is False
    assert defaults.oms_broker_sync_interval_seconds == 5, (
        "production runs 15; the P0a-unreachability finding is stated against 15, so re-read the "
        "env before requoting it"
    )
    # This one AGREES with production, and that agreement is load-bearing for the census tests.
    assert defaults.oms_hold_marketable_managed_exit is True

    prod = Settings(**PRODUCTION_ENV)
    assert prod.strategy_schwab_1m_v2_dual_broker_fanout_enabled is True
    assert prod.oms_broker_sync_interval_seconds == 15


@pytest.mark.asyncio
async def test_schwab_ineligible_drop_emits_a_named_line_at_runtime() -> None:
    """BEHAVIOURAL. Drive the real service: first intent gets refused by the broker and caches the
    symbol; the SECOND is dropped by the cache short-circuit and must name itself."""
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
            **PRODUCTION_ENV,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=FakeRejectSchwabIneligibleBrokerAdapter(),
    )
    service.logger = _CapturingLogger()

    await service.process_trade_intent(_open_intent())
    dropped = await service.process_trade_intent(_open_intent())

    # The pre-existing contract still holds — this change is additive.
    assert dropped[0].payload.status == "rejected"
    assert dropped[0].payload.reason == "schwab_ineligible_cached"

    hits = [ln for ln in service.logger.lines if MARKER in ln]
    assert len(hits) == 1, f"expected exactly one {MARKER} line, got {service.logger.lines!r}"
    assert "schwab_ineligible_cached" in hits[0]
    assert "AEHL" in hits[0]
    # ⛔ The non-obvious half: 'rejected' here does NOT mean risk refused it. Risk PASSED and a
    # LATER gate dropped it. A reader who assumes the reject came from risk looks in the wrong
    # place entirely, so the line must say so in words.
    assert "risk PASSED" in hits[0]
    assert "no broker order created" in hits[0]


@pytest.mark.asyncio
async def test_the_first_broker_refusal_is_not_counted_as_a_dropped_intent() -> None:
    """⛔ MUTATION GUARD, the false-positive direction. The FIRST intent really did reach the
    broker and was refused there — it is a broker reject, not a suppressed intent. If the marker
    ever fires on that path the tape would over-report drops, and an inflated count is as wrong as
    a missing one."""
    service = OmsRiskService(
        settings=Settings(
            redis_stream_prefix="test",
            oms_adapter="simulated",
            strategy_macd_30s_broker_provider="schwab",
            **PRODUCTION_ENV,
        ),
        redis_client=FakeRedis(),
        session_factory=build_test_session_factory(),
        broker_adapter=FakeRejectSchwabIneligibleBrokerAdapter(),
    )
    service.logger = _CapturingLogger()

    first = await service.process_trade_intent(_open_intent())

    assert first[0].payload.status == "rejected"
    assert "placed with a broker" in (first[0].payload.reason or "")
    assert [ln for ln in service.logger.lines if MARKER in ln] == [], (
        "a broker-side refusal must NOT be logged as a dropped intent"
    )


def test_both_broker_branches_are_instrumented_symmetrically() -> None:
    """⭐ STRUCTURAL (see the file docstring for why the Webull half cannot be behavioural yet).

    Assert on the PAIR, not on either alone: counting both in ONE assertion is what makes
    'someone instrumented Schwab and forgot Webull' a RED test rather than two independently
    green ones."""
    src = inspect.getsource(OmsRiskService.process_trade_intent)
    assert src.count(MARKER) == 2, (
        f"expected exactly 2 {MARKER} lines (schwab + webull ineligible-cached); "
        f"found {src.count(MARKER)}"
    )
    webull_branch = src.split('reason="webull_ineligible_cached"', 1)[1].split(
        "return [order_event]", 1
    )[0]
    assert MARKER in webull_branch, "the Webull ineligible-cached drop must emit the same marker"
    assert "risk PASSED" in webull_branch

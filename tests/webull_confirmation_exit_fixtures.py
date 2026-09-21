"""Reusable Webull confirmation-exit reports captured on 2026-09-18 and 2026-09-21.

The error codes, messages, HTTP statuses, request ids, and report types below come from the
retained OMS tape. Account ids and request signatures are intentionally not test fixtures.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from project_mai_tai.broker_adapters.protocols import ExecutionReport, ExitPairReleaseResult


GLND_20260921_PROTECT_BASE = "schwab_1m_v2-GLND-protect-3ab3adb5de99"
GRML_20260921_PROTECT_BASE = "schwab_1m_v2-GRML-protect-06f479506bb0"
NCPL_20260921_PROTECT_BASE = "schwab_1m_v2-NCPL-protect-f2c25c5bc86a"
NCPL_20260921_REPROTECT_BASE = "schwab_1m_v2-NCPL-protect-8eb18c3df517"


def _at(report: ExecutionReport, when: datetime) -> ExecutionReport:
    return replace(report, reported_at=when)


def _detail_rate_limited(
    base: str,
    suffix: str,
    *,
    symbol: str,
    request_id: str,
    reported_at: datetime,
) -> ExecutionReport:
    return ExecutionReport(
        event_type="accepted",
        origin="unknown",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason=(
            "cancel requested but confirmation could not be read: "
            "TOO_MANY_REQUESTS Too many requests (http 429)"
        ),
        metadata={
            "webull_action_name": "/trade/order/detail",
            "webull_method": "GET",
            "webull_request_id": request_id,
            "webull_error_code": "TOO_MANY_REQUESTS",
            "webull_error_message": "Too many requests",
            "webull_http_status": "429",
            "cancel_outcome": "could_not_tell",
        },
        reported_at=reported_at,
    )


def _cancel_refused(
    base: str,
    suffix: str,
    *,
    symbol: str,
    request_id: str,
    reported_at: datetime,
) -> ExecutionReport:
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason=(
            "Webull order rejected: ORDER_CAN_NOT_BE_CANCEL ORDER_CAN_NOT_BE_CANCEL (http 417)"
        ),
        metadata={
            "webull_action_name": "/trade/order/cancel",
            "webull_method": "POST",
            "webull_request_id": request_id,
            "webull_error_code": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_error_message": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_http_status": "417",
            "cancel_outcome": "not_confirmed",
        },
        reported_at=reported_at,
    )


def glnd_141807_released_pair() -> ExitPairReleaseResult:
    """GLND 2026-09-21: both legs were terminal on the first release attempt.

    The OMS retained one aggregate completion timestamp, not separate successful response
    timestamps for the two child reads. Both reports therefore carry that marker time.
    """
    reported_at = datetime(2026, 9, 21, 14, 18, 7, 953000, tzinfo=UTC)
    reports = tuple(
        _at(cancelled_leg(GLND_20260921_PROTECT_BASE, suffix, symbol="GLND"), reported_at)
        for suffix in ("T", "S")
    )
    return ExitPairReleaseResult(outcome="released", reports=reports)


def grml_143406_released_pair() -> ExitPairReleaseResult:
    """GRML 2026-09-21: both legs were terminal on the first release attempt."""
    reported_at = datetime(2026, 9, 21, 14, 34, 6, 581000, tzinfo=UTC)
    reports = tuple(
        _at(cancelled_leg(GRML_20260921_PROTECT_BASE, suffix, symbol="GRML"), reported_at)
        for suffix in ("T", "S")
    )
    return ExitPairReleaseResult(outcome="released", reports=reports)


def ncpl_143211_background_detail_rate_limit() -> ExecutionReport:
    """The fifth NCPL detail 429, observed before the three release attempts began."""
    return _detail_rate_limited(
        NCPL_20260921_PROTECT_BASE,
        "S",
        symbol="NCPL",
        request_id="d14afc4b-9277-4af0-92a1-018891731f2e",
        reported_at=datetime(2026, 9, 21, 14, 32, 11, 649000, tzinfo=UTC),
    )


def ncpl_1432_cancel_refusals() -> tuple[ExecutionReport, ...]:
    """The four NCPL cancel requests that Webull refused during attempts two and three."""
    return (
        _cancel_refused(
            NCPL_20260921_PROTECT_BASE,
            "T",
            symbol="NCPL",
            request_id="a2926174-9049-4105-8295-b620cae58307",
            reported_at=datetime(2026, 9, 21, 14, 32, 14, 250000, tzinfo=UTC),
        ),
        _cancel_refused(
            NCPL_20260921_PROTECT_BASE,
            "S",
            symbol="NCPL",
            request_id="84d157ff-8de1-4fdc-a20b-99f1e2279ab8",
            reported_at=datetime(2026, 9, 21, 14, 32, 14, 337000, tzinfo=UTC),
        ),
        _cancel_refused(
            NCPL_20260921_PROTECT_BASE,
            "T",
            symbol="NCPL",
            request_id="9e2cdbf2-0a44-41c2-813c-dcc73574863b",
            reported_at=datetime(2026, 9, 21, 14, 32, 15, 614000, tzinfo=UTC),
        ),
        _cancel_refused(
            NCPL_20260921_PROTECT_BASE,
            "S",
            symbol="NCPL",
            request_id="513b40b0-7c86-43ab-9aa2-511f6ed6f93c",
            reported_at=datetime(2026, 9, 21, 14, 32, 15, 713000, tzinfo=UTC),
        ),
    )


def ncpl_1432_release_attempts() -> tuple[ExitPairReleaseResult, ...]:
    """The three NCPL post-cancel read results consumed by the release retry loop."""
    attempt_one_at = datetime(2026, 9, 21, 14, 32, 13, 674000, tzinfo=UTC)
    attempt_two_at = datetime(2026, 9, 21, 14, 32, 14, 493000, tzinfo=UTC)
    return (
        ExitPairReleaseResult(
            outcome="unanswerable",
            reports=(
                _at(
                    cancelled_leg(NCPL_20260921_PROTECT_BASE, "T", symbol="NCPL"),
                    attempt_one_at,
                ),
                _detail_rate_limited(
                    NCPL_20260921_PROTECT_BASE,
                    "S",
                    symbol="NCPL",
                    request_id="8b889328-d2c7-46ef-9650-df23dc4f2e81",
                    reported_at=datetime(2026, 9, 21, 14, 32, 13, 673000, tzinfo=UTC),
                ),
            ),
        ),
        ExitPairReleaseResult(
            outcome="unanswerable",
            reports=(
                _at(
                    cancelled_leg(NCPL_20260921_PROTECT_BASE, "T", symbol="NCPL"),
                    attempt_two_at,
                ),
                _detail_rate_limited(
                    NCPL_20260921_PROTECT_BASE,
                    "S",
                    symbol="NCPL",
                    request_id="1ea26bca-88ab-4b05-86af-14f64f57d897",
                    reported_at=datetime(2026, 9, 21, 14, 32, 14, 492000, tzinfo=UTC),
                ),
            ),
        ),
        ExitPairReleaseResult(
            outcome="unanswerable",
            reports=(
                _detail_rate_limited(
                    NCPL_20260921_PROTECT_BASE,
                    "T",
                    symbol="NCPL",
                    request_id="9ba2f6a2-43e4-4d93-8edd-d277117afbc8",
                    reported_at=datetime(2026, 9, 21, 14, 32, 15, 771000, tzinfo=UTC),
                ),
                _detail_rate_limited(
                    NCPL_20260921_PROTECT_BASE,
                    "S",
                    symbol="NCPL",
                    request_id="97b632ae-4513-427f-b351-9291aeb460a0",
                    reported_at=datetime(2026, 9, 21, 14, 32, 15, 843000, tzinfo=UTC),
                ),
            ),
        ),
    )


def ncpl_143216_accepted_replacement_pair() -> ExecutionReport:
    """The replacement full pair Webull accepted after NCPL's unreadable release."""
    return ExecutionReport(
        event_type="accepted",
        origin="broker",
        client_order_id=NCPL_20260921_REPROTECT_BASE,
        broker_order_id="D9J6RD8CGUM8EU1HTT4OHDDIS9",
        symbol="NCPL",
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        reason="webull exit-only protective pair",
        metadata={
            "bracket_target_price": "1.2810",
            "bracket_stop_price": "1.1224",
            "webull_exit_only_pair": "true",
            "client_combo_order_id": NCPL_20260921_REPROTECT_BASE,
        },
        reported_at=datetime(2026, 9, 21, 14, 32, 16, 301000, tzinfo=UTC),
    )


def cancelled_leg(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    return ExecutionReport(
        event_type="cancelled",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="exit leg is terminal at broker; status=cancelled",
        metadata={"cancel_outcome": "confirmed"},
    )


def gipr_170707_order_cannot_cancel(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    request_ids = {
        "T": "72b41918-840a-4883-bb0a-7aca4acd4200",
        "S": "1235cf4f-b803-4442-afbd-2956573a73d8",
    }
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="Webull order rejected: ORDER_CAN_NOT_BE_CANCEL ORDER_CAN_NOT_BE_CANCEL (http 417)",
        metadata={
            "webull_request_id": request_ids[suffix],
            "webull_error_code": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_error_message": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_http_status": "417",
            "cancel_outcome": "not_confirmed",
        },
    )


def gipr_170707_detail_rate_limited(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    request_ids = {
        "T": "445b3434-e28d-4126-8168-65061c689305",
        "S": "d138ccf7-7ac6-4159-ae17-3c5937e6973d",
    }
    return ExecutionReport(
        event_type="accepted",
        origin="unknown",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="cancel requested but confirmation could not be read: TOO_MANY_REQUESTS Too many requests (http 429)",
        metadata={
            "webull_request_id": request_ids[suffix],
            "webull_error_code": "TOO_MANY_REQUESTS",
            "webull_error_message": "Too many requests",
            "webull_http_status": "429",
            "cancel_outcome": "could_not_tell",
        },
    )


def gipr_180510_order_cannot_cancel(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    request_ids = {
        "T": "8f3bbc32-1595-43b1-aa1d-00c5c8487e93",
        "S": "b048007b-6e1a-423a-aa79-604326ec196b",
    }
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="Webull order rejected: ORDER_CAN_NOT_BE_CANCEL ORDER_CAN_NOT_BE_CANCEL (http 417)",
        metadata={
            "webull_request_id": request_ids[suffix],
            "webull_error_code": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_error_message": "ORDER_CAN_NOT_BE_CANCEL",
            "webull_http_status": "417",
            "cancel_outcome": "not_confirmed",
        },
    )


def gipr_180511_detail_rate_limited(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    request_ids = {
        "T": "7fd55ee9-d30f-483d-b205-ad90f2eba468",
        "S": "3d1be6c2-6e2f-423a-bccf-79ee6347ab3b",
    }
    return ExecutionReport(
        event_type="accepted",
        origin="unknown",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="cancel requested but confirmation could not be read: TOO_MANY_REQUESTS Too many requests (http 429)",
        metadata={
            "webull_request_id": request_ids[suffix],
            "webull_error_code": "TOO_MANY_REQUESTS",
            "webull_error_message": "Too many requests",
            "webull_http_status": "429",
            "cancel_outcome": "could_not_tell",
        },
    )


def imcc_174604_cancelled_and_rate_limited(
    base: str, *, symbol: str
) -> tuple[ExecutionReport, ExecutionReport]:
    return (
        cancelled_leg(base, "T", symbol=symbol),
        ExecutionReport(
            event_type="accepted",
            origin="unknown",
            client_order_id=f"{base}S",
            symbol=symbol,
            side="sell",
            intent_type="cancel",
            reason="cancel requested but confirmation could not be read: TOO_MANY_REQUESTS Too many requests (http 429)",
            metadata={
                "webull_request_id": "0d97eac5-7b15-4906-808b-e0783494c234",
                "webull_error_code": "TOO_MANY_REQUESTS",
                "webull_error_message": "Too many requests",
                "webull_http_status": "429",
                "cancel_outcome": "could_not_tell",
            },
        ),
    )


def working_leg_after_cancel_request(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    return ExecutionReport(
        event_type="accepted",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="cancel requested but not confirmed; broker status=accepted",
        metadata={"cancel_outcome": "not_confirmed"},
    )


def filled_leg(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    return ExecutionReport(
        event_type="filled",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        broker_order_id=f"broker-{suffix}",
        broker_fill_id=f"broker-{suffix}:1",
        symbol=symbol,
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        filled_quantity=Decimal("1"),
        fill_price=Decimal("5.0201"),
        reason="exit pair resolved by broker fill during release",
        metadata={"cancel_outcome": "resolved_by_fill"},
        reported_at=datetime(2026, 9, 18, 18, 21, 35, 527000, tzinfo=UTC),
    )


def unknown_answer(base: str, suffix: str, *, symbol: str) -> ExecutionReport:
    """Synthetic future broker rule; the made-up code is deliberate for the default-path test."""
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id=f"{base}{suffix}",
        symbol=symbol,
        side="sell",
        intent_type="cancel",
        reason="Webull order rejected: FUTURE_RULE_CHANGED (http 499)",
        metadata={
            "webull_error_code": "FUTURE_RULE_CHANGED",
            "webull_error_message": "Future broker rule changed",
            "webull_http_status": "499",
            "cancel_outcome": "could_not_tell",
        },
    )


def ztg_195912_no_position_reject() -> ExecutionReport:
    """Class A, live:orb ZTG 2026-09-16 19:59:12.612490Z."""
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id="schwab_1m_v2-ZTG-close-bb6c80a52178",
        symbol="ZTG",
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        reason=(
            "Webull order rejected: "
            "NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K "
            "NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K (http 417)"
        ),
        metadata={
            "webull_request_id": "79a944e5-2ebf-48b7-a026-57f6e5638f30",
            "webull_error_code": ("NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K"),
            "webull_error_message": ("NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K"),
            "webull_http_status": "417",
        },
        reported_at=datetime(2026, 9, 16, 19, 59, 12, 612490, tzinfo=UTC),
    )


def bq_170729_not_tradable_reject() -> ExecutionReport:
    """Class C, live:orb BQ 2026-08-12 17:07:29.407767Z."""
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id="schwab_1m_v2-BQ-close-00458612a3a2",
        symbol="BQ",
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        reason=(
            "Webull order rejected: TICKER_ID_CAN_NOT_TRADE TICKER_ID_CAN_NOT_TRADE (http 417)"
        ),
        metadata={
            "webull_error_code": "TICKER_ID_CAN_NOT_TRADE",
            "webull_error_message": "TICKER_ID_CAN_NOT_TRADE",
            "webull_http_status": "417",
        },
        reported_at=datetime(2026, 8, 12, 17, 7, 29, 407767, tzinfo=UTC),
    )


def lgps_133416_malformed_client_order_id_reject() -> ExecutionReport:
    """Class D, live:orb LGPS 2026-07-13 13:34:16.599906Z."""
    return ExecutionReport(
        event_type="rejected",
        origin="broker",
        client_order_id="orb-LGPS-close-e49f4a08e6f2-r675c2064-r33441ac0",
        symbol="LGPS",
        side="sell",
        intent_type="close",
        quantity=Decimal("1"),
        reason=(
            "Webull order rejected: ILLEGAL_PARAMETER "
            "client_order_id value length between 1 and 40 (http 417)"
        ),
        metadata={
            "webull_error_code": "ILLEGAL_PARAMETER",
            "webull_error_message": "client_order_id value length between 1 and 40",
            "webull_http_status": "417",
        },
        reported_at=datetime(2026, 7, 13, 13, 34, 16, 599906, tzinfo=UTC),
    )

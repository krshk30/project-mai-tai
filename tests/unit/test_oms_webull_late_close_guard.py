from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import logging
from types import SimpleNamespace

from project_mai_tai.oms.service import OmsRiskService, _EXIT_FETCH_FAILED
from project_mai_tai.settings import Settings


ACCT = "live:orb"
SYMBOL = "VEEA"
KEY = (ACCT, SYMBOL)
NO_POSITION = "NEW_NO_POSITION_MARGIN_ACCOUNT_CAN_NOT_SELL_SHORT_FOR_LT_2K"


class _Session:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self) -> None:
        pass


class _Payload:
    def __init__(self, status: str, reason: str = "") -> None:
        self.status = status
        self.reason = reason


class _Event:
    def __init__(self, status: str, reason: str = "") -> None:
        self.payload = _Payload(status, reason)


class _Position:
    quantity = 2


def _service(*, enabled: bool, detail=None, released: bool = False):
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = SimpleNamespace(
        oms_v2_webull_late_close_guard_enabled=enabled,
        provider_for_account=lambda account: "webull" if account == ACCT else "schwab",
    )
    service.logger = logging.getLogger("test-webull-late-close")
    service._v2_exit_close_failures = {}
    service._v2_exit_reject_total = {}
    service._v2_exit_stood_down = set()
    service._v2_exit_reject_alarm_count = {}
    service._v2_exit_reject_alarm_announced = set()
    service._managed_v2_symbols = {KEY}
    service._cw_flip_pending = set()
    service._cw_floor_armed = set()
    service._oco_exit_fetch_deferrals = {}
    service._exit_reservation_released = {KEY} if released else set()
    service._webull_late_close_oco_probed = set()
    service._webull_late_close_retry_after = {}
    service._webull_late_close_logged_until = {}
    service.session_factory = _Session
    service.row = SimpleNamespace(
        id="row-1",
        broker_account_name=ACCT,
        symbol=SYMBOL,
        current_quantity=2,
        entry_time=datetime(2026, 9, 15, 17, 35, tzinfo=UTC),
    )
    service.submits = 0
    service.fetches = 0
    service.resolved = []

    class _Store:
        def get_open_managed_position(self, *_args, **_kwargs):
            return service.row

        def update_managed_position_from_position(self, *_args, **_kwargs) -> None:
            pass

    service.store = _Store()

    async def _sell(*_args, **_kwargs):
        service.submits += 1
        return [_Event("rejected", NO_POSITION)]

    async def _reconcile(*_args, **_kwargs):
        return False

    async def _fetch(*_args, **_kwargs):
        service.fetches += 1
        return detail

    async def _resolved(account, symbol, *, detail=None, expected_row_id=None):
        service.resolved.append((account, symbol, detail, expected_row_id))
        return True

    service._emit_v2_managed_sell = _sell
    service._v2_close_reconcile_flat = _reconcile
    service._find_oco_entry_order = lambda *_args, **_kwargs: SimpleNamespace(
        broker_order_id="entry-order", quantity=2
    )
    service._oco_exit_base_for_entry = lambda *_args, **_kwargs: "entry-base"
    service._fetch_oco_exit_detail = _fetch
    service._close_resolved_oco_managed_row = _resolved
    service._a2_should_defer = lambda *_args, **_kwargs: False
    service._a2_enabled_for = lambda *_args, **_kwargs: False
    service._a2_note_reject = lambda *_args, **_kwargs: None
    service._a2_maybe_escalate = _noop
    service._a2_clear = lambda *_args, **_kwargs: None
    service._clear_exit_reservation_release = lambda *_args, **_kwargs: None
    service._note_v2_exit_reject_alarm = lambda **_kwargs: None
    service._publish_order_event = _noop
    return service


async def _noop(*_args, **_kwargs):
    return None


def _drive(service) -> str:
    return asyncio.run(
        service._emit_v2_exit_on_loop(
            ACCT,
            SYMBOL,
            _Position(),
            6.13,
            kind="HARD",
            reference_price=5.63,
            reason="oms_v2_managed_exit:CW_HARD_STOP",
            bid=5.62,
            close_on_fill=True,
        )
    )


def test_flag_defaults_false_and_flag_off_preserves_the_per_quote_retry_path() -> None:
    assert Settings().oms_v2_webull_late_close_guard_enabled is False
    service = _service(enabled=False, detail=_EXIT_FETCH_FAILED)

    assert _drive(service) == "refused"
    assert _drive(service) == "refused"

    assert service.submits == 2
    assert service.fetches == 0
    assert service._webull_late_close_retry_after == {}


def test_classifier_matches_screaming_snake_and_despaced_without_becoming_a2() -> None:
    assert OmsRiskService._is_webull_no_position_close_reject(NO_POSITION)
    assert OmsRiskService._is_webull_no_position_close_reject(
        "newnopositionmarginaccountcannotsellshortforlt2k"
    )
    assert not OmsRiskService._is_webull_no_position_close_reject(
        "ORDER_NOT_SUPPORT_REVERSE_OPTION"
    )
    assert not OmsRiskService._is_exit_refused_not_sellable(NO_POSITION)


def test_scope_uses_the_configured_provider_not_an_account_name_literal() -> None:
    service = OmsRiskService.__new__(OmsRiskService)
    service.settings = SimpleNamespace(
        oms_v2_webull_late_close_guard_enabled=True,
        provider_for_account=lambda account: "webull" if account == "custom:webull" else "schwab",
    )
    assert service._webull_late_close_enabled_for("custom:webull")
    assert not service._webull_late_close_enabled_for(ACCT)


def test_first_reject_with_covering_oco_fill_uses_existing_closure_and_stops_at_one() -> None:
    detail = {
        "quantity": 2,
        "price": "5.63",
        "filled_at": datetime(2026, 9, 15, 17, 49, 20, tzinfo=UTC),
        "broker_order_id": "oco-stop",
    }
    service = _service(enabled=True, detail=detail)

    assert _drive(service) == "closed"

    assert service.submits == 1
    assert service.fetches == 1
    assert service.resolved == [(ACCT, SYMBOL, detail, "row-1")]


def test_unreadable_lookup_is_probed_once_and_paces_further_closes(caplog) -> None:
    service = _service(enabled=True, detail=_EXIT_FETCH_FAILED)
    caplog.set_level(logging.INFO, logger="test-webull-late-close")

    assert _drive(service) == "refused"
    assert _drive(service) == "refused"
    assert _drive(service) == "refused"

    assert service.submits == 1
    assert service.fetches == 1
    assert service._v2_exit_reject_total[KEY] == 1
    assert service._v2_exit_reject_total[KEY] <= service._V2_EXIT_MAX_REJECTS_PER_EPISODE
    assert sum("decision=suppressed" in message for message in caplog.messages) == 1

    service._webull_late_close_retry_after[KEY] = datetime.now(UTC) - timedelta(milliseconds=1)
    assert _drive(service) == "refused"
    assert service.submits == 2
    assert service.fetches == 1, "the OCO lookup is once per close episode, even after a 429"


def test_released_protection_is_never_paced_even_after_an_unreadable_probe() -> None:
    service = _service(enabled=True, detail=_EXIT_FETCH_FAILED, released=True)

    assert _drive(service) == "refused"
    assert _drive(service) == "refused"

    assert service.submits == 2, "an uncovered position must retain the original close cadence"
    assert service.fetches == 1
    assert service._webull_late_close_retry_after == {}

    service._webull_late_close_retry_after[KEY] = datetime.now(UTC) + timedelta(seconds=30)
    assert _drive(service) == "refused"
    assert service.submits == 3, "even stale pacing state must not delay a released position"


def test_partial_oco_fill_never_closes_or_infers_flat() -> None:
    service = _service(enabled=True, detail={"quantity": 1, "price": "5.63"})

    assert _drive(service) == "refused"

    assert service.resolved == []
    assert service._webull_late_close_retry_after[KEY] > datetime.now(UTC)


def test_episode_end_clears_every_lc1_memory_key() -> None:
    service = _service(enabled=True, detail=None)
    service._webull_late_close_oco_probed.add(KEY)
    service._webull_late_close_retry_after[KEY] = datetime.now(UTC)
    service._webull_late_close_logged_until[KEY] = datetime.now(UTC)

    service._v2_exit_end_episode(KEY)

    assert KEY not in service._webull_late_close_oco_probed
    assert KEY not in service._webull_late_close_retry_after
    assert KEY not in service._webull_late_close_logged_until

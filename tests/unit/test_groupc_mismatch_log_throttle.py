"""C5 changes diagnostic cadence only; real retention remains exercised."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import UUID

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import VirtualPosition
from project_mai_tai.oms import service as module
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings
from tests.unit.test_control_plane import build_test_session_factory
from tests.unit.test_momlive1_virtual_clear import seed
from tests.unit.test_oms_risk_service import FakeRedis


ACCOUNT = UUID(int=1)
NAMES = {ACCOUNT: "live:controlled"}


def subject(monkeypatch):
    result = OmsRiskService.__new__(OmsRiskService)
    result.logger = Mock()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    return result, clock


def log(result, quantity=25, *, symbol="BBCI", account=ACCOUNT, names=NAMES):
    result._log_momentum_owned_backing_mismatch([(account, symbol, D(quantity))], names)


def test_first_change_and_exact_60_second_boundary(monkeypatch):
    result, clock = subject(monkeypatch)
    log(result)
    clock[0] += 59.999
    log(result)
    assert result.logger.critical.call_count == 1
    clock[0] = 160.0
    log(result)
    assert result.logger.critical.call_count == 2
    clock[0] += 0.001
    log(result, 24)
    assert result.logger.critical.call_count == 3


@pytest.mark.parametrize("change", ["account", "symbol", "quantity", "alias"])
def test_changed_evidence_logs_without_waiting(change, monkeypatch):
    result, _ = subject(monkeypatch)
    log(result)
    kwargs = {"account": {"account": UUID(int=2)}, "symbol": {"symbol": "MLEC"},
              "quantity": {"quantity": 24}, "alias": {"names": {ACCOUNT: "live:renamed"}}}[change]
    log(result, **kwargs)
    assert result.logger.critical.call_count == 2


def test_reorder_and_decimal_scale_do_not_fake_a_change(monkeypatch):
    result, _ = subject(monkeypatch)
    rows = [(ACCOUNT, "BBCI", D("25.00")), (ACCOUNT, "MLEC", D(35))]
    result._log_momentum_owned_backing_mismatch(rows, NAMES)
    result._log_momentum_owned_backing_mismatch(
        [(ACCOUNT, "MLEC", D("35.0")), (ACCOUNT, "BBCI", D(25))], NAMES)
    assert result.logger.critical.call_count == 1


def test_clear_rearms_immediately_and_state_stays_bounded(monkeypatch):
    result, _ = subject(monkeypatch)
    log(result)
    result._log_momentum_owned_backing_mismatch([], NAMES)
    log(result)
    assert result.logger.critical.call_count == 2
    state = result._momentum_backing_mismatch_log
    assert set(state) == {"lock", "accounts"}
    assert len(state["accounts"]) == 1
    assert len(state["accounts"][ACCOUNT][0]) == 32


def test_change_outside_displayed_16_rows_is_detected(monkeypatch):
    result, _ = subject(monkeypatch)
    rows = [(ACCOUNT, f"S{i}", D(25)) for i in range(17)]
    result._log_momentum_owned_backing_mismatch(rows, NAMES)
    rows[-1] = (ACCOUNT, "S16", D(24))
    result._log_momentum_owned_backing_mismatch(rows, NAMES)
    assert result.logger.critical.call_count == 2
    assert result.logger.critical.call_args.args[1] == 17
    assert "S16" not in result.logger.critical.call_args.args[2]


def test_overlapping_sync_workers_emit_once(monkeypatch):
    result, _ = subject(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: log(result), range(12)))
    assert result.logger.critical.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["schwab", "webull"])
async def test_repeated_actual_sync_keeps_owned_quantity_and_diagnostics(provider, monkeypatch):
    factory = build_test_session_factory()
    seed(factory, provider=provider)
    result = OmsRiskService(Settings(_env_file=None, oms_adapter="simulated",
        oms_virtual_position_clear_min_age_seconds=0), FakeRedis(), session_factory=factory)
    async def empty_positions(account):
        return []
    result.broker_adapter = SimpleNamespace(list_account_positions=empty_positions)
    result.logger = Mock()
    clock = [100.0]
    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    for _ in range(2):
        await result.sync_broker_positions()
    with factory() as session:
        assert session.scalar(select(VirtualPosition.quantity)) == 25
    assert result.logger.critical.call_count == 1
    clock[0] += 60
    await result.sync_broker_positions()
    assert result.logger.critical.call_count == 2


def test_unconditional_old_logging_mutation_is_detected(monkeypatch):
    def old(result, mismatches, names):
        if mismatches:
            result.logger.critical("unconditional old receipt")
    monkeypatch.setattr(OmsRiskService, "_log_momentum_owned_backing_mismatch", old)
    with pytest.raises(AssertionError):
        test_first_change_and_exact_60_second_boundary(monkeypatch)

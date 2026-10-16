"""Real run/boot-sync/writer commit ordering, with controlled broker and Redis."""

import asyncio
from datetime import UTC, datetime
import logging
from threading import Event
from time import monotonic

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from project_mai_tai.db.base import Base
from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.oms import service as oms
from project_mai_tai.oms.bot_control import ControlConflict
from project_mai_tai.oms.bot_control_evidence import positions_after_protection
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai import positions_read_receipt as receipts
from project_mai_tai.settings import Settings
from tests.unit.test_oms_risk_service import FakeRedis


class QuietRedis(FakeRedis):
    async def xrevrange(self, *args, **kwargs):
        return []

    async def xread(self, *args, **kwargs):
        await asyncio.Event().wait()


@pytest.fixture
def boot(tmp_path, monkeypatch, caplog, request):
    engine = create_engine(f"sqlite:///{tmp_path / 'actual-boot.sqlite'}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    observed = []

    class Broker:
        async def list_account_positions(self, account):
            runtime = service.__dict__.get("_bot_control_runtime")
            if runtime is not None:
                assert runtime.boot_managed_ids is not None and runtime.boot_orb_entry_ids is not None
            observed.append((account, datetime.now(UTC)))
            return []  # A complete controlled flat response, not a failed-read substitute.

        def positions_wire_read_age_seconds(self, account):
            return 0.0

        async def submit_order(self, request):
            raise AssertionError("boot receipt proof must never submit an order")

    control = getattr(request.node, "callspec", None)
    control = control.params.get("control_enabled", False) if control is not None else False
    service = OmsRiskService(Settings(_env_file=None, oms_adapter="paper", momentum_live_enabled=False,
        bot_control_enabled=control,
        oms_armed_stop_persistence_enabled=True,
        strategy_schwab_1m_v2_account_name="live:schwab_1m_v2",
        strategy_schwab_1m_v2_webull_account_name="live:orb",
        strategy_schwab_1m_v2_dual_broker_fanout_enabled=True), QuietRedis(),
        session_factory=factory, broker_adapter=Broker())
    # The paper adapter's registry also creates paper aliases. The controlled
    # boot inventory must actually contain BOTH live accounts we assert below.
    with factory.begin() as unit:
        for name, provider in (("live:schwab_1m_v2", "schwab"), ("live:orb", "webull")):
            service.store.ensure_broker_account(unit, name, provider=provider, environment="production")
    monkeypatch.setattr(oms, "_install_signal_handlers", lambda _: None)
    monkeypatch.setattr(oms, "receipt_session_factory", lambda _: factory)
    # OmsRiskService configures root handlers; capture its actual two loggers directly.
    for logger in (service.logger, receipts.logger):
        monkeypatch.setattr(logger, "handlers", [caplog.handler])
        monkeypatch.setattr(logger, "propagate", False)
        monkeypatch.setattr(logger, "level", logging.INFO)
    yield service, factory, observed
    engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("control_enabled", [False, True])
async def test_real_run_boot_sync_commits_receipts_before_any_periodic_sync(
    boot, monkeypatch, caplog, control_enabled, record_property,
):
    service, factory, observed = boot
    actual_reconcile = service._reconcile_protection_before_serving
    boot_calls = []

    async def reconcile():
        writer = service._positions_read_receipt_writer
        assert writer.boot_id and writer.boot_started_monotonic <= monotonic()
        boot_calls.append(writer)
        await actual_reconcile()

    async def control(stop):
        assert len(boot_calls) == 1 and observed
        writer = boot_calls[0]
        async with asyncio.timeout(3):
            await writer.queue.join()
        with factory() as unit:
            actual = receipts.load_receipts(unit)
        for account in ("live:schwab_1m_v2", "live:orb"):
            assert account in actual and account in writer.first_commits
            commit = writer.first_commits[account]
            assert actual[account]["read_at"] == commit["read_at"]
            assert commit["read_at"] <= commit["committed_at"]
            assert commit["elapsed_ms"] >= 0 and commit["boot_id"] == writer.boot_id
            record_property(f"first_commit_elapsed_ms_{account}", commit["elapsed_ms"])
        stop.set()

    monkeypatch.setattr(service, "_reconcile_protection_before_serving", reconcile)
    monkeypatch.setattr(service, "_run_control_loop", control)
    await asyncio.wait_for(service.run(), 5)
    logs = [r.getMessage() for r in caplog.records if "[RECEIPT-FIRST-COMMIT]" in r.getMessage()]
    for account in ("live:schwab_1m_v2", "live:orb"):
        assert len([line for line in logs if f"account={account} " in line]) == 1
    assert not [task for task in asyncio.all_tasks() if not task.done()
                and getattr(task.get_coro(), "__qualname__", "").endswith("PositionsReadReceiptWriter.run")]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["factory", "commit"])
async def test_writer_failure_never_creates_receipt_permission_or_first_commit_metric(boot, monkeypatch, caplog, failure):
    service, factory, observed = boot
    if failure == "factory":
        def unavailable(_):
            raise RuntimeError("controlled receipt pool unavailable")
        monkeypatch.setattr(oms, "receipt_session_factory", unavailable)
    else:
        def failed_commit(self, batches):
            raise RuntimeError("controlled commit failed")
        monkeypatch.setattr(receipts.PositionsReadReceiptWriter, "_persist", failed_commit)

    async def control(stop):
        assert observed  # Real successful positions sync is NOT a receipt commit.
        writer = service.__dict__.get("_positions_read_receipt_writer")
        if writer is not None:
            async with asyncio.timeout(3):
                await writer.queue.join()
            assert not writer.first_commits
        with factory() as unit:
            assert unit.scalar(select(DashboardSnapshot.id).where(
                DashboardSnapshot.snapshot_type == receipts.SNAPSHOT_TYPE)) is None
            for account in ("live:schwab_1m_v2", "live:orb"):
                with pytest.raises(ControlConflict, match="positions_wire_receipt_unknown"):
                    positions_after_protection(unit, account_name=account,
                        terminal_at=datetime.now(UTC), now=datetime.now(UTC))
        stop.set()

    monkeypatch.setattr(service, "_run_control_loop", control)
    await asyncio.wait_for(service.run(), 5)
    assert "[RECEIPT-FIRST-COMMIT]" not in caplog.text
    assert ("writer_unavailable" if failure == "factory" else "write_failed") in caplog.text


@pytest.mark.asyncio
async def test_first_commit_metric_cannot_precede_actual_database_commit(boot, monkeypatch, caplog):
    _service, factory, _observed = boot
    entered, release = Event(), Event()
    actual_factory = factory

    class HeldSession:
        def __enter__(self):
            self.session = actual_factory()
            self.actual_commit = self.session.commit
            def held_commit():
                entered.set()
                if not release.wait(3):
                    raise AssertionError("test did not release real commit")
                self.actual_commit()
            self.session.commit = held_commit
            return self.session

        def __exit__(self, *args):
            self.session.close()

    writer = receipts.PositionsReadReceiptWriter(HeldSession, boot_id="controlled-real-commit",
                                                boot_started_monotonic=monotonic())
    wire_at = datetime.now(UTC)
    worker = asyncio.create_task(writer.run())
    try:
        writer.offer([("live:orb", wire_at, 0)])
        assert await asyncio.to_thread(entered.wait, 1)
        assert not writer.first_commits and "[RECEIPT-FIRST-COMMIT]" not in caplog.text
        with factory() as unit:
            assert receipts.load_receipts(unit) == {}
        release.set()
        async with asyncio.timeout(3):
            await writer.queue.join()
        assert writer.first_commits["live:orb"]["read_at"] == wire_at
        assert "[RECEIPT-FIRST-COMMIT]" in caplog.text
        writer.offer([("live:orb", wire_at, 0)])
        async with asyncio.timeout(3):
            await writer.queue.join()
        assert caplog.text.count("[RECEIPT-FIRST-COMMIT]") == 1
    finally:
        release.set()
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)


@pytest.mark.asyncio
async def test_early_boot_failure_still_cancels_the_new_early_receipt_worker(boot, monkeypatch):
    service, _factory, _observed = boot
    async def failed_boot():
        assert service._positions_read_receipt_writer is not None
        raise RuntimeError("controlled boot restore failed")
    monkeypatch.setattr(service, "_reconcile_protection_before_serving", failed_boot)
    with pytest.raises(RuntimeError, match="controlled boot restore failed"):
        await asyncio.wait_for(service.run(), 5)
    assert not [task for task in asyncio.all_tasks() if not task.done()
                and getattr(task.get_coro(), "__qualname__", "").endswith("PositionsReadReceiptWriter.run")]

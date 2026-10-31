"""SOURCE UNRUN: exact package settings/constructors, controlled private inputs.

No source initializer/permission or logger success replacement. The existing
owned broker fixture supplies synthetic provider data, never native evidence.
"""
from datetime import UTC, datetime
import json
from pathlib import Path
import sys

from sqlalchemy import select

from project_mai_tai import brokerdecides1_sync
from project_mai_tai.broker_adapters.routing import RoutingBrokerAdapter
from project_mai_tai.db.models import Strategy
from project_mai_tai.oms import service as oms, wbexit3
from project_mai_tai.oms.bot_control_runtime import ControlledBrokerAdapter
from project_mai_tai.services import schwab_1m_v2_bot as bot_module
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import schwab_1m_v2 as strategy_module
from project_mai_tai.strategy_core.schwab_1m_v2 import OHLCVBar
from tests.unit.test_wbexit2_route import owner_case
from tests.unit.wbexit3_narrow_boot_fixture import RecordingRefuser, _physical_join

ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "tests/fixtures/oct30_allon/profile.json"
INSTALL_FLAGS = ("strategy_schwab_1m_v2_pm_softrest_quote_log_enabled",
    "strategy_schwab_1m_v2_flip_print_diagnostic_enabled", "oms_webull_exit3_enabled")
PM_AT = datetime(2026, 10, 29, 11, 40, 20, 257223, tzinfo=UTC)
PM_MS = int(PM_AT.timestamp() * 1000)


def profile():
    value = json.loads(PROFILE.read_text())
    expected = {row["name"]: row for row in json.loads(
        (ROOT / "ops/health/expected_flags.json").read_text())["flags"]}
    for name, on in value["catalog_flags"].items():
        assert name in Settings.model_fields and Settings.model_fields[name].annotation is bool
        assert type(on) is bool and on is True
        assert expected[name]["expected"] is on
    assert set(INSTALL_FLAGS) <= set(value["catalog_flags"])
    return value


def settings_for_allon(original):
    value = profile()
    options = {**original.model_dump(), **value["catalog_flags"], **value["live_identity"]}
    for name, setting in {**value["catalog_flags"], **value["live_identity"]}.items():
        assert name in Settings.model_fields
        assert Settings.model_fields[name].annotation in (bool, str)
        assert type(setting) is Settings.model_fields[name].annotation
    result = Settings(_env_file=None, **options)
    assert_allon(result)
    return result


def assert_allon(settings):
    value = profile()
    for name, expected in {**value["catalog_flags"], **value["live_identity"]}.items():
        assert getattr(settings, name) == expected, name
    assert all(getattr(settings, key) is True for key in INSTALL_FLAGS)


async def allon_owned_case(make_case):
    # Existing fixture creates only the controlled ledger. The consumer under
    # test below is a NEW real OMS constructed with the ALLON Settings already
    # present, never a label applied to a fixture whose consumer stays OFF.
    old = make_case(control=True)
    settings = settings_for_allon(old[0].settings)
    denied = RecordingRefuser("CONTROLLED setup provider")
    service = oms.OmsRiskService(settings=settings, redis_client=old[0].redis,
        session_factory=old[1], broker_adapter=denied)
    assert service.settings is settings
    runtime = service._bot_control_runtime
    assert runtime is not None
    seeded = (service, old[1], old[2], old[3], old[4])
    try:
        with old[1]() as db:
            strategy_id = db.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
        # Explicit controlled local provisioning through the REAL journal/store.
        # This is not production/control hydration or a callback returning True.
        runtime.store.seed(strategy_id)
        await _physical_join(runtime.refresh())
        await _physical_join(wbexit3.hydrate_at_boot(service))
        case = await _physical_join(owner_case(lambda: seeded))
        broker = case[2]
        router = RoutingBrokerAdapter(default_provider="schwab",
            provider_by_account={broker.account: "webull"},
            factories_by_provider={"webull": lambda: broker})
        service.broker_adapter = ControlledBrokerAdapter(router, runtime)
        assert service._bot_control_runtime is runtime
        assert denied.calls == []
        assert_allon(settings)
        return case, settings
    except BaseException:
        await drain(seeded)
        raise


def real_bot(settings, factory, monkeypatch):
    class PMClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return PM_AT.astimezone(tz) if tz is not None else PM_AT.replace(tzinfo=None)
    monkeypatch.setattr(bot_module, "datetime", PMClock)
    monkeypatch.setattr(strategy_module, "datetime", PMClock)
    monkeypatch.setattr(bot_module.time, "time_ns", lambda: PM_MS * 1_000_000)
    phone = RecordingRefuser("CONTROLLED owner phone")
    bot = bot_module.SchwabV2BotService(settings=settings, session_factory=factory,
        owner_phone_sender=phone)
    assert bot.settings is settings and bot.strategy.settings is settings
    assert bot._pm_softrest_quote_log is not None
    assert bot.strategy._flip_print_enabled is True
    assert bot._keepwatch_enabled is True and bot._line_restoration_enabled is True
    assert bot._eh_stream_cross_enabled is True
    # Literal existing run1549–1559 initialization seam, without starting run:
    # constructor alone leaves tick_writer undefined. Keep capture ON and use
    # its ACTUAL writer and PRIVATE factory, not a None/success sink to pass.
    assert settings.strategy_schwab_1m_v2_tick_capture_enabled is True
    bot.tick_writer = bot_module.SchwabV2TickWriter(settings, factory)
    assert bot.tick_writer.settings is settings and bot.tick_writer.session_factory is factory
    # Controlled armed-softrest state, preserving actual LINE/KEEPWATCH/boot
    # guards and actual constructor-installed callbacks. No success precondition.
    state = bot.strategy.watchlist_state("NCPL")
    state.atr_trail = state.resting_level = 1.536583
    state.resting_trigger = state.resting_schwab_wire_stop = 1.55
    state.resting_wire_cap = state.resting_schwab_wire_limit = 1.56
    state.resting_active, state.resting_is_broker_order = True, False
    state.bars.append(OHLCVBar(PM_MS // 60000 * 60000 - 60000, 1.50, 1.54, 1.49, 1.53, 12000))
    bot.strategy._entries_held = True  # CONTROLLED refusal, never an admission override.
    assert bot.strategy.line_buy_ready("NCPL") is False
    return bot, state, phone


async def drain(case, bot=None):
    """Private owned lifetimes only; no process/fleet/global-task adoption."""
    primary = sys.exc_info()[1]
    failures = []
    service = case[0]
    work = []
    if bot is not None:
        try:
            # No writer task was started and no flush is invoked. The actual
            # stop method only marks its existing stop/wake Events.
            await _physical_join(bot.tick_writer.stop())
        except BaseException as exc:
            failures.append(exc)
        work.extend(tuple(bot._eh_stream_emit_tasks))
    work.extend(tuple(service._confirmation_exit_recovery_tasks))
    work.extend(tuple(service.__dict__.get("_webull_protect_tasks", ())))
    orb = service.__dict__.get("_orbroute1_reconciliation_task")
    if orb is not None:
        work.append(orb)
    for task in dict.fromkeys(work):
        try:
            await _physical_join(task)
        except BaseException as exc:
            failures.append(exc)
    for coroutine in (service._drain_webull_order_identities(), brokerdecides1_sync.shutdown_refresh(service)):
        try:
            await _physical_join(coroutine)
        except BaseException as exc:
            failures.append(exc)
    if failures:
        if primary is not None:
            for failure in failures:
                primary.add_note("ALLON owned cleanup: " + repr(failure))
            service.logger.error("[CONTROLLED-ALLON-CLEANUP] failures=%r primary=%r", failures, primary)
        else:
            for failure in failures[1:]:
                failures[0].add_note("additional cleanup: " + repr(failure))
            raise failures[0]

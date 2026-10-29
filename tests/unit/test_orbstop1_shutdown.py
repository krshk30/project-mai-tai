"""Controlled signal callbacks on real asyncio tasks; no OS signals or services.

Main owns execution. These are shutdown-unit controls, not systemd/process proof.
The actual run() cleanup is exercised with finite lifecycle data/IO ports below.
"""

import asyncio
from datetime import UTC, datetime
import signal
from types import SimpleNamespace

import pytest

from project_mai_tai.services import orb_schwab_app as app

OriginalService = app.OrbSchwabService


def invoke_entrypoint(monkeypatch, body, ports, *, fail_registration=None):
    """Run the real entrypoint/main; replace only signal registration and ports."""
    original_main = app.main
    handlers = ports.setdefault("handlers", {})
    removed = ports.setdefault("removed", [])

    def register(sig, callback, *args):
        if sig == fail_registration:
            raise RuntimeError("controlled signal registration failure")
        assert not args
        handlers[sig] = callback

    def remove(sig):
        removed.append(sig)
        return handlers.pop(sig, None) is not None

    async def service_body():
        return await body(handlers)

    async def controlled_main():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, "add_signal_handler", register)
        monkeypatch.setattr(loop, "remove_signal_handler", remove)
        return await original_main()

    monkeypatch.setattr(app, "get_settings", lambda: SimpleNamespace(log_level="INFO"))
    monkeypatch.setattr(app, "OrbSchwabService", lambda: SimpleNamespace(run=service_body))
    monkeypatch.setattr(app, "main", controlled_main)
    return app.run()  # actual asyncio.run; console sys.exit(None) means exit0


def assert_handlers_removed(ports):
    assert not ports["handlers"]
    assert ports["removed"] == [signal.SIGTERM, signal.SIGINT]


def test_normal_return_remains_zero_entrypoint(monkeypatch):
    ports = {}

    async def body(_handlers):
        return None

    assert invoke_entrypoint(monkeypatch, body, ports) is None
    assert_handlers_removed(ports)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_requested_signal_returns_zero_only_after_async_cleanup(monkeypatch, sig):
    ports, cleanup = {}, []

    async def body(handlers):
        try:
            handlers[sig]()
            await asyncio.sleep(0)
        finally:
            cleanup.append("started")
            await asyncio.sleep(0)
            cleanup.append("joined")

    assert invoke_entrypoint(monkeypatch, body, ports) is None
    assert cleanup == ["started", "joined"]
    assert_handlers_removed(ports)


def test_repeated_signals_do_not_interrupt_cleanup(monkeypatch):
    ports, cleanup = {}, []

    async def body(handlers):
        try:
            handlers[signal.SIGTERM]()
            await asyncio.sleep(0)
        finally:
            handlers[signal.SIGTERM]()
            handlers[signal.SIGINT]()
            await asyncio.sleep(0)
            cleanup.append("joined")

    assert invoke_entrypoint(monkeypatch, body, ports) is None
    assert cleanup == ["joined"]
    assert_handlers_removed(ports)


def test_genuine_run_error_is_not_shutdown_success(monkeypatch):
    ports = {}

    async def body(_handlers):
        raise RuntimeError("controlled real run failure")

    with pytest.raises(RuntimeError, match="controlled real run failure"):
        invoke_entrypoint(monkeypatch, body, ports)
    assert_handlers_removed(ports)


def test_genuine_cleanup_error_after_signal_stays_failure(monkeypatch):
    ports = {}

    async def body(handlers):
        try:
            handlers[signal.SIGTERM]()
            await asyncio.sleep(0)
        finally:
            raise RuntimeError("controlled real cleanup failure")

    with pytest.raises(RuntimeError, match="controlled real cleanup failure") as failure:
        invoke_entrypoint(monkeypatch, body, ports)
    assert isinstance(failure.value.__context__, asyncio.CancelledError)
    assert_handlers_removed(ports)


@pytest.mark.parametrize("signal_first", [False, True])
def test_unrelated_cancellation_is_not_swallowed(monkeypatch, signal_first):
    ports = {}

    async def body(handlers):
        if signal_first:
            try:
                handlers[signal.SIGTERM]()
                await asyncio.sleep(0)
            finally:
                raise asyncio.CancelledError("controlled unrelated cleanup cancellation")
        raise asyncio.CancelledError("controlled unrelated run cancellation")

    with pytest.raises(asyncio.CancelledError):
        invoke_entrypoint(monkeypatch, body, ports)
    assert_handlers_removed(ports)


class WaitingTape:
    def __init__(self, trace):
        self.trace = trace
        self.started = asyncio.Event()
        self.session_factory = None

    async def run(self):
        self.started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.trace.append("tape_joined")


class RetainedRouter:
    def __init__(self, trace):
        self.trace = trace

    async def close(self):
        self.trace.append("router_closed")


class LifecyclePorts(OriginalService):
    """Use ORIGINAL run(), replacing data/IO dependencies, never trading guards."""

    def __init__(self, *, announced, hydration_error=False):
        self.trace = []
        self.settings = SimpleNamespace(orb_enabled=True, orb_live_schwab_orders_enabled=True,
            orb_country_routing_enabled=False, orb_webull_account_name="",
            orb_schwab_atr_entry_gate_enabled=True, market_data_subscription_startup_enabled=True)
        self.session_factory = object()  # no DB constructor/read
        self._observe_only = False
        self._country_router = RetainedRouter(self.trace)
        self._session_date = datetime.now(UTC).astimezone(app._ET).date()
        self._last_universe_refresh_at = None
        self._universe = set()
        self._exit_held_symbols = set()
        self._gateway_subscription_announced = announced
        self._live_decision_tape = WaitingTape(self.trace)
        self.heartbeat_started = asyncio.Event()
        self.drain_started = asyncio.Event()
        self.subscriptions = []
        self.hydration_error = hydration_error

    def _owned_orb_entries(self):
        if self.hydration_error:
            raise RuntimeError("controlled ownership hydration unavailable")
        return [{"symbol": "HELD"}]

    def _maybe_roll_session(self):
        return None

    def _refresh_universe(self):
        return None

    async def _sync_gateway_subscription(self, symbols):
        self.subscriptions.append(tuple(symbols))

    async def _drain_market_data(self):
        self.drain_started.set()
        await asyncio.Event().wait()

    async def _live_heartbeat_loop(self):
        self.heartbeat_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            self.trace.append("heartbeat_joined")


@pytest.mark.parametrize("announced", [False, True])
def test_actual_run_cleanup_joins_children_and_preserves_coverage_rule(monkeypatch, announced):
    ports, retained = {}, []
    monkeypatch.setattr(app, "decision_session_factory", lambda _settings: object())

    async def body(handlers):
        service = LifecyclePorts(announced=announced)
        retained.append(service)

        async def request_stop():
            await service.drain_started.wait()
            await service.heartbeat_started.wait()
            await service._live_decision_tape.started.wait()
            handlers[signal.SIGTERM]()

        trigger = asyncio.create_task(request_stop())
        try:
            await service.run()  # inherited production method, not a fake run loop
        finally:
            await trigger

    assert invoke_entrypoint(monkeypatch, body, ports) is None
    service = retained[0]
    assert service.trace == ["router_closed", "tape_joined", "heartbeat_joined"]
    assert service.subscriptions == ([("HELD",), ()] if announced else [("HELD",)])
    assert service._exit_held_symbols == {"HELD"}
    assert_handlers_removed(ports)


def test_actual_startup_ownership_read_error_never_releases_coverage(monkeypatch):
    ports, retained = {}, []

    async def body(_handlers):
        service = LifecyclePorts(announced=True, hydration_error=True)
        retained.append(service)
        await service.run()

    with pytest.raises(RuntimeError, match="controlled ownership hydration unavailable"):
        invoke_entrypoint(monkeypatch, body, ports)
    assert retained[0].subscriptions == []
    assert retained[0].trace == []  # original pre-finally hydration fence retained
    assert_handlers_removed(ports)


def test_partial_signal_registration_error_is_visible_and_cleans_installed_handler(monkeypatch):
    ports, calls = {}, []

    async def body(_handlers):
        calls.append("run")

    with pytest.raises(RuntimeError, match="controlled signal registration failure"):
        invoke_entrypoint(monkeypatch, body, ports, fail_registration=signal.SIGINT)
    assert not calls and not ports["handlers"]
    assert ports["removed"] == [signal.SIGTERM]

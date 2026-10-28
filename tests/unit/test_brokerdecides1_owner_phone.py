"""All transport/processes are injected FAKES; no actual curl/network/phone."""

import asyncio
from hashlib import sha256
from pathlib import Path

import pytest

from project_mai_tai import brokerdecides1_owner_phone as phone
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from project_mai_tai.brokerdecides1_owner_watchdog import owner_watchdog_pass as _owner_poll
from tests.unit._brokerdecides_alert_controls import poll_and_settle_new_task
from tests.unit.test_brokerdecides1_unused_owner_sell import case, NOW
from tests.unit.test_brokerdecides1_crossday_owner import refresh


async def owner_watchdog_pass(service):
    return await poll_and_settle_new_task(_owner_poll, service)


class Process:
    def __init__(self, output=b"200", code=0, gate=None):
        self.output, self.code, self.gate = output, code, gate
        self.returncode = None
        self.input = None
        self.killed = self.reaped = False

    async def communicate(self, raw):
        self.input = raw
        if self.gate is not None:
            await self.gate.wait()
        self.returncode = self.code
        return self.output, None

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self):
        self.reaped = True
        return self.returncode


def sender(process):
    calls = []
    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process
    return phone.ExistingChannelSender("https://ntfy.sh/CONTROLLED_NOT_SENT", spawn=spawn), calls


def test_existing_channel_source_is_pinned_and_read_not_executed():
    raw = b'NTFY_URL="https://ntfy.sh/CONTROLLED_NOT_SENT"\nraise RuntimeError("MUST_NOT_EXECUTE")\n'
    assert phone.existing_channel(raw, reviewed_sha=sha256(raw).hexdigest()) == "https://ntfy.sh/CONTROLLED_NOT_SENT"
    real = Path(__file__).resolve().parents[2] / "ops/health/armed_segments_check.py"
    assert sha256(real.read_bytes()).hexdigest() == "ee8c3116635429e7ad2ed5453bcd8cb13ebc311f528bc46da2c5fa20cedbe1e2"


@pytest.mark.parametrize("literal", ["http://ntfy.sh/topic", "https://foreign.example/topic", "https://ntfy.sh/",
    "https://ntfy.sh/two/topics", "https://ntfy.sh/topic?query=x", "https://ntfy.sh/topic#fragment"])
def test_unknown_channel_shape_fails_closed(literal):
    raw = f"NTFY_URL={literal!r}".encode()
    with pytest.raises(ValueError):
        phone.existing_channel(raw, reviewed_sha=sha256(raw).hexdigest())


def test_channel_drift_requires_new_independent_source_pin():
    with pytest.raises(ValueError):
        phone.existing_channel(b'NTFY_URL="https://ntfy.sh/CONTROLLED_NOT_SENT"')


@pytest.mark.asyncio
@pytest.mark.parametrize("output,code,accepted", [(b"200", 0, True), (b"201", 0, True), (b"202", 0, True),
    (b"204", 0, True), (b"301", 0, False), (b"429", 22, False), (b"500", 22, False),
    (b"000", 7, False), (b"", 0, False), (b"200\n", 0, False), (b"200", 7, False)])
async def test_only_clean_http_acceptance_consumes_publish_attempt(output, code, accepted):
    process = Process(output, code)
    send, calls = sender(process)
    assert await send("RED owner lock", "symbol=CLRO", priority="urgent", tags="rotating_light") is accepted
    args, kwargs = calls[0]
    assert args[0] == "/usr/bin/curl" and args[-1] == "https://ntfy.sh/CONTROLLED_NOT_SENT"
    assert args[args.index("--data-binary") + 1] == "@-"
    assert process.input == b"symbol=CLRO" and process.reaped
    assert kwargs["stdin"] == asyncio.subprocess.PIPE and "--location" not in args


@pytest.mark.asyncio
async def test_permission_or_missing_binary_never_claims_privilege_or_delivery():
    async def denied(*args, **kwargs): raise PermissionError("CONTROLLED")
    send = phone.ExistingChannelSender("https://ntfy.sh/CONTROLLED_NOT_SENT", spawn=denied)
    assert await send("RED", "body") is False


@pytest.mark.asyncio
async def test_timeout_kills_and_reaps_exact_process_without_consuming_phone():
    process = Process()
    send, calls = sender(process)
    async def timeout(awaitable, timeout):
        awaitable.close()
        raise TimeoutError("CONTROLLED")
    send.wait_for = timeout
    assert await send("RED", "body") is False
    assert len(calls) == 1 and process.killed and process.reaped


@pytest.mark.asyncio
async def test_cancellation_reaps_inflight_process_and_propagates():
    gate = asyncio.Event()
    process = Process(gate=gate)
    send, calls = sender(process)
    task = asyncio.create_task(send("RED", "body"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert len(calls) == 1 and process.killed and process.reaped


@pytest.mark.asyncio
async def test_unknown_channel_and_header_injection_never_spawn():
    process = Process()
    send, calls = sender(process)
    assert await send("RED\nInjected: header", "body") is False
    assert await send("RED", "x" * 8193) is False
    send.channel = None
    assert await send("RED", "body") is False
    assert calls == []


@pytest.mark.asyncio
async def test_actual_service_constructor_injection_watchdog_sends_once_and_keeps_owner_unknown():
    process = Process()
    send, calls = sender(process)
    settings = Settings(strategy_schwab_1m_v2_brokerdecides1_enabled=True)
    service = SchwabV2BotService(settings, owner_phone_sender=send)
    strategy, state, _ = case(phase="unknown")
    service.strategy = strategy
    assert service._brokerdecides1_owner_phone is send
    assert not strategy._strict_first_rest_admitted(state, slot="first")
    for elapsed in (300001, 360001):
        refresh(strategy, NOW + elapsed)
        await owner_watchdog_pass(service)
    assert len(calls) == 1 and state._brokerdecides1_owner_hold.delivered is True
    assert state.flip_owner_phase == "unknown" and process.reaped


def test_flag_off_does_not_install_or_construct_sender(monkeypatch):
    def forbidden(): raise AssertionError("flag-off factory")
    monkeypatch.setattr(phone, "make_owner_phone", forbidden)
    service = SchwabV2BotService(Settings(strategy_schwab_1m_v2_brokerdecides1_enabled=False))
    assert service._brokerdecides1_owner_phone is None


def test_service_default_factory_is_installed_once_without_network(monkeypatch):
    calls = []
    injected = object()
    def make(): calls.append(1); return injected
    monkeypatch.setattr(phone, "make_owner_phone", make)
    service = SchwabV2BotService(Settings(strategy_schwab_1m_v2_brokerdecides1_enabled=True))
    assert calls == [1] and service._brokerdecides1_owner_phone is injected

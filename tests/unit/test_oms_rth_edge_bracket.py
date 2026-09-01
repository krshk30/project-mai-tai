"""The RTH-edge bracket sweep (#646 Part 1) — arm a PRE-MARKET entry the instant the broker allows.

⭐ WHY A SWEEP AT ALL. `_apply_v2_oco_bracket_entry` decorates a BUY-open intent, so a bracket can
only attach to an order we are placing. A position entered at 07:30 and still held at 09:30 is
never bracketed for its entire life — nothing revisits it — so it rides the software ladder all
day. That ladder produced KUST (−5.17%, nine cancels against a bid that never fell below the limit).

⛔ 09:30 IS THE BROKER'S TIME, NOT OURS. Probe P (2026-08-04, preview-only) measured Schwab
rejecting a STOP leg in the AM session: "This order type is not available for this session."
A bracket cannot exist before the open however we schedule it.

⭐ THE ORDERING IS FREE BY CONSTRUCTION. This sweep only PLACES the OCO; it never stands the
software exit down. The stand-down is driven by `_refresh_native_oco_armed_state`, which activates
only once the BROKER confirms both legs WORKING. So the software exit keeps protecting the position
until the bracket is provably live, and a failed placement leaves it exactly as it was. There is no
unprotected gap and no new ordering logic to get wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.oms import service as oms_service
from project_mai_tai.oms.service import OmsRiskService
from project_mai_tai.settings import Settings

ET = ZoneInfo("America/New_York")
RTH_NOW = datetime(2026, 9, 1, 14, 0, tzinfo=UTC)  # Tuesday 10:00 ET


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return RTH_NOW.replace(tzinfo=None)
        return RTH_NOW.astimezone(tz)


@pytest.fixture(autouse=True)
def _inject_oms_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oms_service, "utcnow", lambda: RTH_NOW)
    monkeypatch.setattr(oms_service, "datetime", _FrozenDateTime)


class _Logger:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _rec(self, level):
        def log(msg, *args):
            self.lines.append((level, msg % args if args else msg))
        return log

    def __getattr__(self, name):
        return self._rec(name)


class _Adapter:
    def __init__(self, armed: set[str] | None = None, boom: bool = False) -> None:
        self.armed = armed or set()
        self.boom = boom
        self.calls: list[tuple[str, list[str]]] = []

    async def fetch_armed_native_oco_symbols(self, acct: str, symbols: list[str]) -> set[str]:
        self.calls.append((acct, list(symbols)))
        if self.boom:
            raise RuntimeError("broker unreachable")
        return {s for s in symbols if s in self.armed}


def _clock_svc(**over) -> OmsRiskService:
    """For the due-check tests only: the REAL `_v2_rth_edge_bracket_due`, driven by an explicit
    `now`. Kept separate from `_svc` so the sweep tests cannot accidentally assert the clock."""
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = Settings(**over)
    return svc


def _svc(*, enabled: bool = True, adapter: _Adapter | None = None, **over) -> OmsRiskService:
    """For the SWEEP tests. `due` is stubbed True so these assert the sweep's own logic and do not
    silently pass merely because the suite happened to run before 09:30 ET — which is exactly how
    they first 'passed' while emitting nothing at all."""
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.settings = Settings(oms_v2_rth_edge_bracket_enabled=enabled, **over)
    svc._v2_rth_edge_bracket_due = lambda now=None: True
    svc.logger = _Logger()
    svc.broker_adapter = adapter if adapter is not None else _Adapter()
    svc._managed_v2_symbols = {("live:schwab_1m_v2", "KUST")}
    svc._v2_rth_edge_bracket_done = set()
    svc._v2_rth_edge_bracket_attempts = {}
    svc._v2_rth_edge_bracket_last_try = {}
    svc.emitted: list[tuple[str, str]] = []

    async def _emit(*, acct, symbol, edge_et, rearm=False):
        svc.emitted.append((acct, symbol))

    svc._emit_v2_rth_edge_bracket = _emit
    return svc


# ------------------------------------------------------------------ the clock

def test_not_due_before_the_open() -> None:
    """⛔ The one that matters: firing at 09:29 means emitting an order Schwab will reject."""
    svc = _clock_svc()
    at = datetime(2026, 8, 4, 9, 29, tzinfo=ET).astimezone(UTC)
    assert svc._v2_rth_edge_bracket_due(at) is False


def test_due_from_the_opening_bell_onward() -> None:
    svc = _clock_svc()
    assert svc._v2_rth_edge_bracket_due(datetime(2026, 8, 4, 9, 30, tzinfo=ET).astimezone(UTC)) is True
    assert svc._v2_rth_edge_bracket_due(datetime(2026, 8, 4, 14, 0, tzinfo=ET).astimezone(UTC)) is True


def test_never_due_at_the_weekend() -> None:
    svc = _clock_svc()
    assert svc._v2_rth_edge_bracket_due(datetime(2026, 8, 1, 12, 0, tzinfo=ET).astimezone(UTC)) is False


def test_the_edge_time_is_pinned_to_0930() -> None:
    """⛔ Pin the VALUE. A default that silently drifts is how the vol floor guarded dead code."""
    s = Settings()
    assert (s.oms_v2_rth_edge_bracket_hour_et, s.oms_v2_rth_edge_bracket_minute_et) == (9, 30)


def test_the_flag_defaults_OFF() -> None:
    """This sweep places real broker orders against real positions. Off until deliberately armed."""
    assert Settings().oms_v2_rth_edge_bracket_enabled is False


# ------------------------------------------------------------------ the sweep

@pytest.mark.asyncio
async def test_flag_off_is_a_total_no_op() -> None:
    """Kill switch: not one broker call, not one emit."""
    svc = _svc(enabled=False)
    await svc._v2_rth_edge_bracket()
    assert svc.broker_adapter.calls == [] and svc.emitted == []


@pytest.mark.asyncio
async def test_a_position_already_bracketed_at_the_broker_is_left_alone() -> None:
    """⛔ Double-bracketing is two OCO pairs reserving the same shares — the E5 oversell. Broker
    truth decides, not our own belief about what we armed."""
    svc = _svc(adapter=_Adapter(armed={"KUST"}))
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == []
    # and it stops asking about it
    assert any(k[2] == "KUST" for k in svc._v2_rth_edge_bracket_done)


@pytest.mark.asyncio
async def test_an_unbracketed_position_is_armed_once() -> None:
    svc = _svc()
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == [("live:schwab_1m_v2", "KUST")]
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == [("live:schwab_1m_v2", "KUST")], "must not re-arm an armed position"


@pytest.mark.asyncio
async def test_an_unreadable_broker_FAILS_CLOSED() -> None:
    """We cannot prove a position is unbracketed, so we must not place. A late bracket beats a
    double one."""
    svc = _svc(adapter=_Adapter(boom=True))
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == []
    assert any("could not read armed OCO state" in m for _, m in svc.logger.lines)


@pytest.mark.asyncio
async def test_an_adapter_without_the_capability_does_nothing() -> None:
    svc = _svc()
    svc.broker_adapter = object()
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == []


@pytest.mark.asyncio
async def test_a_failed_emit_RETRIES_rather_than_forfeiting_the_day() -> None:
    """⛔ NOT claim-once. The EOD transition claims before acting because it only releases a latch.
    This one places an order against an UNPROTECTED position, so a transient broker error must not
    silently skip it for the whole session."""
    svc = _svc()
    attempts = {"n": 0}

    async def _boom(*, acct, symbol, edge_et, rearm=False):
        attempts["n"] += 1
        raise RuntimeError("transient broker 500")

    svc._emit_v2_rth_edge_bracket = _boom
    await svc._v2_rth_edge_bracket()
    assert attempts["n"] == 1
    svc._v2_rth_edge_bracket_last_try.clear()      # simulate the 60s rate-limit elapsing
    await svc._v2_rth_edge_bracket()
    assert attempts["n"] == 2, "a failure must be retried, not latched"


@pytest.mark.asyncio
async def test_it_gives_up_LOUDLY_after_the_capped_attempts() -> None:
    """A position we could not bracket is something a human needs told about — not a debug line.
    It is not naked (the P0a-held software exit still owns it) and the message must say so."""
    svc = _svc()

    async def _boom(*, acct, symbol, edge_et, rearm=False):
        raise RuntimeError("broker keeps refusing")

    svc._emit_v2_rth_edge_bracket = _boom
    for _ in range(5):
        svc._v2_rth_edge_bracket_last_try.clear()
        await svc._v2_rth_edge_bracket()
    gave_up = [m for lvl, m in svc.logger.lines if lvl == "error" and "GAVE UP" in m]
    assert len(gave_up) == 1, "exactly one give-up line, not one per pass"
    assert "not naked" in gave_up[0]


@pytest.mark.asyncio
async def test_the_rate_limit_stops_a_failing_emit_from_hammering_the_broker() -> None:
    """The sweep runs every ~5s. Without the 60s gate a refusing broker would be retried 12x/min."""
    svc = _svc()

    async def _boom(*, acct, symbol, edge_et, rearm=False):
        raise RuntimeError("nope")

    svc._emit_v2_rth_edge_bracket = _boom
    await svc._v2_rth_edge_bracket()
    await svc._v2_rth_edge_bracket()          # immediately again -- must be suppressed
    assert svc._v2_rth_edge_bracket_attempts[
        (svc._session_day_et(), "live:schwab_1m_v2", "KUST")
    ] == 1


# ------------------------------------------------------------------ #646 Part 3: stand-down-clear
# ⭐⭐ THE CONSTRAINT. When a bracket resolves or stands down, the exit must re-arm a bracket or
# inherit the P0a marketable-hold -- it must NEVER fall back to the bare timer ladder. NVVE
# 2026-07-23 is the evidence the path is real, not theoretical: ELEVEN cancelled sells on a
# BRACKETED entry, because `[OMS-OCO-STAND-DOWN-CLEARED] ... ladder deferred` handed the exit back
# to the same refresh cadence that produced KUST.

def _rearm_svc(*, cleared_secs_ago: float | None, enabled: bool = True, **over):
    svc = _svc(oms_v2_stand_down_clear_rearm_enabled=enabled, **over)
    svc._native_oco_resolving = {}
    if cleared_secs_ago is not None:
        svc._native_oco_resolving[("live:schwab_1m_v2", "KUST")] = RTH_NOW - timedelta(
            seconds=cleared_secs_ago
        )
    return svc


def test_a_bracket_that_JUST_cleared_is_NOT_re_armed() -> None:
    """⛔⭐ THE ONE THAT PREVENTS AN OVERSELL. The COMMON reason a bracket clears is that a leg
    FILLED and the position is closing; OMS position state lags that fill by tens of seconds
    (Schwab fill -> positions runs to ~6 min). Re-arming inside that window places a fresh pair of
    sells against a position about to be flat -- the E5 shape the bracket exists to eliminate."""
    svc = _rearm_svc(cleared_secs_ago=5)
    assert svc._v2_stand_down_rearm_due("live:schwab_1m_v2", "KUST", now=RTH_NOW) is False


def test_a_bracket_cleared_LONGER_AGO_THAN_THE_GRACE_on_a_still_held_position_re_arms() -> None:
    """Grace elapsed AND still in the managed set => it did NOT resolve by a fill. That is NVVE."""
    svc = _rearm_svc(cleared_secs_ago=120)
    assert svc._v2_stand_down_rearm_due("live:schwab_1m_v2", "KUST", now=RTH_NOW) is True


def test_the_grace_boundary_is_the_configured_value_not_a_hardcode() -> None:
    """⛔ Pin the VALUE. If the resolution grace is retuned, the re-arm gate must move with it."""
    svc = _rearm_svc(cleared_secs_ago=120, oms_native_oco_resolve_grace_seconds=300)
    assert svc._v2_stand_down_rearm_due("live:schwab_1m_v2", "KUST", now=RTH_NOW) is False


def test_no_stand_down_means_no_re_arm() -> None:
    svc = _rearm_svc(cleared_secs_ago=None)
    assert svc._v2_stand_down_rearm_due("live:schwab_1m_v2", "KUST", now=RTH_NOW) is False


def test_the_rearm_flag_defaults_OFF_and_gates_the_path() -> None:
    assert Settings().oms_v2_stand_down_clear_rearm_enabled is False
    svc = _rearm_svc(cleared_secs_ago=120, enabled=False)
    assert svc._v2_stand_down_rearm_due("live:schwab_1m_v2", "KUST", now=RTH_NOW) is False


@pytest.mark.asyncio
async def test_a_re_arm_gets_a_FRESH_attempt_budget() -> None:
    """A position that exhausted its attempts at 09:30 must still be re-armable at 14:00 -- a
    stand-down is a NEW event, not a retry of the morning's arm. Without this, one bad open would
    leave the position on the bare ladder for the rest of the session."""
    svc = _rearm_svc(cleared_secs_ago=120)
    key = (svc._session_day_et(), "live:schwab_1m_v2", "KUST")
    svc._v2_rth_edge_bracket_done.add(key)
    svc._v2_rth_edge_bracket_attempts[key] = 99
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == [("live:schwab_1m_v2", "KUST")]


@pytest.mark.asyncio
async def test_a_re_arm_still_respects_broker_truth() -> None:
    """Re-arming a position the broker says is already bracketed would double-bracket it. The
    stand-down record is OUR belief; the broker is the truth."""
    svc = _rearm_svc(cleared_secs_ago=120, adapter=_Adapter(armed={"KUST"}))
    await svc._v2_rth_edge_bracket()
    assert svc.emitted == []

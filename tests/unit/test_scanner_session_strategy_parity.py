"""CONTROLLED literal-07eb strategy loop parity; no live warmup/native proof.

Only capture keys/approved UNKNOWN omissions differ. The scanner/alert/retention
methods and all guards run unchanged. Clock and capture I/O are explicit ports.
"""

from collections import defaultdict, deque
from contextlib import contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
from pathlib import Path
import struct
import sys
from types import BuiltinFunctionType, CodeType, FunctionType, MethodType, ModuleType
from zoneinfo import ZoneInfo

import pytest

from project_mai_tai.services import strategy_engine_app as current
from project_mai_tai.services import scanner_confirmed_capture as capture
from project_mai_tai.settings import Settings
from project_mai_tai.strategy_core import ReferenceData
from project_mai_tai.strategy_core import momentum_confirmed, snapshot_utils, top_gainers
from tests.unit.test_strategy_engine_service import make_snapshot_payload


ET = ZoneInfo("America/New_York")
BASE = "07ebcbaf12e76522e35e2809f0caaf43eb9b68f4"
APP_SHA = "73670c64058567f0097e92b2f0880e39f66e5f8eb750a70ac8c512d46f5de330"
CAPTURE_SHA = "204a5725100c04b4d5bf99cd11c8ad34aaf14db805f3110c156a39682f95721b"
FLAG = "scanner_confirmed_capture_session_day_enabled"
OLD_NAME = "scanner_literal_07eb_strategy"


def load_source(name, filename, checksum):
    path = Path(__file__).resolve().parents[1] / "fixtures/smartalert1" / filename
    assert not path.is_symlink()
    raw = path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == checksum
    if name not in sys.modules:
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), "exec"), module.__dict__)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def literal_old():
    old = load_source(OLD_NAME, "strategy_engine_app_07eb.py.txt", APP_SHA)
    dependency = load_source("scanner_literal_07eb_capture", "scanner_capture_07eb.py.txt", CAPTURE_SHA)
    old.capture_events = dependency.capture_events  # Literal OLD dependency, not NEW flag-OFF.
    return old


class Clock:
    def __init__(self, value):
        self.value = value
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.value


class CaptureOnly:
    """Only the unchanged capture SQL contract; no restoration/permission reads."""
    def __init__(self):
        self.calls = []
        self.commits = 0
        self.opens = 0
        self.fail = False
        self.unexpected = []

    @contextmanager
    def __call__(self):
        self.opens += 1
        yield self

    def execute(self, statement, params):
        names = {str(capture._COUNT_CONFIRM_SQL): "COUNT", str(capture._LATEST_EVENT_SQL): "LATEST",
                 str(capture._INSERT_SQL): "INSERT"}
        kind = names.get(str(statement))
        if kind is None:
            self.unexpected.append(str(statement))
            raise AssertionError("capture port cannot answer other APP queries")
        self.calls.append((kind, deepcopy(params)))
        if self.fail:
            raise RuntimeError("CONTROLLED_CAPTURE_IO_FAILURE")
        answer = 0 if kind == "COUNT" else None

        class Result:
            def scalar(self):
                return answer

        return Result()

    def commit(self):
        self.commits += 1


def state_bytes(state, *, clock, port):
    """Complete reachable instance graph, not selected summaries/empty dicts.

    Explicit exceptions: supplied clock/capture ports are compared separately;
    the ONE intended policy flag differs. Module filename/namespace and Python
    addresses are source-loading artifacts. No prices/IDs/fields are filtered.
    Unknown object types REFUSE rather than disappearing from the comparison.
    """
    seen = {}
    ports = {id(clock): "SUPPLIED_CLOCK", id(port): "CAPTURE_PORT"}

    def module_name(value):
        return current.__name__ if value == OLD_NAME else value

    def freeze(value):
        if value is None or type(value) in (bool, int, str):
            return [type(value).__name__, value]
        if isinstance(value, float):
            return ["float64bits", struct.pack("!d", value).hex()]
        if isinstance(value, Decimal):
            return ["Decimal", str(value)]
        if isinstance(value, datetime):
            return ["datetime", value.isoformat(), value.fold, getattr(value.tzinfo, "key", None)]
        if isinstance(value, date):
            return ["date", value.isoformat()]
        if isinstance(value, timedelta):
            return ["timedelta", value.days, value.seconds, value.microseconds]
        if isinstance(value, bytes):
            return ["bytes", value.hex()]
        if isinstance(value, Path):
            return ["Path", str(value)]
        if isinstance(value, ZoneInfo):
            return ["ZoneInfo", value.key]
        if isinstance(value, timezone):
            return ["timezone", value.tzname(None), freeze(value.utcoffset(None))]
        if isinstance(value, Enum):
            return [module_name(type(value).__module__), type(value).__qualname__, freeze(value.value)]
        if isinstance(value, CodeType):
            return ["code", value.co_code.hex(), freeze(value.co_consts), list(value.co_names),
                    list(value.co_varnames), list(value.co_freevars), list(value.co_cellvars)]
        if value is Ellipsis:
            return ["Ellipsis"]
        if id(value) in ports:
            return ["external_port", ports[id(value)]]
        if id(value) in seen:
            return ["ref", seen[id(value)]]
        seen[id(value)] = len(seen)
        tag = [module_name(type(value).__module__), type(value).__qualname__, seen[id(value)]]
        if isinstance(value, MethodType):
            return tag + [freeze(value.__func__), freeze(value.__self__)]
        if isinstance(value, FunctionType):
            return tag + [module_name(value.__module__), value.__qualname__, freeze(value.__code__),
                freeze(value.__defaults__), freeze(value.__kwdefaults__),
                [freeze(cell.cell_contents) for cell in value.__closure__ or ()]]
        if isinstance(value, (BuiltinFunctionType, type)):
            return tag + [module_name(value.__module__), value.__qualname__]
        if isinstance(value, defaultdict):
            return tag + [freeze(value.default_factory), [[freeze(k), freeze(v)] for k, v in value.items()]]
        if isinstance(value, dict):
            return tag + [[[freeze(k), freeze(v)] for k, v in value.items()]]
        if isinstance(value, deque):
            return tag + [value.maxlen, [freeze(v) for v in value]]
        if isinstance(value, (tuple, list)):
            return tag + [[freeze(v) for v in value]]
        if isinstance(value, (set, frozenset)):
            assert all(type(v) in (str, int, bool) or v is None for v in value), "unsupported state-set element"
            return tag + [sorted([freeze(v) for v in value], key=lambda v: json.dumps(v))]
        if isinstance(value, Settings):
            return tag + [[[k, freeze(v)] for k, v in vars(value).items() if k != FLAG],
                          freeze(value.__pydantic_fields_set__), freeze(value.__pydantic_extra__),
                          freeze(value.__pydantic_private__)]
        if type(value).__module__.startswith("numpy") and hasattr(value, "tobytes"):
            return tag + [str(value.dtype), list(value.shape), value.tobytes().hex()]
        if hasattr(value, "__dict__"):
            return tag + [[[k, freeze(v)] for k, v in vars(value).items()]]
        raise AssertionError("unsupported complete-state object: " + str(tag[:2]))

    return json.dumps(freeze(state), ensure_ascii=True, separators=(",", ":")).encode()


def candidate(symbol, clock="09:36:55 AM ET"):
    return dict(ticker=symbol, confirmed_at=clock, entry_price=3.2, price=3.2, prev_close=2.0,
        change_pct=60.0, volume=1000000, rvol=8.0, shares_outstanding=1000000,
        avg_daily_volume=100000, confirmation_path="PATH_C_EXTREME_MOVER", force_watchlist=True,
        rank_score=80.0, bid=3.19, ask=3.2, spread=0.01, spread_pct=0.32,
        hod=3.2, vwap=3.0, first_spike_time="09:30:00 AM ET", first_spike_price=2.0,
        squeeze_count=2, data_age_secs=0)


def run_world(module, *, enabled, start, clock_missing, db_error, retention, monkeypatch):
    clock, port = Clock(start), CaptureOnly()
    monkeypatch.setattr(module, "now_eastern", clock)
    monkeypatch.setattr(module, "utcnow", lambda: clock().astimezone(timezone.utc))
    monkeypatch.setattr(snapshot_utils, "now_eastern", clock)
    monkeypatch.setattr(top_gainers, "now_eastern", clock)
    settings = Settings(_env_file=None, scanner_confirmed_capture_enabled=True,
        scanner_confirmed_capture_session_day_enabled=enabled,
        scanner_feed_retention_enabled=retention, news_enabled=False,
        massive_api_key=None, strategy_history_persistence_enabled=False,
        strategy_macd_1m_taapi_indicator_source_enabled=False,
        strategy_macd_1m_massive_indicator_overlay_enabled=False,
        strategy_macd_30s_massive_indicator_overlay_enabled=False)
    # Real constructor, no external store/client/readability/entry overlay.
    state = module.StrategyEngineState(settings=settings, now_provider=clock, session_factory=None)
    assert state.session_factory is None and state.bots
    # Existing capture I/O port only; bots remain attached to their real None
    # constructor store, so this cannot manufacture ownership/entry permission.
    state.session_factory = port
    carry_time = "09:36:55 AM ET" if start.hour == 0 else "04:01:00"
    fade_time = "13:46:06" if start.hour == 0 else "04:01:00"
    seeds = [candidate("CARRY", "" if clock_missing else carry_time), candidate("FADE", fade_time)]
    state.seed_confirmed_candidates(deepcopy(seeds))
    state.restore_confirmed_runtime_view(deepcopy(seeds))
    state.alert_engine.prefill_history([
        {"FRESH": (2.0, 100000), "CARRY": (3.2, 1000000), "FADE": (3.2, 1000000)} for _ in range(20)])
    previous_watchlists = {code: set(bot.watchlist) for code, bot in state.bots.items()}
    assert any("FADE" in symbols for symbols in previous_watchlists.values())
    frames = []
    for index in range(3):
        clock.value = start + timedelta(seconds=index*5)
        port.fail = db_error and index == 1
        snapshots = [module.snapshot_from_payload(make_snapshot_payload(symbol=symbol, price=price,
                    volume=1000000+index*10000, previous_close=2.0, change_pct=change))
                    for symbol, price, change in (("FRESH", 3.2, 60.0), ("CARRY", 3.2, 60.0), ("FADE", 2.1, 5.0))]
        reference = {s: ReferenceData(shares_outstanding=1000000, avg_daily_volume=100000)
                     for s in ("FRESH", "CARRY", "FADE")}
        begin = len(port.calls)
        result = state.process_snapshot_batch(snapshots, reference)
        watchlists = {code: set(bot.watchlist) for code, bot in state.bots.items()}
        changes = {code: dict(added=sorted(symbols - previous_watchlists[code]),
                             removed=sorted(previous_watchlists[code] - symbols))
                   for code, symbols in watchlists.items()}
        frames.append(dict(output=deepcopy(result), state=state_bytes(state, clock=clock, port=port),
            capture=deepcopy(port.calls[begin:]), commits=port.commits, opens=port.opens, clock_calls=clock.calls,
            output_bytes=state_bytes(result, clock=clock, port=port),
            watchlists={code: sorted(symbols) for code, symbols in watchlists.items()}, add_remove=changes,
            confirmed=deepcopy(state.confirmed_scanner.get_all_confirmed())))
        previous_watchlists = watchlists
    assert not port.unexpected and state.cycle_count == 3
    assert any(frame["output"]["watchlist"] for frame in frames), "must exercise real nonempty watchlist"
    assert any(row["ticker"] == "FRESH" for row in frames[0]["output"]["newly_confirmed"]), "real alerts must confirm"
    assert "CARRY" in frames[0]["output"]["watchlist"] and "FADE" not in frames[0]["output"]["watchlist"]
    assert any(kind == "INSERT" and params["event_type"] == "FADE" for kind, params in frames[0]["capture"])
    assert any("FADE" in change["removed"] for change in frames[0]["add_remove"].values())
    if not retention:
        assert any("FRESH" in change["added"] for change in frames[0]["add_remove"].values())
    return frames


def comparable(frame):
    return {key: value for key, value in frame.items() if key != "capture"}


@pytest.mark.parametrize("retention", [False, True])
@pytest.mark.parametrize("hour", [0, 9, 13, 16])
def test_literal_old_off_on_complete_snapshot_outputs_and_state_only_capture_keys_differ(tmp_path, monkeypatch, retention, hour):
    monkeypatch.chdir(tmp_path)  # Real constructor's relative history reads stay private/empty, never host data.
    start = datetime(2026, 10, 27, hour, 2, tzinfo=ET)
    old = run_world(literal_old(), enabled=False, start=start, clock_missing=False,
                    db_error=False, retention=retention, monkeypatch=monkeypatch)
    off = run_world(current, enabled=False, start=start, clock_missing=False,
                    db_error=False, retention=retention, monkeypatch=monkeypatch)
    on = run_world(current, enabled=True, start=start, clock_missing=False,
                   db_error=False, retention=retention, monkeypatch=monkeypatch)
    for baseline, disabled, enabled in zip(old, off, on):
        assert comparable(baseline) == comparable(disabled) == comparable(enabled)
        assert baseline["capture"] == disabled["capture"]
        assert len(baseline["capture"]) == len(enabled["capture"])
        for (old_kind, old_params), (new_kind, new_params) in zip(baseline["capture"], enabled["capture"]):
            assert old_kind == new_kind
            assert {k: v for k, v in old_params.items() if k not in {"trade_date", "event_at"}} == {
                k: v for k, v in new_params.items() if k not in {"trade_date", "event_at"}}
            if hour >= 4:
                assert old_params == new_params
            else:
                assert new_params["trade_date"] == date(2026, 10, 26)
                if new_kind == "INSERT" and new_params["symbol"] == "CARRY":
                    assert new_params["event_at"] == datetime(2026, 10, 26, 9, 36, 55, tzinfo=ET)
    assert any(any(kind == "INSERT" and p["event_type"] == "CONFIRM" for kind, p in f["capture"]) for f in on)


@pytest.mark.parametrize("fault", ["unknown_clock", "db_error"])
def test_unknown_clock_and_capture_db_error_never_change_real_scanner_loop(tmp_path, monkeypatch, fault):
    monkeypatch.chdir(tmp_path)
    kwargs = dict(start=datetime(2026, 10, 27, 0, 2, tzinfo=ET), clock_missing=fault == "unknown_clock",
                  db_error=fault == "db_error", retention=False, monkeypatch=monkeypatch)
    old = run_world(literal_old(), enabled=False, **kwargs)
    on = run_world(current, enabled=True, **kwargs)
    for before, after in zip(old, on):
        # This nonempty fixture retains a proven FRESH row even when CARRY's
        # clock is unknown; opens/commits and all real state remain comparable.
        assert comparable(before) == comparable(after)
        assert len(after["capture"]) <= len(before["capture"])
    if fault == "db_error":
        assert old[1]["commits"] == old[0]["commits"] and on[1]["commits"] == on[0]["commits"]
        assert on[2]["commits"] > on[1]["commits"]  # Real loop recovers next iteration, no forced status.
    else:
        assert all(not (kind == "INSERT" and p["symbol"] == "CARRY") for f in on for kind, p in f["capture"])
        assert all("CARRY" in f["output"]["watchlist"] for f in on)


def test_complete_state_capture_keeps_private_fields_ids_prices_and_container_order():
    class State:
        def __init__(self):
            self._owner_id = "exact-owner"
            self.price = Decimal("1.23000000000000000001")
            self._bars = deque([1, 2], maxlen=5)

    state = State()
    clock, port = Clock(datetime(2026, 10, 27, 9, tzinfo=ET)), CaptureOnly()
    original = state_bytes(state, clock=clock, port=port)
    state._owner_id = "different-owner"
    assert state_bytes(state, clock=clock, port=port) != original
    state._owner_id = "exact-owner"
    state.price = Decimal("1.23000000000000000002")
    assert state_bytes(state, clock=clock, port=port) != original
    state.price = Decimal("1.23000000000000000001")
    state._bars.reverse()
    assert state_bytes(state, clock=clock, port=port) != original

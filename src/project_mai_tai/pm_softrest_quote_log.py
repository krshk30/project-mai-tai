"""Best-effort PM stream diagnostics. Never a trading decision or quote source.

One scalar record per already-existing armed symbol; no retained tick/event
objects, tasks, timers, providers, DB or independent subscription membership.
All public observation operations contain ordinary diagnostic exceptions.
Process-control BaseExceptions are not converted into trading permissions.
"""

from dataclasses import dataclass
from datetime import timezone
import json
import math
import re
import threading
import time
from zoneinfo import ZoneInfo


FLAG = "strategy_schwab_1m_v2_pm_softrest_quote_log_enabled"
TAG = "[V2-PM-SOFTREST-QUOTE]"
ET = ZoneInfo("America/New_York")
UNKNOWN = "UNKNOWN"
DECISIONS = frozenset({"REFUSED", "DRAFT_CREATED", "SCHEDULED", "ERROR"})
REASONS = frozenset({
    "falseflip_held", "stream_mode_disabled", "rest_not_active", "gap_hold",
    "not_premarket", "invalid_print", "print_not_fresh", "trigger_price_or_latch",
    "no_fresh_ask", "ask_past_band", "line_not_ready", "eh_mode_disabled", "boot_hold",
    "not_eh_session", "invalid_rest_level", "price_domain_refused", "position_or_latch",
    "entry_window_refused", "bar_missing_or_stale", "below_trigger", "confirming_ask_refused",
    "sizing_refused", "native_trade_time_missing_or_mismatch", "strategy_returned_none",
    "strategy_error", "draft_created", "schedule_error", "draft_scheduled",
})
FIELDS = frozenset({
    "trigger", "cap", "effective_ask", "ask_age_ms", "print_age_ms", "decision_now_ms",
    "placed_wire_trigger", "placed_trigger", "placed_line", "placed_wire_cap", "placed_legacy_cap",
})


def _number(value):
    if type(value) not in (int, float, str) or isinstance(value, str) and len(value) > 64:
        return UNKNOWN
    try:
        result = float(value)
        return repr(result) if math.isfinite(result) else UNKNOWN
    except Exception:
        return UNKNOWN


def _clock(value):
    if type(value) is str and len(value) <= 20 and value.isdecimal():
        value = int(value)
    # Diagnostics have no authority to interpret a nonpositive or huge clock.
    return value if type(value) is int and 0 < value < 10**16 else UNKNOWN


def _scalar(value):
    # Do not retain arbitrary raw objects or long SDK text, even until finish().
    return value if type(value) in (int, float) or type(value) is str and len(value) <= 64 else None


@dataclass
class _Record:
    day: str = ""
    attempted_ns: int | None = None
    decision: tuple[str, str] | None = None
    ask_message_ms: int | None = None
    ask_receive: str | None = None
    ask_cache_clock_ms: int | None = None


class Observation:
    def __init__(self, book, symbol, received, monotonic_ns, kind, fields):
        self._book, self._symbol = book, symbol
        self._received, self._monotonic_ns, self._kind = received, monotonic_ns, kind
        self._fields = fields
        record = book._records[symbol]
        self._ask_provenance = (record.ask_message_ms, record.ask_receive, record.ask_cache_clock_ms)
        self._decision, self._reason = "NOT_EVALUATED", "quote_only" if kind == "quote" else "cross_not_called"
        self._finished = False

    def note(self, decision, reason):
        try:
            if decision in DECISIONS and reason in REASONS:
                self._decision, self._reason = decision, reason
        except Exception:
            pass

    def none_if_unset(self):
        try:
            if self._decision == "NOT_EVALUATED":
                self.note("REFUSED", "strategy_returned_none")
        except Exception:
            pass

    def values(self, **fields):
        try:
            for key, value in fields.items():
                if key in FIELDS:
                    self._fields[key] = _scalar(value)
        except Exception:
            pass

    def ask_updated(self, cache_clock_ms):
        try:
            record = self._book._records[self._symbol]
            record.ask_message_ms = self._fields.get("schwab_message_ts_ms")
            record.ask_receive = self._received
            record.ask_cache_clock_ms = _clock(cache_clock_ms)
            self._ask_provenance = (record.ask_message_ms, record.ask_receive, record.ask_cache_clock_ms)
        except Exception:
            pass

    def ask_invalidated(self):
        try:
            record = self._book._records[self._symbol]
            record.ask_message_ms = record.ask_receive = record.ask_cache_clock_ms = None
            self._ask_provenance = (None, None, None)
        except Exception:
            pass

    def finish(self):
        try:
            if self._finished:
                return
            self._finished = True
            self._book._emit(self)
        except Exception:
            pass


class PmSoftRestQuoteLog:
    def __init__(self, logger, *, monotonic_ns=time.monotonic_ns):
        self._logger, self._clock = logger, monotonic_ns
        self._owner = threading.get_ident()
        self._records = {}
        self.failures = 0

    def begin(self, symbol, *, received_at, kind, service, bid, ask, last,
              message_ts_ms, trade_ts_ms, placed):
        try:
            if (threading.get_ident() != self._owner or type(symbol) is not str
                    or re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,15}", symbol) is None
                    or service != "LEVELONE_EQUITIES" or kind not in {"trade", "quote"}
                    or received_at.tzinfo is None or received_at.utcoffset() is None):
                return None
            et = received_at.astimezone(ET)
            if not 7 * 60 <= et.hour * 60 + et.minute < 9 * 60 + 30:
                return None
            at = self._clock()
            if type(at) is not int or at < 0:
                return None
            day = et.date().isoformat()
            previous = self._records.get(symbol)
            if previous is None or previous.day != day:
                self._records[symbol] = _Record(day=day)
            fields = dict(bid=_scalar(bid), ask=_scalar(ask), last=_scalar(last),
                schwab_message_ts_ms=_clock(message_ts_ms), trade_ts_ms=_clock(trade_ts_ms))
            fields.update({key: _scalar(value) for key, value in placed.items() if key in FIELDS})
            return Observation(self, symbol, received_at.astimezone(timezone.utc).isoformat(), at, kind, fields)
        except Exception:
            return None

    def forget(self, symbol):
        try:
            if threading.get_ident() == self._owner:
                self._records.pop(symbol, None)
        except Exception:
            pass

    def clear(self):
        try:
            if threading.get_ident() == self._owner:
                self._records.clear()
        except Exception:
            pass

    def _emit(self, observation):
        try:
            if threading.get_ident() != self._owner:
                return
            record = self._records[observation._symbol]
            pair = (observation._decision, observation._reason)
            actual = observation._decision in DECISIONS
            changed = actual and pair != record.decision
            periodic = record.attempted_ns is None or observation._monotonic_ns - record.attempted_ns >= 1_000_000_000
            if not (changed or periodic):
                return
            # Attempt watermark also bounds a repeatedly failing logger. Quote-
            # only heartbeats do not overwrite the actual decision-change key.
            record.attempted_ns = observation._monotonic_ns
            if actual:
                record.decision = pair
            fields = observation._fields
            ask_message_ms, ask_receive, ask_cache_clock_ms = observation._ask_provenance
            row = dict(symbol=observation._symbol, source="schwab_levelone", kind=observation._kind,
                receive_ts_utc=observation._received,
                schwab_message_ts_ms=fields["schwab_message_ts_ms"],
                trade_ts_ms=fields["trade_ts_ms"], quote_event_ts_ms=UNKNOWN,
                quote_clock="schwab_message" if _clock(ask_message_ms) != UNKNOWN else UNKNOWN,
                quote_ts_ms=_clock(ask_message_ms),
                ask_receive_ts_utc=ask_receive or UNKNOWN,
                ask_cache_clock_ms=_clock(ask_cache_clock_ms),
                ask_source="stream_ask_cache" if fields.get("effective_ask") is not None else UNKNOWN,
                decision=pair[0], reason=pair[1])
            for key in ("bid", "ask", "last", *sorted(FIELDS)):
                value = fields.get(key)
                row[key] = _clock(value) if key == "decision_now_ms" else _number(value)
            self._logger.info("%s %s", TAG, json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))
        except Exception:
            try:
                self.failures += 1
            except Exception:
                pass


def make_log(settings, logger):
    """Default OFF, with no helper allocation or clocks when the switch is OFF."""
    try:
        return PmSoftRestQuoteLog(logger) if getattr(settings, FLAG, False) is True else None
    except Exception:
        return None


def observe(observation, operation, *args, **kwargs):
    """Contain even a failing observer method; all operations are source literals."""
    try:
        if observation is not None and operation in {
                "note", "none_if_unset", "values", "ask_updated", "ask_invalidated",
                "finish", "forget", "clear"}:
            getattr(observation, operation)(*args, **kwargs)
    except Exception:
        pass

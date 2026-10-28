"""L12 admission evidence. Pure bounded memory; no broker, DB, or Redis calls.

Broker absence is meaningful only inside an explicitly complete two-account read.
An epoch/pass change invalidates prior negatives; a local send reservation never
expires merely because a later native read does not contain the order yet.
"""

from dataclasses import asdict, dataclass
import json
from threading import Lock

MAX_SYMBOLS = 4096
MAX_ACCOUNTS = 2
MAX_PACKET_BYTES = 512 * 1024
SCHEMA = 3


def symbols(values):
    result = frozenset(values)
    if len(result) > MAX_SYMBOLS or any(
        not isinstance(v, str) or not v or len(v) > 32 or v != v.upper()
        for v in result
    ):
        raise ValueError("symbol_coverage_UNKNOWN")
    return result


@dataclass(frozen=True)
class NativeAccountBook:
    account: str
    provider: str
    wire_started_ms: int
    wire_completed_ms: int
    working: frozenset[str]
    positions: frozenset[str]
    complete: bool
    reason: str
    scope_policy: str = "all_native_working_including_previous_sessions"
    window_days: int | None = None

    def __post_init__(self):
        if (not isinstance(self.account, str) or not self.account.startswith("live:")
                or self.provider not in {"schwab", "webull"}
                or type(self.wire_started_ms) is not int
                or type(self.wire_completed_ms) is not int
                or not 0 < self.wire_started_ms <= self.wire_completed_ms
                or type(self.complete) is not bool
                or not isinstance(self.reason, str) or len(self.reason) > 128
                or not isinstance(self.working, frozenset)
                or not isinstance(self.positions, frozenset)):
            raise ValueError("native_book_identity_UNKNOWN")
        symbols(self.working)
        symbols(self.positions)
        if self.scope_policy == "schwab_reviewed_window_20261022_145124":
            if self.provider != "schwab" or type(self.window_days) is not int or not 1 <= self.window_days <= 181:
                raise ValueError("Schwab_reviewed_window_UNKNOWN")
        elif self.scope_policy != "all_native_working_including_previous_sessions" or self.window_days is not None:
            raise ValueError("native_scope_policy_UNKNOWN")


@dataclass(frozen=True)
class AdmissionSnapshot:
    epoch: str
    generation: int
    started_ms: int
    completed_ms: int
    books: tuple[NativeAccountBook, ...]
    inflight: frozenset[str]
    filled_segments: frozenset[tuple[str, int]]
    local_complete: bool
    reason: str
    terminal_reservations: frozenset[tuple[str, str]] = frozenset()

    def __post_init__(self):
        if (not isinstance(self.epoch, str) or not self.epoch or len(self.epoch) > 64
                or type(self.generation) is not int or self.generation <= 0
                or type(self.started_ms) is not int or type(self.completed_ms) is not int
                or not 0 < self.started_ms <= self.completed_ms
                or not isinstance(self.books, tuple) or len(self.books) > MAX_ACCOUNTS
                or any(not isinstance(b, NativeAccountBook) for b in self.books)
                or not isinstance(self.inflight, frozenset)
                or not isinstance(self.filled_segments, frozenset)
                or not isinstance(self.terminal_reservations, frozenset)
                or type(self.local_complete) is not bool
                or not isinstance(self.reason, str) or len(self.reason) > 128):
            raise ValueError("snapshot_shape_UNKNOWN")
        symbols(self.inflight)
        if len(self.filled_segments) > MAX_SYMBOLS:
            raise ValueError("fill_census_overflow")
        for item in self.filled_segments:
            if (not isinstance(item, tuple) or len(item) != 2
                    or type(item[1]) is not int or item[1] <= 0):
                raise ValueError("fill_segment_UNKNOWN")
            symbols((item[0],))
        if len(self.terminal_reservations) > MAX_SYMBOLS:
            raise ValueError("terminal_census_overflow")
        for item in self.terminal_reservations:
            if (not isinstance(item, tuple) or len(item) != 2
                    or not isinstance(item[1], str) or not item[1] or len(item[1]) > 128):
                raise ValueError("terminal_identity_UNKNOWN")
            symbols((item[0],))


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str
    oldest_age_ms: int | None


def assess(snapshot, *, accounts, symbol, segment, now_ms, max_age_ms):
    """No moving clock or I/O: caller supplies the decision time and reviewed bound."""
    if (type(now_ms) is not int or type(max_age_ms) is not int or max_age_ms <= 0):
        return Decision(False, "age_policy_UNREVIEWED", None)
    if snapshot is None:
        return Decision(False, "snapshot_missing", None)
    expected = tuple(accounts)
    if (len(expected) != MAX_ACCOUNTS or len(set(expected)) != MAX_ACCOUNTS
            or len(snapshot.books) != MAX_ACCOUNTS
            or {b.account for b in snapshot.books} != set(expected)
            or {b.provider for b in snapshot.books} != {"schwab", "webull"}):
        return Decision(False, "account_coverage_UNKNOWN", None)
    if (not snapshot.epoch or snapshot.generation <= 0 or not snapshot.local_complete
            or not 0 < snapshot.started_ms <= snapshot.completed_ms <= now_ms):
        return Decision(False, "snapshot_UNKNOWN", None)
    age = now_ms - min(b.wire_started_ms for b in snapshot.books)
    if (any(not b.complete or b.wire_completed_ms > snapshot.completed_ms
            or b.wire_started_ms < snapshot.started_ms for b in snapshot.books)):
        return Decision(False, "native_coverage_UNKNOWN", age)
    if age < 0 or age >= max_age_ms:
        return Decision(False, "snapshot_stale", age)
    if type(segment) is not int or segment <= 0:
        return Decision(False, "fill_segment_UNKNOWN", age)
    if any(symbol in b.working for b in snapshot.books):
        return Decision(False, "native_working_order", age)
    if any(symbol in b.positions for b in snapshot.books):
        return Decision(False, "native_position", age)
    if symbol in snapshot.inflight:
        return Decision(False, "own_inflight_UNKNOWN_or_working", age)
    if (symbol, segment) in snapshot.filled_segments:
        return Decision(False, "buy_fill_in_segment", age)
    return Decision(True, "broker_flat_no_inflight_no_segment_fill", age)


def encode(snapshot):
    payload = asdict(snapshot)
    payload["schema"] = SCHEMA
    payload["inflight"] = sorted(snapshot.inflight)
    payload["filled_segments"] = sorted(snapshot.filled_segments)
    payload["terminal_reservations"] = sorted(snapshot.terminal_reservations)
    for book in payload["books"]:
        book["working"] = sorted(book["working"])
        book["positions"] = sorted(book["positions"])
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode()) > MAX_PACKET_BYTES:
        raise ValueError("snapshot_bytes_overflow")
    return raw


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_snapshot_key")
        result[key] = value
    return result


def decode(raw):
    if not isinstance(raw, (str, bytes)) or len(raw) > MAX_PACKET_BYTES:
        raise ValueError("snapshot_bytes_UNKNOWN")
    if isinstance(raw, str) and len(raw.encode()) > MAX_PACKET_BYTES:
        raise ValueError("snapshot_bytes_overflow")
    payload = json.loads(raw, object_pairs_hook=_unique_object)
    if (not isinstance(payload, dict) or type(payload.get("schema")) is not int
            or payload.pop("schema", None) != SCHEMA):
        raise ValueError("snapshot_schema_UNKNOWN")
    for field in ("books", "inflight", "filled_segments", "terminal_reservations"):
        if not isinstance(payload.get(field), list) or len(payload[field]) > MAX_SYMBOLS:
            raise ValueError("snapshot_collection_UNKNOWN")
    books = []
    for book in payload["books"]:
        if not isinstance(book, dict):
            raise ValueError("native_book_UNKNOWN")
        if not {"scope_policy", "window_days"}.issubset(book):
            raise ValueError("native_scope_policy_missing_UNKNOWN")
        for field in ("working", "positions"):
            values = book.get(field)
            if not isinstance(values, list) or len(values) != len(symbols(values)):
                raise ValueError("native_book_duplicate_or_overflow")
            book[field] = frozenset(values)
        books.append(NativeAccountBook(**book))
    payload["books"] = tuple(books)
    if len(payload["inflight"]) != len(symbols(payload["inflight"])):
        raise ValueError("inflight_duplicate")
    payload["inflight"] = frozenset(payload["inflight"])
    fills = payload["filled_segments"]
    if any(not isinstance(v, list) or len(v) != 2 for v in fills):
        raise ValueError("fill_shape_UNKNOWN")
    payload["filled_segments"] = frozenset(tuple(v) for v in fills)
    if len(fills) != len(payload["filled_segments"]):
        raise ValueError("fill_duplicate")
    terminals = payload["terminal_reservations"]
    if any(not isinstance(v, list) or len(v) != 2 for v in terminals):
        raise ValueError("terminal_shape_UNKNOWN")
    payload["terminal_reservations"] = frozenset(tuple(v) for v in terminals)
    if len(terminals) != len(payload["terminal_reservations"]):
        raise ValueError("terminal_duplicate")
    return AdmissionSnapshot(**payload)


class NativeBookPublisher:
    """Single pass publisher. Failed/in-progress passes replace old flat evidence."""

    def __init__(self, *, epoch, accounts):
        self.accounts = tuple(accounts)
        if (not isinstance(epoch, str) or not epoch or len(epoch) > 64
                or len(self.accounts) != MAX_ACCOUNTS
                or len(set(self.accounts)) != MAX_ACCOUNTS):
            raise ValueError("publisher_identity_UNKNOWN")
        self.epoch = epoch
        self.generation = 0
        self.snapshot = None
        self._lock = Lock()

    def begin(self, started_ms):
        with self._lock:
            self.generation += 1
            self.snapshot = AdmissionSnapshot(self.epoch, self.generation,
                started_ms, started_ms, (), frozenset(), frozenset(), False, "pass_in_progress")
            return self.generation

    def finish(self, generation, *, completed_ms, books, inflight, filled_segments,
               local_complete, reason, terminal_reservations=frozenset()):
        with self._lock:
            if generation != self.generation:
                raise ValueError("superseded_sync_pass")
            fills = frozenset(filled_segments)
            if len(fills) > MAX_SYMBOLS or any(
                    not isinstance(s, str) or type(n) is not int or n <= 0 for s, n in fills):
                raise ValueError("fill_census_UNKNOWN")
            if len(books) > MAX_ACCOUNTS or type(local_complete) is not bool:
                raise ValueError("coverage_UNKNOWN")
            self.snapshot = AdmissionSnapshot(self.epoch, generation,
                self.snapshot.started_ms, completed_ms, tuple(books), symbols(inflight),
                fills, local_complete, reason, frozenset(terminal_reservations))
            return self.snapshot


class AdmissionMemory:
    """Consumer-local send overlay; broker negatives cannot erase a reservation.

    Explicit terminal proof must resolve its exact token. Restart requires a
    complete durable in-flight census from the publisher before admission.
    """

    def __init__(self):
        self.snapshot = None
        self._reservations = {}

    def apply(self, snapshot):
        current = self.snapshot
        if (current is not None and current.epoch == snapshot.epoch
                and snapshot.generation < current.generation):
            raise ValueError("snapshot_generation_regressed")
        if (current is not None and current.epoch == snapshot.epoch
                and current.generation == snapshot.generation and current.local_complete
                and current != snapshot):
            raise ValueError("completed_generation_changed")
        if current is not None and current.epoch != snapshot.epoch and snapshot.started_ms < current.started_ms:
            raise ValueError("publisher_epoch_regressed")
        self.snapshot = snapshot
        if (snapshot.local_complete and len(snapshot.books) == MAX_ACCOUNTS
                and all(b.complete for b in snapshot.books)
                and len({b.account for b in snapshot.books}) == MAX_ACCOUNTS
                and {b.provider for b in snapshot.books} == {"schwab", "webull"}
                and current is not None
                and {b.account for b in current.books} == {b.account for b in snapshot.books}):
            for symbol, token in snapshot.terminal_reservations:
                self.terminal(symbol, token, proven=True)

    def assess(self, **kwargs):
        if kwargs["symbol"] in self._reservations:
            return Decision(False, "local_send_reserved", None)
        return assess(self.snapshot, **kwargs)

    def reserve(self, symbol, token):
        if not token or symbol in self._reservations or len(self._reservations) >= MAX_SYMBOLS:
            raise ValueError("send_reservation_UNKNOWN")
        self._reservations[symbol] = token

    def reservation_tokens(self):
        return frozenset(self._reservations.values())

    def terminal(self, symbol, token, *, proven):
        if proven is not True or self._reservations.get(symbol) != token:
            return False
        del self._reservations[symbol]
        return True

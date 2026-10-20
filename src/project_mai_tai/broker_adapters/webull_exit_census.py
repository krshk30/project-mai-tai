"""Exit-only complete today-list proof; ordinary single-order reads are unchanged."""
from dataclasses import dataclass, field
from datetime import UTC, datetime
import threading
import time
from zoneinfo import ZoneInfo

from project_mai_tai.broker_adapters.webull_order_reads import QueryBudgetUnavailable


@dataclass
class _Scan:
    session: str
    rows: dict = field(default_factory=dict)
    cursors: set = field(default_factory=set)
    cursor: str = ""
    pages: int = 0
    cycle_pages: int = 0
    cycle_at: float = -float("inf")
    first_at: datetime | None = None
    first_clock: float | None = None
    complete: bool = False
    failed: bool = False


class ExitCensusReader:
    def __init__(self, budget, cycle_seconds, clock=time.monotonic):
        self.budget, self.clock = budget, clock
        self.cycle_seconds = max(2., float(cycle_seconds))
        self.lock, self.scans = threading.Lock(), {}

    def read(self, account_id, fetch_page, *, after=None):
        with self.lock:
            now = self.clock()
            session = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            scan = self.scans.get(account_id)
            if (scan is None or scan.session != session or scan.complete or scan.failed
                    or after is not None and scan.first_at is not None and scan.first_at <= after):
                scan = self.scans[account_id] = _Scan(session)
            if now - scan.cycle_at >= self.cycle_seconds:
                scan.cycle_at, scan.cycle_pages = now, 0
            try:
                while scan.cycle_pages < 2:
                    try:
                        self.budget.claim("list-today", account_id)
                    except QueryBudgetUnavailable:
                        return None
                    scan.cycle_pages += 1
                    if scan.first_at is None:
                        scan.first_clock, scan.first_at = self.clock(), datetime.now(UTC)
                    status, body = fetch_page(scan.cursor)
                    acquired, stamp = self.clock(), datetime.now(UTC)
                    if (status != 200 or not isinstance(body, dict) or body.get("error_code")
                            or any(str(body[k]) != account_id for k in ("account_id", "accountId")
                                   if body.get(k) is not None)):
                        raise ValueError("exit census page unreadable or foreign")
                    rows = body.get("orders")
                    continuations = [body[k] for k in ("has_next", "hasNext") if k in body]
                    if (not isinstance(rows, list) or len(rows) > 100 or not continuations
                            or any(type(v) is not bool or v != continuations[0] for v in continuations)):
                        raise ValueError("exit census continuation unknown")
                    for raw in rows:
                        if not isinstance(raw, dict):
                            raise ValueError("exit census row unreadable")
                        ids = [raw[k] for k in ("client_order_id", "clientOrderId") if k in raw]
                        if (not ids or any(not isinstance(v, str) or not v or v != v.strip()
                                           or v != ids[0] for v in ids)
                                or any(str(raw[k]) != account_id for k in ("account_id", "accountId")
                                       if raw.get(k) is not None)):
                            raise ValueError("exit census row identity unknown")
                        coid = ids[0]
                        if coid in scan.rows and scan.rows[coid] != raw:
                            raise ValueError("exit census conflicting duplicate")
                        scan.rows[coid] = raw
                    scan.pages += 1
                    if not continuations[0]:
                        scan.complete = True
                        if (after is not None and scan.first_at <= after
                                or not 0 <= acquired - scan.first_clock <= 30.):
                            raise ValueError("exit census acquisition stale")
                        return {"account_id": account_id, "complete": True, "truncated": False,
                                "pages": scan.pages, "started_at": scan.first_at.isoformat(),
                                "acquired_at": stamp.isoformat(), "orders": list(scan.rows.values())}
                    cursor = rows[-1].get("client_order_id", rows[-1].get("clientOrderId")) if rows else ""
                    if not cursor or cursor == scan.cursor or cursor in scan.cursors or scan.pages >= 20:
                        raise ValueError("exit census nonprogress or page cap")
                    scan.cursor = cursor
                    scan.cursors.add(cursor)
            except Exception:
                scan.failed = True
                raise
            return None  # Resume bounded traversal on the existing caller cadence only.


_READERS = {}
_LOCK = threading.Lock()


def shared_exit_census(host, app_key, budget, cycle_seconds):
    with _LOCK:
        return _READERS.setdefault((host, app_key), ExitCensusReader(budget, cycle_seconds))

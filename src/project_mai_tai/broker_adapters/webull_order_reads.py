"""Bounded list traversal and restart-safe terminal query evidence.

The rolling ceiling is a conservative application policy. The legacy Webull
endpoint's actual quota is unmeasured. All permits count HTTP attempts, not scans.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from sqlalchemy.exc import IntegrityError

from project_mai_tai.db.models import DashboardSnapshot


class QueryBudgetUnavailable(RuntimeError):
    """No query permit now; callers retain UNKNOWN without blocking a deadline."""


class QueryBudget:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.attempts: dict[str, deque[float]] = {}
        self.waiting: dict[str, deque[str]] = {}
        self.last_wait: dict[tuple[str, str], float] = {}
        self.terminal_lock = threading.Lock()
        self.terminal_inflight: set[tuple[str, str, str]] = set()

    def claim(self, endpoint: str, owner: str, *, strict: bool = False) -> None:
        with self.lock:
            now = self.clock()
            attempts = self.attempts.setdefault(endpoint, deque())
            while attempts and now - attempts[0] >= 2.0:
                attempts.popleft()
            queue = self.waiting.setdefault(endpoint, deque())
            # A removed order/account must not leave an immortal queue head.
            for queued in list(queue):
                if now - self.last_wait.get((endpoint, queued), now) >= 30.0:
                    queue.remove(queued)
                    self.last_wait.pop((endpoint, queued), None)
            if owner not in queue:
                queue.append(owner)
            self.last_wait[endpoint, owner] = now
            # Reserve the second detail permit for fresh RPG/EOD/cancel proof.
            ceiling = 1 if endpoint == "detail" and not strict else 2
            if len(attempts) >= ceiling or (not strict and queue[0] != owner):
                raise QueryBudgetUnavailable(f"{endpoint} query budget unavailable")
            queue.remove(owner)
            self.last_wait.pop((endpoint, owner), None)
            attempts.append(now)


_BUDGETS: dict[tuple[str, str], QueryBudget] = {}
_BUDGET_LOCK = threading.Lock()


def shared_budget(host: str, app_key: str) -> QueryBudget:
    # Share across accounts, aliases, threads, and adapter instances in this process.
    with _BUDGET_LOCK:
        return _BUDGETS.setdefault((host, app_key), QueryBudget())


_READERS: dict[tuple[str, str], TodayOrderReader] = {}


def shared_reader(host: str, app_key: str, cycle_seconds: float):
    budget = shared_budget(host, app_key)
    with _BUDGET_LOCK:
        return _READERS.setdefault((host, app_key), TodayOrderReader(budget, cycle_seconds))


@dataclass
class ListScan:
    session: str
    cursor: str = ""
    pages: int = 0
    complete: bool = False
    failed: bool = False
    cycle_at: float = -float("inf")
    cycle_pages: int = 0
    rows: dict[str, tuple[float, datetime, dict]] = field(default_factory=dict)
    cursors: set[str] = field(default_factory=set)


class TodayOrderReader:
    def __init__(self, budget: QueryBudget, cycle_seconds: float, clock=time.monotonic):
        self.budget = budget
        self.cycle_seconds = max(2.0, float(cycle_seconds))
        self.clock = clock
        self.lock = threading.Lock()
        self.scans: dict[str, ListScan] = {}

    def read(self, account_id: str, client_id: str, fetch_page, *, fresh_seconds=2.0):
        with self.lock:
            now = self.clock()
            session = datetime.now(UTC).astimezone(ZoneInfo("America/New_York")).date().isoformat()
            scan = self.scans.get(account_id)
            if scan is None or scan.session != session:
                scan = self.scans[account_id] = ListScan(session)
            row = scan.rows.get(client_id)
            if row is not None and 0 <= now - row[0] < fresh_seconds:
                return self._evidence(account_id, client_id, scan, row)
            if now - scan.cycle_at >= self.cycle_seconds:
                scan.cycle_at, scan.cycle_pages = now, 0
                if scan.complete or scan.failed:
                    scan = self.scans[account_id] = ListScan(session, cycle_at=now)
            while scan.cycle_pages < 2 and not (scan.complete or scan.failed):
                try:
                    self.budget.claim("list-today", account_id)
                except QueryBudgetUnavailable:
                    break
                scan.cycle_pages += 1
                try:
                    status, body = fetch_page(scan.cursor)
                    acquired = self.clock()
                    acquired_at = datetime.now(UTC)
                    if status != 200 or not isinstance(body, dict) or body.get("error_code"):
                        raise ValueError("unreadable today-orders page")
                    rows = body.get("orders")
                    if not isinstance(rows, list) or len(rows) > 100:
                        raise ValueError("invalid today-orders rows")
                    has_next = body.get("has_next", body.get("hasNext", False))
                    if ("has_next" not in body and "hasNext" not in body) or not isinstance(
                        has_next, bool
                    ):
                        raise ValueError("invalid today-orders continuation")
                    page = {}
                    for raw in rows:
                        if not isinstance(raw, dict):
                            raise ValueError("invalid today-orders row")
                        coid = str(raw.get("client_order_id") or raw.get("clientOrderId") or "")
                        if not coid or any(
                            str(raw[k]) != account_id
                            for k in ("account_id", "accountId")
                            if raw.get(k) is not None
                        ):
                            raise ValueError("unbound today-orders row")
                        old = page.get(coid) or (scan.rows.get(coid) or (None, None, None))[2]
                        if old is not None and old != raw:
                            raise ValueError("conflicting today-orders identity")
                        page[coid] = raw
                    cursor = (
                        str(rows[-1].get("client_order_id") or rows[-1].get("clientOrderId") or "")
                        if rows
                        else ""
                    )
                    if has_next and (not cursor or cursor == scan.cursor or cursor in scan.cursors):
                        raise ValueError("today-orders cursor did not progress")
                    scan.pages += 1
                    scan.rows.update(
                        {coid: (acquired, acquired_at, raw) for coid, raw in page.items()}
                    )
                    scan.complete = not has_next
                    if has_next:
                        scan.cursor = cursor
                        scan.cursors.add(cursor)
                        if scan.pages >= 20:
                            scan.failed = True
                    row = scan.rows.get(client_id)
                    if row is not None and 0 <= self.clock() - row[0] < fresh_seconds:
                        return self._evidence(account_id, client_id, scan, row)
                except Exception:
                    # Keep no affirmative absence and never stamp old pages as freshly read.
                    scan.failed = True
                    raise
            return None

    @staticmethod
    def _evidence(account_id, client_id, scan, row):
        body = dict(row[2])
        body["_webull_list_evidence"] = {
            "account_id": account_id,
            "client_order_id": client_id,
            "session": scan.session,
            "acquired_at": row[1].isoformat(),
            "pages": scan.pages,
            "complete": scan.complete,
            "cursor": scan.cursor,
            "truncated": scan.failed,
        }
        return body


def terminal_version(account_id: str, client_id: str, body: dict) -> str:
    identity = {k: v for k, v in body.items() if not k.startswith("_webull_")}
    encoded = json.dumps([account_id, client_id, identity], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


class TerminalProofStore:
    """An immutable proof per evidence version; primary key makes commit retry idempotent."""

    def __init__(self, session_factory):
        self.session_factory = session_factory

    @staticmethod
    def identity(account_id, client_id, version):
        return uuid5(NAMESPACE_URL, f"webull-terminal:{account_id}:{client_id}:{version}")

    def load(self, account_id, client_id, version):
        with self.session_factory() as session:
            row = session.get(DashboardSnapshot, self.identity(account_id, client_id, version))
            if row is None or row.snapshot_type != "webull_terminal_read_proof":
                return None
            payload = row.payload
            if (
                payload.get("account_id"),
                payload.get("client_order_id"),
                payload.get("version"),
            ) != (account_id, client_id, version):
                return None
            return payload

    def save(self, account_id, client_id, version, proof):
        identity = self.identity(account_id, client_id, version)
        with self.session_factory() as session:
            session.add(
                DashboardSnapshot(
                    id=identity, snapshot_type="webull_terminal_read_proof", payload=proof
                )
            )
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self.load(account_id, client_id, version)
                # Acquired times and numeric formatting may differ across a crash
                # retry; the normalized execution and scope must remain identical.
                if (
                    existing is None
                    or existing.get("execution") != proof.get("execution")
                    or existing.get("source") != proof.get("source")
                ):
                    raise

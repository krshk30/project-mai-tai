"""Session-scoped entry membership and executable rest checkpoints for own restarts."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import logging
import math
import time

from sqlalchemy import delete, select, text

from project_mai_tai.db.models import DashboardSnapshot
from project_mai_tai.fanout_segment_store import current_session_anchor


SNAPSHOT_TYPE = "v2_restart_rest"
SCHEMA_VERSION = 1
RETENTION_DAYS = 7
logger = logging.getLogger(__name__)
REST_FIELDS = (
    "resting_active", "resting_level", "resting_trigger", "resting_slot",
    "last_resting_placed_slot", "resting_is_broker_order",
    "resting_schwab_wire_stop", "resting_webull_wire_stop",
    "resting_schwab_wire_limit", "resting_webull_wire_limit", "resting_wire_cap",
    "resting_schwab_quantity", "resting_webull_quantity",
    "resting_schwab_generation", "resting_webull_generation",
    "resting_schwab_retired_generation", "resting_webull_retired_generation",
    "webull_resting_active", "webull_resting_generation_id",
    "resting_below_floor_bars", "resting_flip_ms", "pm_resting_flip_seen_ms",
    "resting_buy_frozen", "resting_frozen_floor_bar_ms",
)


def checkpoint_state(state) -> dict:
    rest = {name: getattr(state, name) for name in REST_FIELDS}
    if state.restart_rest_evidence_pending and set(state.restart_rest_cancel_fields) == set(REST_FIELDS):
        rest = dict(state.restart_rest_cancel_fields)
    return {
        "sell_ms": int(state.restart_last_live_sell_ms),
        "opportunity_id": int(state.flip_owner_opportunity_id or state.fanout_segment_id),
        "cw_resting_taken": bool(state.cw_resting_taken),
        "cw_reclaim_taken": bool(state.cw_reclaim_taken),
        "cancel_pending": bool(state.restart_rest_evidence_pending),
        "rest": rest,
    }


class RestartRestStore:
    """One bounded fleet checkpoint; neither saved phases nor absence prove broker-flat."""

    def __init__(self, session_factory):
        self._session_factory = session_factory
        self._last_cleanup_anchor = None

    def record(self, payload: dict, *, now: datetime | None = None) -> None:
        observed = now or datetime.now(UTC)
        anchor_ms = int(current_session_anchor(observed).timestamp() * 1000)
        if payload["session_anchor_ms"] != anchor_ms:
            raise ValueError("checkpoint crossed session boundary")
        with self._session_factory() as session:
            if session.get_bind().dialect.name == "postgresql":
                session.execute(text("SET LOCAL lock_timeout = '250ms'"))
                session.execute(text("SET LOCAL statement_timeout = '2000ms'"))
            session.add(DashboardSnapshot(
                snapshot_type=SNAPSHOT_TYPE,
                created_at=observed,
                payload={**deepcopy(payload), "schema_version": SCHEMA_VERSION,
                         "strategy_code": "schwab_1m_v2"},
            ))
            if self._last_cleanup_anchor != anchor_ms:
                session.execute(delete(DashboardSnapshot).where(
                    DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                    DashboardSnapshot.created_at < observed - timedelta(days=RETENTION_DAYS),
                ))
            if now is None and current_session_anchor(datetime.now(UTC)) != current_session_anchor(observed):
                raise ValueError("checkpoint crossed session boundary before commit")
            session.commit()
            self._last_cleanup_anchor = anchor_ms

    def restore(self, *, now: datetime | None = None) -> dict | None:
        observed = now or datetime.now(UTC)
        anchor = current_session_anchor(observed)
        with self._session_factory() as session:
            row = session.scalar(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE,
                DashboardSnapshot.created_at >= anchor,
                DashboardSnapshot.created_at <= observed,
            ).order_by(DashboardSnapshot.created_at.desc(), DashboardSnapshot.id.desc()).limit(1))
            payload = deepcopy(row.payload) if row is not None else None
        if payload is None:
            return None
        anchor_ms = int(anchor.timestamp() * 1000)
        if (not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION
                or payload.get("strategy_code") != "schwab_1m_v2"
                or payload.get("session_anchor_ms") != anchor_ms
                or not isinstance(payload.get("cursor"), str)
                or not isinstance(payload.get("symbols"), dict)):
            raise ValueError("unreadable restart checkpoint")
        for symbol, saved in payload["symbols"].items():
            if (not symbol or symbol != symbol.upper() or not isinstance(saved, dict)
                    or not isinstance(saved.get("state"), dict)
                    or type(saved.get("watch_ms")) is not int
                    or not anchor_ms <= saved["watch_ms"] <= int(observed.timestamp() * 1000)):
                raise ValueError("invalid entry membership checkpoint")
            state = saved["state"]
            if (any(type(state.get(name)) is not int or state[name] < 0
                    for name in ("sell_ms", "opportunity_id"))
                    or any(type(state.get(name)) is not bool
                           for name in ("cw_resting_taken", "cw_reclaim_taken"))
                    or type(state.get("cancel_pending", False)) is not bool
                    or not isinstance(state.get("rest"), dict)
                    or set(state["rest"]) != set(REST_FIELDS)):
                raise ValueError("invalid rest checkpoint")
            for name, value in state["rest"].items():
                if isinstance(value, float) and (not math.isfinite(value) or value < 0):
                    raise ValueError("invalid rest price")
                if not isinstance(value, (str, bool, int, float)):
                    raise ValueError("invalid rest field")
                if name in {
                    "resting_active", "resting_is_broker_order",
                    "webull_resting_active", "resting_buy_frozen",
                } and type(value) is not bool:
                    raise ValueError("invalid rest latch")
                if ("generation" in name or name in {"resting_slot", "last_resting_placed_slot"}):
                    if not isinstance(value, str):
                        raise ValueError("invalid rest identity")
                elif name not in {
                    "resting_active", "resting_is_broker_order",
                    "webull_resting_active", "resting_buy_frozen",
                }:
                    if (type(value) not in {int, float} or not math.isfinite(value) or value < 0):
                        raise ValueError("invalid rest number")
            if state["rest"]["resting_slot"] not in {"first", "reclaim"}:
                raise ValueError("invalid rest slot")
        return payload


class RestartCheckpointWriter:
    """One in-flight immutable snapshot and one latest pending snapshot, off-loop SQL."""

    def __init__(self, store):
        self.store = store
        self.pending = None
        self.task = None
        self.closed = False
        self.last_signature = None
        self.last_saved_ms = 0
        self.failures = 0
        self.retry_at = 0.0
        self._closing = asyncio.Event()

    def submit(self, payload: dict) -> bool:
        if self.closed:
            return False
        # The caller makes fresh scalar-only state dictionaries on the loop.
        # Never read mutable strategy state from the database worker.
        self.pending = payload
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._write_latest(), name="v2_restart_checkpoint")
        return True

    def _record_if_changed(self, payload):
        meaningful = {key: payload[key] for key in ("session_anchor_ms", "symbols")}
        signature = json.dumps(meaningful, sort_keys=True)
        captured_ms = payload["captured_at_ms"]
        if signature == self.last_signature and captured_ms - self.last_saved_ms < 60_000:
            return None
        self.store.record(payload)
        return signature, captured_ms

    async def _write_latest(self):
        while self.pending is not None:
            delay = self.retry_at - time.monotonic()
            if delay > 0 and not self.closed:
                try:
                    await asyncio.wait_for(self._closing.wait(), timeout=delay)
                except TimeoutError:
                    pass
            payload, self.pending = self.pending, None
            try:
                saved = await asyncio.to_thread(self._record_if_changed, payload)
            except Exception:
                self.failures += 1
                self.retry_at = time.monotonic() + min(30.0, 2 ** min(self.failures-1, 5))
                if self.pending is None:
                    self.pending = payload
                logger.exception("[V2-RESTART-REST] checkpoint_failed=1 writer=%s sequence=%s "
                                 "pending_latest=1 retry_seconds=%.1f",
                                 payload.get("writer_instance_id"), payload.get("checkpoint_sequence"),
                                 self.retry_at-time.monotonic())
                if self.closed and self.pending is payload:
                    return
            else:
                self.failures, self.retry_at = 0, 0.0
                if saved is not None:
                    self.last_signature, self.last_saved_ms = saved

    async def drain(self, *, timeout: float = 3.0) -> bool:
        """Graceful close only, never called by a bar/quote/scanner producer."""
        self.closed = True
        self._closing.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), timeout=timeout)
            except TimeoutError:
                # Cancelling to_thread cannot stop its SQL; do not pretend it did.
                logger.error("[V2-RESTART-REST] checkpoint_drain_unproven=1 timeout_seconds=%s", timeout)
                return False
        if self.pending is not None:
            logger.error("[V2-RESTART-REST] checkpoint_drain_unproven=1 reason=write_failed")
            return False
        return True

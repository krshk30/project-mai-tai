from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, time as daytime
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
import os
import re
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from urllib.parse import quote

from sqlalchemy import func, or_, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
from project_mai_tai.broker_adapters.momentum_order_read import MomentumEntryLookup, MomentumOrderRead, MomentumReadUnavailable
from project_mai_tai.broker_adapters.webull_order_reads import QueryBudgetUnavailable
from project_mai_tai.db.models import BrokerAccount, BrokerOrder, DashboardSnapshot, Fill, Strategy, SystemIncident, VirtualPosition
from project_mai_tai.positions_read_receipt import SNAPSHOT_TYPE, load_receipts
from project_mai_tai.momentum_live.actor import ActorPorts, LiveActor, OwnedObservation
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, STRATEGY_CODE, Signal, select_route
from project_mai_tai.momentum_live.execution import ExecutionPorts, LiveEntryExecutor
from project_mai_tai.momentum_live.lifecycle import OwnedPosition
from project_mai_tai.momentum_live.owner_proof import OwnerProofPublisher, current_process_identity, replace_quote_owner
from project_mai_tai.momentum_live.persistence import LivePersistence
from project_mai_tai.momentum_live.protection import NativeProtection, ProtectionHandle, ProtectionPorts, ProtectionRead
from project_mai_tai.strategy_core.time_utils import US_MARKET_HOLIDAYS

ET = ZoneInfo("America/New_York")
# Main's retained Oct14 119 consecutive intervals: median15.028s, twice30.056s.
POSITION_SYNC_MAX_AGE_NS = 30_056_000_000


def unknown_sec_route(symbol, now):
    local = now.astimezone(ET)
    try:
        decision = select_route(now, market_open=local.weekday() < 5 and local.date() not in US_MARKET_HOLIDAYS,
                                is_us=None)
    except Refusal:
        return SimpleNamespace(broker=None, reason="refused_hours")
    return SimpleNamespace(broker=decision.route.value, reason=decision.reason)


class OmsLiveRuntime:
    """New-strategy ports using the existing OMS adapter and off-loop DB units."""

    def __init__(self, service, *, route_for=unknown_sec_route, clock=None, identity=None):
        self.service = service
        self.settings = service.settings
        self.db = LivePersistence(service.store)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.identity = identity
        self.route_for = route_for
        self.accounts = (self.settings.strategy_schwab_1m_v2_account_name,
                         self.settings.strategy_schwab_1m_v2_webull_account_name)
        self.book = EpisodeLedger()
        self.bindings = {}
        self.runtime = {}
        self.boot_owned_exposures = {}
        self._legacy_stop_blocks = {}
        self._missing_entry_reads = {}
        self._missing_entry_pass = {}
        self._missing_entry_started = {}
        self._missing_entry_read_budget = (None, 0)
        self._exit_gate_frames = {}
        self._exit_gate_states = {}
        self._exit_gate_fire = None
        self._exit_gate_instance = (os.getpid(), time.monotonic_ns())
        self.state_lock = asyncio.Lock()
        self.actor = None
        self.publisher = None
        self.publication_task = None
        self.native = NativeProtection(ProtectionPorts(self._save_handle, self._prepare_protection,
            self._submit_protection, self._read_protection, self._cancel_protection))
        self.backend = OwnedNativeBackend()

    def now_ns(self):
        return int(self.clock().timestamp()*1e9)

    def _entry_enabled(self):
        if self._legacy_stop_blocks:
            return False
        if not self.settings.momentum_live_enabled:
            return False
        if not getattr(self.settings, "bot_control_enabled", False):
            return True
        control = getattr(self.service, "_bot_control_runtime", None)
        return control is not None and control.admission_open(STRATEGY_CODE)

    def _stop_requested(self):
        if not getattr(self.settings, "bot_control_enabled", False):
            return False
        control = getattr(self.service, "_bot_control_runtime", None)
        return control is not None and control.stop_requested(STRATEGY_CODE)

    def _entry_control_metadata(self, signal):
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is None:
            raise Refusal("KILLSW1:entry_control_unknown")
        metadata = {}
        seconds, nanos = divmod(signal.observed_ns, 1_000_000_000)
        decided = datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000)
        try:
            control.stamp(metadata, STRATEGY_CODE, decided)
        except Exception as exc:
            raise Refusal("KILLSW1:entry_scope_not_running") from exc
        return metadata

    async def _abandon_unsent_for_stop(self, identity):
        return await self.service._run_db(lambda unit: self.db.abandon_unsent_entry(unit, identity))


    def provider(self, account):
        value = self.settings.provider_for_account(account)
        expected = "schwab" if account == self.accounts[0] else "webull" if account == self.accounts[1] else None
        if value != expected or expected is None:
            raise Refusal("live_shared_account_provider_mismatch")
        return value

    async def start(self) -> bool:
        day_start = datetime.combine(self.clock().astimezone(ET).date(), daytime(), ET).astimezone(UTC)
        bundle = await self.service._run_db(lambda session: self.db.restore_bundle(session, day_start), commit=False)
        if not self.settings.momentum_live_enabled and not bundle["episodes"]:
            await self._cleanup_empty_boot_owner()
            return False
        if self.settings.momentum_live_enabled and self.settings.oms_adapter == "simulated":
            raise Refusal("live_enabled_simulated_adapter_refused")
        if len(set(self.accounts)) != 2 or any(not value.startswith("live:") for value in self.accounts):
            raise Refusal("live_shared_account_aliases_missing")
        for account in self.accounts:
            self.provider(account)
        self.book = EpisodeLedger.restore(bundle["episodes"])
        self.bindings.update(bundle["bindings"])
        self.runtime.update(bundle["runtime"])
        for episode in self.book.episodes.values():
            if episode.state == "claimed" and episode.first_submit_ns is None:
                snapshot = await self._inspect(episode.episode_id)
                if snapshot["order_count"] or snapshot["quantity"] or snapshot["exited"] or episode.filled or episode.exited:
                    raise Refusal("boot_quote_reservation_order_or_exposure_UNKNOWN")
                # A durable claim with no order or send is proven unsent. Keep
                # episode/source idempotence, but release its quote-only slot.
                self.book.reconcile(episode.episode_id, cumulative_filled=Decimal(0),
                    cumulative_exited=Decimal(0), now_ns=self.now_ns(), terminal_entry=True)
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == episode.episode_id))
        for identity, values in self.runtime.items():
            raw = values.get("protection")
            if raw:
                handle = ProtectionHandle(**{**raw, "quantity": Decimal(raw["quantity"]),
                    "cumulative_exited": Decimal(raw["cumulative_exited"]), "child_ids": tuple(raw["child_ids"])})
                self.native.handles[identity] = handle
                self.native.generations[identity] = int(values.get("protection_generation", 1))
        await self._check_restored_native_stops()
        entry_ports = ExecutionPorts(self.clock, self.quote, self.route_for, self._foreign_owner,
            self._claim, self._prepare, self._save, self._submit, self._record, self._route_report,
            self._request_quote_owner, self._wait_quote,
            entry_metadata=self._entry_control_metadata if getattr(self.settings, "bot_control_enabled", False) else None,
            refuse_unsent_entry=self._refuse_unsent_entry,
            reprice_unsent_entry=self._reprice_unsent_entry,
            refuse_local_entry=self._refuse_local_entry,
            mark_refused_pre_submit=self._mark_refused_pre_submit)
        executor = LiveEntryExecutor(self.book, entry_ports, schwab_account=self.accounts[0], webull_account=self.accounts[1])
        ports = ActorPorts(self._entry_enabled, self.now_ns, self._observe,
            self._save, self._cancel_entry, self._protect, self._exit, self._exit_quote,
            stop_requested=self._stop_requested, abandon_unsent=self._abandon_unsent_for_stop,
            diagnostic_account=lambda: self.accounts[0],
            exit_decision_observer=self._exit_gate_fired)
        self.actor = LiveActor(self.book, executor, ports, state_lock=self.state_lock)
        await self.actor.start()
        self.publisher = OwnerProofPublisher(self.service.redis, self.settings.redis_stream_prefix, self.book,
            producer_instance_id=self.identity or current_process_identity(), account_names=self.accounts,
            bindings=self.bindings, enabled=self._entry_enabled,
            replace_owner=self._replace_owner, state_lock=self.state_lock, clock_ns=self.now_ns)
        await self.publisher.publish_once()
        await self._feedback()
        self.publication_task = asyncio.create_task(self._publish_loop())
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is not None and getattr(self.settings, "bot_control_enabled", False):
            self._control_driver = self._control_cleanup_progress
            control.executor.register(STRATEGY_CODE, self._control_driver)
        return True

    async def _check_restored_native_stops(self):
        """Admission-only boot fence; existing owned cleanup stays operational."""
        findings = []
        for identity, handle in self.native.handles.items():
            if handle.session != "NORMAL":
                continue
            reason = "geometry_unknown"
            try:
                from project_mai_tai.momentum_live.exit_policy import stop_loss_level

                snapshot = await self._inspect(identity)
                roots = [item for item in snapshot["orders"]
                         if item["request"].client_order_id == handle.base_coid]
                if len(roots) != 1:
                    raise Refusal("restored_native_root_unknown")
                request = roots[0]["request"]
                price = snapshot["price"]
                stop = Decimal(request.metadata["bracket_stop_price"])
                if (request.broker_account_name != handle.account or request.symbol != handle.symbol
                        or request.strategy_code != STRATEGY_CODE or request.side != "sell"
                        or request.intent_type != "close" or request.quantity != handle.quantity
                        or request.metadata.get("episode_id") != identity
                        or request.metadata.get("session") != "NORMAL"
                        or price is None or not price.is_finite() or price <= 0
                        or not stop.is_finite() or stop <= 0):
                    raise Refusal("restored_native_geometry_unknown")
                tick = Decimal(".0001") if price < 1 else Decimal(".01")
                expected = (stop_loss_level(price)/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                if stop == expected:
                    continue
                legacy = (price*Decimal(".85")/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                reason = "legacy_15pct" if stop == legacy else "geometry_unknown"
            except Exception:
                # Unknown geometry is an admission refusal, never an OMS crash
                # or permission to replace/cancel an existing protection tree.
                snapshot = None
            self._legacy_stop_blocks[identity] = handle.base_coid
            findings.append(f"{identity}:{reason}")
            if snapshot is not None:
                try:
                    reconciled = await self._reconcile_closed_native_restore(identity, handle, snapshot)
                    if reconciled is not None:
                        snapshot = reconciled
                except Exception:
                    pass  # An incomplete local proof retains the admission fence.
                self._clear_legacy_stop_block(identity, snapshot, handle)
        if findings:
            self.service.logger.critical(
                "[MOMEXIT-B-RESTORE-GUARD] episodes=%s entry_admission=%s owned_cleanup=enabled",
                ",".join(findings), "blocked" if self._legacy_stop_blocks else "flat_terminal",
            )

    async def _reconcile_closed_native_restore(self, identity, handle, snapshot):
        episode = self.book.episodes.get(identity)
        if (episode is None or episode.state not in {"closed", "terminal_no_fill"}
                or handle.session != "NORMAL" or not snapshot["entry_terminal"]
                or snapshot["quantity"] != snapshot["exited"] or snapshot["outstanding_exit"]
                or len(handle.child_ids) != 2 or len(set(handle.child_ids)) != 2):
            return None
        if self.provider(handle.account) == "webull":
            expected = tuple(self.backend.leaf(handle.account, self).exit_pair_leg_client_order_ids(handle.base_coid))
            if set(expected) != set(handle.child_ids):
                return None
            children = {coid: None for coid in expected}
        else:
            children = {f"ml1-{handle.symbol}-n-{sha256(native.encode()).hexdigest()[:16]}": native
                        for native in handle.child_ids}

        def proof(session):
            current = self.db.inspect_episode(session, identity)
            if (not current["entry_terminal"] or current["quantity"] != current["exited"]
                    or current["outstanding_exit"]):
                return None
            rows = session.scalars(select(BrokerOrder).where(
                BrokerOrder.client_order_id.in_({handle.base_coid, *children}),
                BrokerOrder.payload["episode_id"].as_string() == identity)).all()
            by_coid = {row.client_order_id: row for row in rows}
            if set(by_coid) != {handle.base_coid, *children}:
                return None  # Missing child is UNKNOWN, never inferred from flat.
            terminal = {"filled", "cancelled", "rejected", "expired", "aborted"}
            native_ids = set()
            child_quantity = Decimal(0)
            for coid, row in by_coid.items():
                account = session.get(BrokerAccount, row.broker_account_id)
                strategy = session.get(Strategy, row.strategy_id)
                metadata = row.payload or {}
                if (account is None or account.name != handle.account or strategy is None
                        or strategy.code != STRATEGY_CODE or row.symbol != handle.symbol or row.side != "sell"
                        or row.quantity != handle.quantity or row.status not in terminal or not row.broker_order_id
                        or row.broker_order_id in native_ids):
                    return None
                native_ids.add(row.broker_order_id)
                fills = session.scalars(select(Fill).where(Fill.order_id == row.id)).all()
                quantity = Decimal(0)
                for fill in fills:
                    if (fill.strategy_id != row.strategy_id or fill.broker_account_id != row.broker_account_id
                            or fill.side != "sell" or not fill.quantity.is_finite() or fill.quantity < 0):
                        return None
                    quantity += fill.quantity
                if not 0 <= quantity <= row.quantity:
                    return None
                if coid == handle.base_coid:
                    if (quantity != 0 or metadata.get("session") != "NORMAL"
                            or metadata.get("momlive_one_shot") != "true"
                            or not (metadata.get("webull_exit_only_pair") == "true"
                                    or metadata.get("exit_only_oco") == "true")
                            or metadata.get("entry_broker_order_id") != handle.entry_order_id):
                        return None
                else:
                    if (metadata.get("momlive_native_child") != "true"
                            or metadata.get("momlive_terminal_broker_event") != row.status
                            or children[coid] is not None and str(row.broker_order_id) != children[coid]
                            or row.status == "filled" and quantity != row.quantity):
                        return None
                    child_quantity += quantity
            if child_quantity > handle.quantity:
                return None
            return current

        current = await self.service._run_db(proof, commit=False)
        if current is not None:
            # Reconcile only the stale handle journal, never orders, fills or
            # broker state. Closed-today episodes have no actor work owner.
            prior_status, prior_exited = handle.status, handle.cumulative_exited
            handle.status, handle.cumulative_exited = "terminal", current["exited"]
            try:
                await self._save_handle(handle)
            except BaseException:
                handle.status, handle.cumulative_exited = prior_status, prior_exited
                raise
        return current

    def _clear_legacy_stop_block(self, identity, snapshot, handle):
        if (self._legacy_stop_blocks.get(identity) == handle.base_coid and handle.status == "terminal"
                and snapshot["entry_terminal"] and snapshot["quantity"] == snapshot["exited"]
                and not snapshot["outstanding_exit"]):
            self._legacy_stop_blocks.pop(identity, None)

    async def close(self):
        control = getattr(self.service, "_bot_control_runtime", None)
        driver = self.__dict__.pop("_control_driver", None)
        if control is not None and driver is not None:
            if control.executor.drivers.get(STRATEGY_CODE) == driver:
                control.executor.drivers.pop(STRATEGY_CODE, None)
        if self.publication_task:
            self.publication_task.cancel()
            await asyncio.gather(self.publication_task, return_exceptions=True)
        if self.actor:
            await self.actor.close()
        if self._missing_entry_reads:
            # A timed-out wrapper is not physical completion of a broker read.
            await asyncio.gather(*self._missing_entry_reads.values(), return_exceptions=True)
            self._missing_entry_reads.clear()
            self._missing_entry_started.clear()

    async def _control_cleanup_progress(self, lease, journal):
        from project_mai_tai.oms.bot_control_executor import StopProgress

        if self.actor is None or not self.actor.restored or not lease.scope_complete:
            return StopProgress("BLOCKED_UNKNOWN")
        # The existing actor remains the only entry/position execution owner.
        # Offering work is non-blocking; this coordinator never creates a second
        # close from an account position or clears an episode's reservation.
        for identity in tuple(self.actor.work.owners):
            self.actor.work.offer(identity)
        async with self.state_lock:
            for identity, handle in tuple(self.native.handles.items()):
                episode = self.book.episodes.get(identity)
                if episode is None:
                    return StopProgress("BLOCKED_UNKNOWN")
                if episode.state in {"closed", "terminal_no_fill"} and handle.status != "terminal":
                    observation = await self._observe(identity)
                    if observation.position is None or observation.position.remaining != 0:
                        return StopProgress("BLOCKED_UNKNOWN")
                    if not await self.native.release_for_exit(observation.position):
                        return StopProgress("RECONCILING_PROTECTION")
            active = [identity for identity, episode in self.book.episodes.items()
                      if episode.state not in {"closed", "terminal_no_fill"}]
            if any(self.runtime.get(identity, {}).get("killsw1_session_blocked") for identity in active):
                return StopProgress("BLOCKED_SESSION")
            if any(self.runtime.get(identity, {}).get("killsw1_broker_refused")
                   or self.runtime.get(identity, {}).get("killsw1_wait") for identity in active):
                return StopProgress("BLOCKED_UNKNOWN")
            if active:
                return StopProgress("EXIT_WORKING")

            def prove(session):
                # Reuse the existing owned-parent/order/fill/VP conservation
                # proof. This is NOT a claim that either shared account is flat.
                self.db.prove_empty_owned_state(session, episode_ids=tuple(self.book.episodes))
                accounts = session.scalars(select(BrokerAccount).where(
                    BrokerAccount.id.in_(lease.account_ids),
                )).all()
                if len(accounts) != len(lease.account_ids) or any(
                    account.environment != "live" or account.provider not in {"schwab", "webull"}
                    for account in accounts
                ):
                    raise Refusal("KILLSW1:cleanup_account_scope_unknown")
                strategy = session.scalar(select(Strategy).where(Strategy.code == STRATEGY_CODE))
                if strategy is None or strategy.id != lease.strategy_id:
                    raise Refusal("KILLSW1:cleanup_strategy_scope_changed")
                mismatch = session.scalar(select(Fill.id).join(
                    BrokerOrder, BrokerOrder.id == Fill.order_id,
                ).where(BrokerOrder.strategy_id == lease.strategy_id, or_(
                    Fill.strategy_id != BrokerOrder.strategy_id,
                    Fill.broker_account_id != BrokerOrder.broker_account_id,
                    Fill.symbol != BrokerOrder.symbol, Fill.side != BrokerOrder.side,
                    Fill.quantity <= 0,
                )).limit(1))
                executed = select(func.coalesce(func.sum(Fill.quantity), 0)).where(
                    Fill.order_id == BrokerOrder.id,
                ).correlate(BrokerOrder).scalar_subquery()
                unbacked_filled = session.scalar(select(BrokerOrder.id).where(
                    BrokerOrder.strategy_id == lease.strategy_id,
                    BrokerOrder.status == "filled", BrokerOrder.quantity != executed,
                ).limit(1))
                if mismatch is not None or unbacked_filled is not None:
                    raise Refusal("KILLSW1:cleanup_fill_coverage_unknown")
                observed = datetime.now(UTC).isoformat()
                result = {}
                for account in accounts:
                    proof = {"source": "owned_parent_order_fill_vp_conservation",
                             "strategy_id": str(lease.strategy_id), "account_id": str(account.id),
                             "command_id": str(lease.command_id), "generation": lease.generation,
                             "episodes": tuple(sorted(self.book.episodes)), "observed_at": observed}
                    result[str(account.id)] = {
                        "complete": True, "owned_quantity": 0, "working_orders": 0, "in_flight": 0,
                        "observed_at": observed,
                        "evidence_hash": sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest(),
                        "source": proof["source"],
                    }
                return result

            census = await self.service._run_db(prove, commit=False)
        return StopProgress("SETTLED", census)

    async def _cleanup_empty_boot_owner(self):
        await self.service._run_db(self.db.prove_empty_owned_state, commit=False)
        read = getattr(self.service.redis, "xrevrange", None)
        if read is None:
            return  # No writes/clear without an actual retained receipt.
        async with asyncio.timeout(1):
            rows = await read(f"{self.settings.redis_stream_prefix}:momentum-live-owner-proof", count=1)
        if not rows:
            return
        if not isinstance(rows, list) or len(rows) != 1:
            raise Refusal("empty_owner_previous_proof_UNKNOWN")
        encoded = rows[0][1].get("data")
        if not isinstance(encoded, str) or len(encoded) > 65536:
            raise Refusal("empty_owner_previous_proof_UNKNOWN")
        proof = json.loads(encoded)
        episodes = proof.get("episodes")
        if (proof.get("version") != 1 or proof.get("source_service") != "oms"
                or proof.get("strategy_code") != STRATEGY_CODE or proof.get("restore_ready") is not True
                or proof.get("account_names") != sorted(self.accounts)
                or not re.fullmatch(r"[1-9][0-9]*:[1-9][0-9]*", str(proof.get("producer_instance_id")))
                or not isinstance(episodes, list) or len(episodes) > 2
                or proof.get("quote_symbols") != sorted({row["symbol"] for row in episodes})
                or len({row["episode_id"] for row in episodes}) != len(episodes)
                or any(row.get("account_name") not in self.accounts for row in episodes)):
            raise Refusal("empty_owner_previous_proof_UNKNOWN")
        await self.service._run_db(lambda session: self.db.prove_empty_owned_state(session,
            episode_ids=[row["episode_id"] for row in episodes]), commit=False)
        self.book = EpisodeLedger.restore([])
        self.publisher = OwnerProofPublisher(self.service.redis, self.settings.redis_stream_prefix, self.book,
            producer_instance_id=self.identity or current_process_identity(), account_names=self.accounts,
            bindings={}, enabled=lambda: False, replace_owner=self._replace_owner,
            state_lock=self.state_lock, clock_ns=self.now_ns)
        await self.publisher.publish_once()

        self.publication_task = asyncio.create_task(self._publish_empty_off_loop())

    async def _publish_empty_off_loop(self):
        while True:
            await asyncio.sleep(1)
            try:
                # Never refresh an idle receipt after owned exposure becomes
                # unreadable or appears in the durable book.
                async with asyncio.timeout(1):
                    await self.service._run_db(self.db.prove_empty_owned_state, commit=False)
                await self.publisher.publish_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.service.logger.warning("[MOMLIVE1-PROOF] empty_off_publication=UNKNOWN")

    async def _replace_owner(self, symbols):
        await replace_quote_owner(self.service.redis, self.settings.redis_stream_prefix, symbols,
                                  maxlen=self.settings.redis_market_data_subscription_stream_maxlen)

    async def _request_quote_owner(self):
        async with self.state_lock:
            self.actor._refresh_owners()
            for symbol in self.actor.dispatch.by_symbol:
                self.actor.dispatch.quote_waiters.setdefault(symbol, asyncio.Event())
            await self.publisher.publish_locked()

    async def _wait_quote(self, symbol):
        waiter = self.actor.dispatch.quote_waiters.get(symbol)
        if waiter is None:
            raise Refusal("quote_owner_waiter_missing")
        try:
            await waiter.wait()
        finally:
            self.actor.dispatch.quote_waiters.pop(symbol, None)

    async def _feedback(self):
        await self.service.redis.xadd(f"{self.settings.redis_stream_prefix}:momentum-live-state",
            {"data": json.dumps({"source": "oms", "strategy_code": STRATEGY_CODE, "version": 1,
                "producer_instance_id": self.publisher.identity, "ready": self.actor.restored,
                "observed_ns": self.now_ns(), "episodes": self.book.snapshot()})}, maxlen=4, approximate=False)

    async def _publish_loop(self):
        while True:
            await asyncio.sleep(1)
            try:
                await self.publisher.publish_once()
                async with asyncio.timeout(1):
                    await self._feedback()
            except asyncio.CancelledError:
                raise
            except Exception:
                self.service.logger.warning("[MOMLIVE1-PROOF] publication=UNKNOWN")

    def quote(self, symbol):
        return self.service._latest_quotes_by_symbol.get(symbol)

    def _exit_quote(self, symbol):
        raw = self.quote(symbol) or {}
        stamp = raw.get("received_at")
        return {"bid": Decimal(str(raw["bid"])) if raw.get("bid") is not None else None,
                "observed_ns": int(stamp.timestamp()*1e9) if isinstance(stamp, datetime) and stamp.tzinfo else None}

    async def _foreign_owner(self, account, symbol):
        self.provider(account)
        if symbol in self.settings.protected_symbol_set or symbol in self.service._manual_stop_symbols:
            return True
        def read(session):
            receipt = load_receipts(session).get(account)
            if (not receipt or receipt.get("writer") != "oms.sync_broker_positions"
                    or receipt.get("broker_account_name") != account
                    or type(receipt.get("position_count")) is not int or receipt["position_count"] < 0):
                raise Refusal("account_position_sync_receipt_UNKNOWN")
            # The shared reader normalizes naive timestamps for legacy readers;
            # entry admission requires the original durable timestamp to be aware.
            raw = next((row.payload for row in session.scalars(select(DashboardSnapshot).where(
                DashboardSnapshot.snapshot_type == SNAPSHOT_TYPE).order_by(
                DashboardSnapshot.created_at.desc())).all()
                if (row.payload or {}).get("broker_account_name") == account), None)
            try:
                stamp = datetime.fromisoformat(str(raw["read_at"]))
            except (TypeError, KeyError, ValueError):
                raise Refusal("account_position_sync_receipt_UNKNOWN") from None
            if stamp.tzinfo is None or stamp != receipt.get("read_at"):
                raise Refusal("account_position_sync_receipt_UNKNOWN")
            if not 0 <= self.now_ns()-int(stamp.timestamp()*1e9) <= POSITION_SYNC_MAX_AGE_NS:
                raise Refusal("account_position_sync_stale")
            strategy = session.scalar(select(Strategy).where(Strategy.code == STRATEGY_CODE))
            broker = session.scalar(select(BrokerAccount).where(BrokerAccount.name == account))
            if broker is None:
                raise Refusal("broker_account_not_registered")
            return self.db.foreign_owner(session, strategy.id if strategy else None, broker.id, symbol)
        return await self.service._run_db(read, commit=False)

    async def _claim(self, episode, account, provider, signal: Signal):
        saved = {**asdict(signal), "signal_price": str(signal.signal_price)}
        result = await self.service._run_db(lambda session: self.db.claim(session,
            episode=episode, account=account, provider=provider, signal=saved))
        if result:
            self.bindings[episode["episode_id"]] = account
        return result

    async def _save(self, episode):
        values = dict(self.runtime.get(episode["episode_id"], {}))
        await self.service._run_db(lambda session: self.db.save(session, episode, metadata=values))
        if episode.get("state") in {"closed", "terminal_no_fill"}:
            self._exit_gate_retire(episode["episode_id"])

    async def _prepare(self, identity, request, provider):
        await self.service._run_db(lambda session: self.db.prepare_order(session, identity, request, provider=provider))

    async def _refuse_unsent_entry(self, request, reason, context):
        await self.service._run_db(lambda session: self.db.refuse_unsent_entry(session, request, reason, context))

    async def _mark_refused_pre_submit(self, request, reason):
        await self.service._run_db(lambda session: self.db.mark_refused_pre_submit(session, request, reason))
        episode = self.book.episodes[request.metadata["episode_id"]]
        if episode.state == "claimed" and episode.first_submit_ns is None:
            episode.state, episode.cancel_requested = "cancel_requested", True

    async def _reprice_unsent_entry(self, previous, current, episode=None):
        await self.service._run_db(lambda session: self.db.reprice_unsent_entry(
            session, previous, current, episode))

    async def _refuse_local_entry(self, request, proof):
        from project_mai_tai.momentum_live.entry_wire import _NoDispatchProof
        if type(proof) is not _NoDispatchProof:
            raise Refusal("mombuffer1_local_no_dispatch_proof_unknown")
        permit = proof.consume(request)
        await self.service._run_db(lambda session: self.db.refuse_local_entry(
            session, request, proof.reason, proof.context, permit=permit,
            expected_episode=proof.attempt.episode))

    async def _submit(self, request):
        self.provider(request.broker_account_name)
        return await self.service.broker_adapter.submit_order(request)

    async def _record(self, request, reports):
        result = await self.service._run_db(lambda session: self.db.record(session, request, reports))
        if result.get("entry_ack_identity_unknown"):
            await self._refuse_entry_identity(request.metadata["episode_id"])
        if (request.metadata.get("exit_reason") == "KILLSW1_STOP"
                and any(report.origin == "broker" and report.event_type == "rejected" for report in reports)):
            identity = request.metadata["episode_id"]
            values = self.runtime.setdefault(identity, {})
            if not values.get("killsw1_broker_refused"):
                values["killsw1_broker_refused"] = request.client_order_id
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
                def incident(session):
                    session.add(SystemIncident(
                        service_name="oms-risk", severity="critical", status="open",
                        title=f"KILLSW1 broker refused owned close: {request.symbol}", opened_at=self.clock(),
                        payload={"source": "KILLSW1", "episode_id": identity,
                                 "client_order_id": request.client_order_id, "reason": "broker_rejected_close",
                                 "admission": "STOP", "retry": "BLOCKED"},
                    ))
                self.service.logger.critical(
                    "[KILLSW1-BLOCKED] episode=%s coid=%s reason=broker_rejected_close retry=NEVER_BLIND",
                    identity, request.client_order_id,
                )
                await self.service._run_db(incident)
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is not None and control.stop_requested(STRATEGY_CODE):
            errors = self.__dict__.setdefault("_control_receipt_errors", {})
            try:
                await control.record_cleanup_read(request, reports)
                errors.pop(request.client_order_id, None)
            except Exception as exc:
                # Accounting is already committed. An audit outage must not
                # prevent protecting a newly filled slice. Unresolved attempts
                # still prevent STOP settlement and never permit another sell.
                reason = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                if errors.get(request.client_order_id) != reason:
                    self.service.logger.error(
                        "[KILLSW1-RECEIPT] coid=%s audit=UNKNOWN accounting=COMMITTED reason=%s",
                        request.client_order_id, reason,
                    )
                    errors[request.client_order_id] = reason
        return result

    async def _route_report(self, identity, day, category):
        self.runtime.setdefault(identity, {})["route_category"] = category
        await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))

    async def _inspect(self, identity):
        snapshot = await self.service._run_db(lambda session: self.db.inspect_episode(session, identity), commit=False)
        entries = [row["request"] for row in snapshot["orders"] if row["request"].side == "buy"]
        quantity, exited = snapshot["quantity"], snapshot["exited"]
        if quantity == exited:
            self.boot_owned_exposures.pop(identity, None)
        elif (len(entries) == 1 and entries[0].broker_account_name in self.accounts
              and entries[0].strategy_code == STRATEGY_CODE
              and quantity.is_finite() and exited.is_finite() and 0 <= exited < quantity
              and (identity in self.boot_owned_exposures or len(self.boot_owned_exposures) < 2)):
            self.boot_owned_exposures[identity] = {"symbol": entries[0].symbol,
                "quantity": str(quantity-exited), "account": entries[0].broker_account_name}
        return snapshot

    async def _read_missing_execution_time(self, identity, row):
        """One bounded original-order read; never infer an execution clock."""
        request = row["request"]
        key = "momlive_first_fill_ns" if request.side == "buy" else "momlive_last_exit_fill_ns"
        if row["status"] != "filled" or request.metadata.get(key) is not None:
            return
        values = self.runtime.setdefault(identity, {})
        attempted = list(values.get("execution_time_readback_coids") or [])
        if request.client_order_id in attempted or len(attempted) >= 64:
            return
        attempted.append(request.client_order_id)
        values["execution_time_readback_coids"] = attempted
        # Save before the read so a restart cannot turn missing proof into a poll loop.
        await self._save(next(item for item in self.book.snapshot() if item["episode_id"] == identity))
        report = await self.backend.fetch_entry_update(request, self, fresh=True)
        complete = (report is not None and report.origin == "broker" and report.event_type == "filled"
                    and report.filled_quantity == request.quantity
                    and report.metadata.get("momlive_execution_time_source") == "broker_execution_time")
        if complete:
            await self._record(request, [report])
        else:
            self.service.logger.warning("[MOMLIVE1-EXECUTION-TIME-UNKNOWN] episode=%s coid=%s side=%s",
                identity, request.client_order_id, request.side)

    async def _observe(self, identity):
        snapshot = await self._inspect(identity)
        entry = None
        for row in snapshot["orders"]:
            request = row["request"]
            if request.side == "buy" and request.intent_type == "open":
                entry = request
                if row["status"] == "filled" and request.metadata.get("momlive_first_fill_ns") is None:
                    await self._read_missing_execution_time(identity, row)
                    continue
                if (row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}
                        or snapshot["quantity"] > 0 and snapshot["first_fill_ns"] is None
                        and self.runtime.get(identity, {}).get("first_execution_time_status") != "UNKNOWN"):
                    sent = self.book.episodes[identity].first_submit_ns
                    if (sent is not None and self.now_ns() - sent >= 2_000_000_000
                            and row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}):
                        request = replace(request, metadata={**request.metadata, "momlive_entry_deadline_read": "true"})
                    missing_unsent = (not row["broker_order_id"] and "submitted_at" in row
                                      and row["submitted_at"] is None)
                    report = None if missing_unsent else await self.backend.fetch_entry_update(request, self)
                    if report is not None:
                        await self._record(request, [report])
            elif request.side == "sell" and row["status"] == "filled":
                await self._read_missing_execution_time(identity, row)
            elif (request.side == "sell" and request.metadata.get("exit_reason")
                  and row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}):
                report = await self.backend.fetch_entry_update(request, self)
                if report is not None:
                    await self._record(request, [report])
        snapshot = await self._inspect(identity)
        if (self.book.episodes[identity].cancel_requested
                and not self.runtime.get(identity, {}).get("cancel_wire_sent_coid")
                and not snapshot["entry_terminal"]):
            await self._cancel_entry(identity)
            # Cancellation can commit terminality/fills. Never return the old
            # snapshot to the actor and reopen a just-retired reservation.
            snapshot = await self._inspect(identity)
        if snapshot["entry_terminal"] and not snapshot["quantity"] and snapshot.get("closed_ns") is not None:
            self.book.episodes[identity].closed_ns = snapshot["closed_ns"]
        position = None
        if snapshot["quantity"] > 0:
            if entry is None or not entry.metadata.get("broker_order_id"):
                # Re-read the durable ID after the broker's first receipt.
                entries = [row["request"] for row in snapshot["orders"]
                           if row["request"].side == "buy" and row["request"].intent_type == "open"]
                entry = entries[0] if len(entries) == 1 else None
            if entry is None or not entry.metadata.get("broker_order_id"):
                raise Refusal("held_entry_identity_UNKNOWN")
            if snapshot["first_fill_ns"] is None:
                self.runtime.setdefault(identity, {})["first_execution_time_status"] = "UNKNOWN"
            position = OwnedPosition(identity, entry.broker_account_name, entry.symbol, entry.client_order_id,
                entry.metadata["broker_order_id"], snapshot["quantity"], snapshot["price"], snapshot["first_fill_ns"],
                exited=snapshot["exited"], entry_send_ns=self.book.episodes[identity].first_submit_ns,
                pending_exit_reason=self.runtime.get(identity, {}).get("soft_exit_reason"),
                market_session=self._protection_market_session())
            self.runtime.setdefault(identity, {}).update(time_exit_clock_source=position.exit_clock_source,
                                                        time_exit_clock_ns=position.exit_clock_ns)
            handle = self.native.handles.get(identity)
            if handle:
                await self.native._observe(position, handle)
                snapshot = await self._inspect(identity)
                self._clear_legacy_stop_block(identity, snapshot, handle)
        return OwnedObservation(snapshot["quantity"], snapshot["exited"], snapshot["first_fill_ns"],
            snapshot["entry_terminal"], snapshot["outstanding_exit"], position,
            owner_provider=self._diagnostic_owner_provider(position))

    def _diagnostic_owner_provider(self, position):
        try:
            if position is not None and position.account == self.accounts[0]:
                return self.provider(position.account)
        except Exception:
            pass  # Optional logging evidence cannot alter the original observation.
        return None

    async def _refuse_entry_identity(self, identity):
        values = self.runtime.setdefault(identity, {})
        if not values.get("momcancel1_identity_critical_reported"):
            emitted = self.__dict__.setdefault("_momcancel1_identity_critical_emitted", set())
            if identity not in emitted:
                self.service.logger.critical(
                    "[MOMCANCEL1] episode=%s cancel_entry_broker_identity_UNKNOWN entry_retry=NEVER",
                    identity,
                )
                emitted.add(identity)
            # Process-local emission survives a failed receipt write. Only a
            # committed receipt suppresses emission in a restored runtime.
            values["momcancel1_identity_critical_reported"] = True
            try:
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
            except asyncio.CancelledError:
                values.pop("momcancel1_identity_critical_reported", None)
                raise
            except Exception as exc:
                values.pop("momcancel1_identity_critical_reported", None)
                self.service.logger.error(
                    "[MOMCANCEL1] episode=%s diagnostic_receipt=UNKNOWN failure=%s",
                    identity, type(exc).__name__,
                )
        self.service.logger.error("[MOMCANCEL1] episode=%s cancel_entry_broker_identity_UNKNOWN", identity)
        raise Refusal("cancel_entry_broker_identity_UNKNOWN") from None

    async def _fresh_cancel_read(self, request, *, handle=None):
        report = await self.backend.fetch_entry_update(request, self, fresh=True)
        if report is not None:
            # Attribution failures must stop the cancel, not become read UNKNOWN.
            if handle is None:
                await self._record(request, [report])
            else:
                await self.backend._record_child(handle, self, report, request)
        return report

    async def _fresh_cancel_outcome(self, request, *, handle=None):
        outcome = await self.backend.fresh_cancel_read(request, self)
        report = outcome.report
        if report is not None and (outcome.kind == "report"
                or outcome.kind == "status_unknown" and report.filled_quantity > 0):
            if handle is None:
                await self._record(request, [report])
            else:
                await self.backend._record_child(handle, self, report, request)
        return outcome

    @staticmethod
    def _cancel_read_terminal(report, request):
        return report is not None and (
            report.filled_quantity == request.quantity
            or report.event_type in {"filled", "cancelled", "rejected", "expired", "aborted"})

    def _missing_entry_unknown(self, identity, reason):
        values = self.runtime.setdefault(identity, {})
        if not values.get("momcancel1_missing_entry_alerted"):
            values["momcancel1_missing_entry_alerted"] = True
            if (values.get("momcancel1_identity_critical_reported")
                    or identity in getattr(self, "_momcancel1_identity_critical_emitted", set())):
                return
            self.service.logger.error(
                "[MOMCANCEL1] episode=%s broker_client_lookup=UNKNOWN reason=%s retained=true retry=SYNC_PASS",
                identity, reason)

    async def _lookup_missing_entry(self, identity, request):
        pass_id = getattr(self.service, "_reserve1_sync_pass_id", None)
        if type(pass_id) is not int or pass_id <= 0:
            self._missing_entry_unknown(identity, "no_actual_sync_pass")
            return None
        coid = request.client_order_id
        pending = self._missing_entry_reads.get(coid)
        if pending is not None:
            if not pending.done():
                if time.monotonic() - self._missing_entry_started[coid] >= 2:
                    self._missing_entry_unknown(identity, "read_pending_physical_join_required")
                return None
            self._missing_entry_reads.pop(coid)
            self._missing_entry_started.pop(coid)
            try:
                result = pending.result()
            except asyncio.CancelledError:
                self._missing_entry_unknown(identity, "read_cancelled_not_absence")
                return None
            except Exception as exc:
                self._missing_entry_unknown(identity, type(exc).__name__)
                return None
        else:
            if self._missing_entry_pass.get(coid) == pass_id:
                return None
            budget_pass, count = self._missing_entry_read_budget
            if budget_pass != pass_id:
                count = 0
            if count >= 2:
                return None  # Even intra-pass slot turnover cannot start a third read.
            self._missing_entry_read_budget = (pass_id, count + 1)
            self._missing_entry_pass[coid] = pass_id
            self._missing_entry_started[coid] = time.monotonic()
            # Never wait for broker I/O under the shared owned-episode lock.
            # The task awaits the actual to_thread read; close() joins it.
            self._missing_entry_reads[coid] = asyncio.create_task(
                self.backend.lookup_missing_entry(request, self, pass_id), name="MOMCANCEL1-client-id-read")
            return None
        if (type(result) is not MomentumEntryLookup or result.sync_pass != pass_id
                or getattr(self.service, "_reserve1_sync_pass_id", None) != pass_id
                or (result.account, result.client_order_id, result.symbol) != (
                    request.broker_account_name, coid, request.symbol)
                or result.provider != self.provider(request.broker_account_name)
                or not 0 <= self.now_ns() - result.observed_ns <= 2_000_000_000):
            self._missing_entry_unknown(identity, "lookup_binding_or_freshness")
            return None
        if result.kind == "unknown":
            self._missing_entry_unknown(identity, result.reason)
            return None
        return result

    async def _cancel_entry(self, identity):
        snapshot = await self._inspect(identity)
        for row in snapshot["orders"]:
            request = row["request"]
            if request.side == "buy" and request.intent_type == "open" and row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}:
                values = self.runtime.setdefault(identity, {})
                if values.get("cancel_wire_sent_coid"):
                    if values["cancel_wire_sent_coid"] != request.client_order_id:
                        raise Refusal("cancel_original_identity_CHANGED")
                    return
                if not row["broker_order_id"] and "submitted_at" in row and row["submitted_at"] is None:
                    if self.actor is not None and getattr(self.actor, "entry_inflight_symbol", None) == request.symbol:
                        return  # An active prewire/POST await is not a retained failed entry.
                    try:
                        outcome = await self._lookup_missing_entry(identity, request)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._missing_entry_unknown(identity, type(exc).__name__)
                        return
                    if outcome is None:
                        return
                    if outcome.kind == "absent":
                        try:
                            closed_ns = await self.service._run_db(lambda session: self.db.retire_broker_absent_entry(
                                session, request, outcome, now_ns=self.now_ns(),
                                sync_pass=getattr(self.service, "_reserve1_sync_pass_id", None)))
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            self._missing_entry_unknown(identity, type(exc).__name__)
                            return
                        self.book.reconcile(identity, cumulative_filled=Decimal(0),
                            cumulative_exited=Decimal(0), now_ns=self.now_ns(), terminal_entry=True)
                        self.book.episodes[identity].closed_ns = closed_ns
                        self._missing_entry_pass.pop(request.client_order_id, None)
                        self.service.logger.info(
                            "[MOMCANCEL1] episode=%s coid=%s broker_client_lookup=ABSENT "
                            "order=aborted parent=cancelled state=terminal_no_fill slot=freed "
                            "no_wire=true settlement=refused_pre_submit_proven",
                            identity, request.client_order_id)
                        return
                    if outcome.kind != "present" or outcome.report is None:
                        self._missing_entry_unknown(identity, "lookup_outcome_invalid")
                        return
                    try:
                        report = self.backend._validate_fresh_report(outcome.report, request)
                        await self._record(request, [report])
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        self._missing_entry_unknown(identity, type(exc).__name__)
                        return
                    if self._cancel_read_terminal(report, request):
                        return
                    request = replace(request, metadata={**request.metadata,
                        "broker_order_id": report.broker_order_id})
                if self.provider(request.broker_account_name) == "schwab" and (
                        not request.metadata.get("broker_order_id")
                        or request.metadata.get("momcancel1_ack_identity") == "UNKNOWN"):
                    await self._refuse_entry_identity(identity)
                report = await self._fresh_cancel_read(request)
                if self._cancel_read_terminal(report, request):
                    return
                sent = self.book.episodes[identity].first_submit_ns
                if report is None and (sent is None or self.now_ns() - sent < 2_000_000_000):
                    return
                # Reports belong to the cancel request; only the subsequent fresh
                # ORIGINAL order read may mark the entry terminal or add its fills.
                cancel = replace(request, intent_type="cancel",
                    quantity=Decimal(0), reason="MOMLIVE1_2S_CANCEL_REMAINDER")
                control = getattr(self.service, "_bot_control_runtime", None)
                if control is not None and self._stop_requested():
                    if not await control.begin_cleanup(request, "CANCEL_ENTRY"):
                        return
                values["cancel_wire_sent_coid"] = request.client_order_id
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
                try:
                    await self.service.broker_adapter.submit_order(cancel)
                except Exception as exc:
                    self.service.logger.warning("[MOMORDER1-CANCEL-UNKNOWN] episode=%s coid=%s failure=%s",
                        identity, request.client_order_id, type(exc).__name__)
                finally:
                    await self._fresh_cancel_read(request)

    async def _save_handle(self, handle):
        values = asdict(handle)
        values.update(quantity=str(handle.quantity), cumulative_exited=str(handle.cumulative_exited),
                      child_ids=list(handle.child_ids))
        self.runtime.setdefault(handle.episode_id, {}).update(protection=values,
            protection_generation=self.native.generations.get(handle.episode_id, 1))
        await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == handle.episode_id))

    async def _prepare_protection(self, request):
        await self._prepare(request.metadata["episode_id"], request, self.provider(request.broker_account_name))

    async def _submit_protection(self, request):
        reports = await self._submit(request)
        await self._record(request, reports)
        return reports

    def _exit_gate_fired(self, position, reason, decision_ns):
        try:
            self._exit_gate_fire = (asyncio.current_task(), position.episode_id, decision_ns,
                                    reason, position.exit_clock_source, position.exit_clock_ns)
        except Exception:
            pass  # Original actor clock read only; no diagnostic business clock.

    def _exit_gate_state(self, identity):
        states = getattr(self, "_exit_gate_states", None)
        if not isinstance(states, dict):
            return None
        if identity not in states:
            if len(states) >= 2:
                return None
            states[identity] = {"root": None, "latch_ns": None, "post_cancel": False,
                "entry_gate": None, "outcomes": {}, "steps": {}, "lines": 0,
                "suppressed": 0, "last_summary_suppressed": 0, "sink_errors": 0,
                "retired": False, "generation_changed": False, "clock": None, "overlap": False}
        return states[identity]

    def _exit_gate_cancel_latched(self, handle):
        try:
            state = self._exit_gate_state(handle.episode_id)
            if state is None:
                return
            if state["root"] not in (None, handle.base_coid):
                state.update(latch_ns=None, entry_gate=None, post_cancel=False, generation_changed=True)
            state["root"] = handle.base_coid
            state["post_cancel"] = True
            if state["latch_ns"] is None:
                state["latch_ns"] = time.monotonic_ns()
        except Exception:
            pass  # Called ONLY after original successful _save_handle, before SDK.

    def _exit_gate_open(self, position, fire):
        task = asyncio.current_task()
        state = self._exit_gate_state(position.episode_id)
        if state is None:
            return None
        handle = self.native.handles.get(position.episode_id)
        if handle is not None:
            if state["root"] not in (None, handle.base_coid):
                state.update(latch_ns=None, entry_gate=None, post_cancel=False, generation_changed=True)
            state["root"] = handle.base_coid
            if self.runtime.get(position.episode_id, {}).get(f"native_cancel_wires:{handle.base_coid}"):
                state["post_cancel"] = True  # Restored latch WITHOUT an invented clock.
        observed = fire[2] if fire is not None and fire[:2] == (task, position.episode_id) else None
        if observed is not None and fire[3] == "time_300s" and state["clock"] is None:
            state["clock"] = (fire[4], fire[5], observed)
        trace = {"state": state, "steps": state["steps"],
                 "outcome": "returned", "active": None, "attempt_journal_saved": False,
                 "attempt_logged": False}
        frames = getattr(self, "_exit_gate_frames", None)
        key = (position.episode_id, task)
        if isinstance(frames, dict) and any(k[0] == position.episode_id for k in frames):
            state["overlap"] = True
        trace["bound"] = isinstance(frames, dict) and len(frames) < 2 and key not in frames
        if trace["bound"]:
            frames[key] = trace
        return trace

    def _exit_gate_change(self, identity, state, gate, event, before, after):
        # CONDITIONAL proposal: 20 applies to ENTRY/CHANGE calls; attempt summary extra.
        try:
            if not state["post_cancel"]:
                return
            if state["lines"] >= 20:
                state["suppressed"] += 1
                return
            state["lines"] += 1  # Count attempted INFO calls, including logger failure.
            now = time.monotonic_ns()
            since = (now - state["latch_ns"]) / 1e6 if state["latch_ns"] is not None else None
            self.service.logger.info("[MOMLIVE1-EXIT-GATE] runtime=%s episode=%s event=%s "
                "gate=%s from=%s to=%s since_local_latch_save_ms=%s latch_clock=%s "
                "line=%s cap_scope=process_episode",
                self._exit_gate_instance, identity, event, gate, before, after, since,
                "observed_after_save" if since is not None else "UNKNOWN", state["lines"])
        except Exception:
            state["sink_errors"] += 1

    def _exit_gate_start(self, trace, gate):
        try:
            if trace is not None:
                state = trace["state"]
                if state["post_cancel"] and state["entry_gate"] != gate:
                    previous = state["entry_gate"] or "unobserved"
                    state["entry_gate"] = gate
                    self._exit_gate_change(trace["identity"], state, gate, "ENTRY", previous, gate)
                started = time.monotonic_ns()  # AFTER entry logging; avoid charging that IO to the await.
                trace["active"] = (gate, started)
                return started
        except Exception:
            pass
        return None

    def _exit_gate_end(self, trace, gate, started, outcome):
        try:
            if trace is None or started is None:
                return
            ended = time.monotonic_ns()
            if trace["active"] == (gate, started):
                trace["active"] = None
            state = trace["state"]
            value = trace["steps"].get(gate)
            if value is None:
                value = trace["steps"][gate] = [started, ended, 0, 0, 0, outcome]
            duration = ended - started
            value[1], value[2], value[3] = ended, value[2] + duration, max(value[3], duration)
            value[4], value[5] = value[4] + 1, outcome
            if state["post_cancel"]:
                previous = state["outcomes"].get(gate, "unobserved")
                if previous != outcome:
                    state["outcomes"][gate] = outcome
                    self._exit_gate_change(trace["identity"], state, gate, "CHANGE", previous, outcome)
        except Exception:
            pass

    def _exit_gate_attempt(self, trace, position, reason, values, site):
        if trace is None or not trace["attempt_journal_saved"] or trace["attempt_logged"]:
            return
        trace["attempt_logged"] = True
        state = trace["state"]
        try:
            gates = ("terminal_before_prepare", "terminal_after_prepare", "fresh_pre_release",
                     "fresh_post_release", "positions", "foreign_vp", "fresh_pre_ceiling",
                     "owned_ceiling", "fresh_pre_send")
            steps = tuple((gate, *(v / 1e6 for v in trace["steps"][gate][:4]),
                           *trace["steps"][gate][4:]) if gate in trace["steps"]
                          else (gate, None, None, None, None, 0, "not_reached") for gate in gates)
            source, anchor, fired = state["clock"] or ("UNKNOWN", None, None)
            if reason != "time_300s":
                source, anchor, fired = "UNKNOWN", None, None
            due = anchor + 300_000_000_000 if reason == "time_300s" and type(anchor) is int else None
            proven = source in ("send", "first_execution") and due is not None and type(fired) is int
            suppressed = state["suppressed"] - state["last_summary_suppressed"]
            state["last_summary_suppressed"] = state["suppressed"]
            self.service.logger.info("[MOMLIVE1-EXIT-ATTEMPT-TIMING] runtime=%s episode=%s "
                "attempt=%s site=%s eval_outcome=%s steps_ms=%s "
                "step_fields=gate,first_start,last_end,total,max,calls,last_outcome incomplete=%s "
                "suppressed=%s suppressed_total=%s change_calls_total=%s sink_errors=%s "
                "terminal_binding=%s generation_changed=%s overlap=%s "
                "clock_site=first_locally_observed_time_300s_decision clock_source=%s "
                "due_business_ns=%s fired_business_ns=%s late_ns=%s clock_proven=%s",
                self._exit_gate_instance, position.episode_id, values.get("soft_exit_attempt"),
                site, trace["outcome"], steps,
                (trace["active"][0], trace["active"][1] / 1e6, "end_UNKNOWN") if trace["active"] else None,
                suppressed,
                state["suppressed"], state["lines"], state["sink_errors"], trace["bound"],
                state["generation_changed"], state["overlap"], source if source in ("send", "first_execution") else "UNKNOWN",
                due, fired, fired - due if proven else None, proven)
        except Exception:
            state["sink_errors"] += 1
        finally:
            state["steps"] = {}  # New attempt's fixed aggregate; no event history.

    def _exit_gate_finish(self, trace, position, reason, values):
        try:
            frames = getattr(self, "_exit_gate_frames", {})
            key = (position.episode_id, asyncio.current_task())
            if trace is not None and frames.get(key) is trace:
                frames.pop(key)
            self._exit_gate_attempt(trace, position, reason, values, "return_or_error_finally")
            state = trace["state"] if trace is not None else None
            if (state is not None and state["retired"]
                    and not any(k[0] == position.episode_id for k in frames)):
                self._exit_gate_states.pop(position.episode_id, None)
        except Exception:
            pass

    def _exit_gate_retire(self, identity):
        try:
            state = self._exit_gate_states.get(identity)
            if state is not None:
                state["retired"] = True
                if not any(key[0] == identity for key in self._exit_gate_frames):
                    self._exit_gate_states.pop(identity, None)
        except Exception:
            pass

    def _protection_market_session(self):
        local = self.clock().astimezone(ET)
        return ("AM" if (local.hour, local.minute) < (9, 30)
                else "NORMAL" if (local.hour, local.minute) < (16, 0) else "PM")

    async def _protect(self, position):
        position.market_session = self._protection_market_session()
        snapshot = await self._inspect(position.episode_id)
        if any(row["request"].metadata.get("exit_reason")
               and row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}
               for row in snapshot["orders"]):
            position.release_unknown = True
            return
        if self.runtime.get(position.episode_id, {}).get("soft_exit_reason"):
            # The triggered software exit remains watched until flat. Do not
            # attach a new pair while its close/cancel result is unresolved.
            return
        await self.native.ensure(position, broker=self.provider(position.account),
                                 session=position.market_session)

    async def _read_protection(self, handle):
        trace = None
        gate = "terminal_before_prepare"
        try:
            frames = getattr(self, "_exit_gate_frames", {})
            trace = frames.get((handle.episode_id, asyncio.current_task()))
            prepared = bool(self.runtime.get(handle.episode_id, {}).get(f"native_cancel_wires:{handle.base_coid}"))
            gate = "terminal_after_prepare" if prepared else "terminal_before_prepare"
        except Exception:
            pass
        started = self._exit_gate_start(trace, gate)
        outcome = "exception"
        try:
            read = await self.backend.read(handle, self)
            try:
                outcome = "terminal" if read.terminal else "working" if read.working else "unknown"
            except Exception:
                outcome = "unknown"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            self._exit_gate_end(trace, gate, started, outcome)
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is not None and self._stop_requested():
            if (read.episode_id != handle.episode_id or read.account != handle.account
                    or read.entry_order_id != handle.entry_order_id or read.base_coid != handle.base_coid
                    or handle.child_ids and read.child_ids != handle.child_ids
                    or (read.working and read.terminal)
                    or not read.cumulative_exited.is_finite()
                    or read.cumulative_exited < handle.cumulative_exited):
                raise Refusal("KILLSW1:native_tree_identity_unknown")
            row, snapshot = await self.backend.root(handle, self)
            if read.cumulative_exited > snapshot["quantity"]:
                raise Refusal("KILLSW1:native_tree_quantity_unknown")
            await control.record_cleanup_tree(row["request"], read)
        return read

    async def _cancel_protection(self, handle):
        try:
            await self.backend.cancel(handle, self)
        except Exception:
            # No UNKNOWN read may strand the generic cancel-request latch. Per-leg
            # durable wire latches in the backend prevent resends after a wire.
            handle.status = "unknown"
            await self._save_handle(handle)
            raise

    def _soft_exit_chase_window(self):
        local = self.clock().astimezone(ET).time()
        return daytime(4) <= local < daytime(9, 30) or daytime(16) <= local < daytime(20)

    @staticmethod
    def _mandatory_chase(position, reason):
        return (reason in {"hard_stop_15pct", "time_300s", "KILLSW1_STOP", "EOD"}
                or reason == "target_5pct" and position.software_target_allowed)

    async def _chase_cancel(self, identity, request, values, *, mandatory=False, reason=None):
        now = self.now_ns()
        if values.get("soft_exit_cancel_coid") == request.client_order_id:
            last = values.get("soft_exit_cancel_completed_ns") or values.get("soft_exit_cancel_verified_ns")
            if last is None:
                # Restart after an uncertain cancel: this fresh working-order
                # read establishes a new conservative baseline, never a resend.
                values["soft_exit_cancel_verified_ns"] = now
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
                return False
            if now - int(last) < 2_000_000_000:
                return False
        report = await self._fresh_cancel_read(request)
        if self._cancel_read_terminal(report, request):
            return False
        if report is None:
            if not mandatory or not request.metadata.get("broker_order_id"):
                return False
            self.service.logger.warning(
                "[SAFEX1-R7-CANCEL-READ-UNKNOWN] episode=%s coid=%s reason=%s action=cancel terminal_proof=required",
                identity, request.client_order_id, reason)
        values["soft_exit_cancel_coid"] = request.client_order_id
        values.pop("soft_exit_cancel_completed_ns", None)
        values.pop("soft_exit_cancel_verified_ns", None)
        await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
        result = "UNKNOWN"
        try:
            reports = await self._submit(replace(request, intent_type="cancel", quantity=Decimal(0),
                                                 reason="MOMLIVE1_SOFTEXIT_CHASE_CANCEL"))
            result = ",".join(report.event_type for report in reports) or "UNKNOWN"
        except Exception as exc:
            result = type(exc).__name__  # Ambiguous/417 must still read the ORIGINAL order.
        finally:
            await self._fresh_cancel_read(request)
            values["soft_exit_cancel_completed_ns"] = self.now_ns()
            await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == identity))
            self.service.logger.info("[MOMLIVE1-EXIT-CHASE] episode=%s attempt=%s action=cancel coid=%s bid=%s original_qty=%s result=%s",
                                     identity, values.get("soft_exit_attempt"), request.client_order_id,
                                     self._exit_quote(request.symbol).get("bid"), request.quantity, result)
        return True

    async def _control_receipt_ready(self, position, values):
        from project_mai_tai.oms.bot_control_evidence import positions_after_protection

        checked = values.get("killsw1_receipt_check_ns")
        if checked is not None and 0 <= self.now_ns() - checked < 1_000_000_000:
            return values.get("killsw1_receipt_ready") is True
        try:
            terminal = datetime.fromisoformat(values["killsw1_protection_terminal_at"])
            await self.service._run_db(lambda session: positions_after_protection(
                session, account_name=position.account, terminal_at=terminal, now=self.clock(),
            ), commit=False)
        except (KeyError, ValueError) as exc:
            blocked = str(exc)
            if values.get("killsw1_wait") != blocked:
                self.service.logger.warning("[KILLSW1-BLOCKED] episode=%s reason=%s sell=0",
                                            position.episode_id, blocked)
            values["killsw1_wait"] = blocked
            ready = False
        else:
            values.pop("killsw1_wait", None)
            ready = True
        values["killsw1_receipt_check_ns"] = self.now_ns()
        values["killsw1_receipt_ready"] = ready
        return ready  # Final quantity proof rechecks freshness after the broker await.

    async def _exit(self, position, reason):
        fire = getattr(self, "_exit_gate_fire", None)
        self._exit_gate_fire = None
        values = self.runtime.setdefault(position.episode_id, {})
        if position.remaining <= 0:
            return
        trace = None
        try:
            trace = self._exit_gate_open(position, fire)
            if trace is not None:
                trace["identity"] = position.episode_id
        except Exception:
            pass
        try:
            control_market = False
            if reason == "KILLSW1_STOP":
                if values.get("killsw1_broker_refused"):
                    return
                local = self.clock().astimezone(ET)
                provider = self.provider(position.account)
                if (local.weekday() >= 5 or local.date() in US_MARKET_HOLIDAYS
                        or not (4, 0) <= (local.hour, local.minute) < (20, 0)
                        or provider == "schwab" and local.hour < 7):
                    last_log = values.get("killsw1_session_blocked_log_ns")
                    if last_log is None or self.now_ns() - last_log >= 30_000_000_000:
                        self.service.logger.warning("[KILLSW1-BLOCKED] episode=%s reason=broker_session protection=retained",
                                                    position.episode_id)
                        values["killsw1_session_blocked_log_ns"] = self.now_ns()
                    values["killsw1_session_blocked"] = True
                    return
                values.pop("killsw1_session_blocked", None)
                control_market = (9, 30) <= (local.hour, local.minute) < (16, 0)
                snapshot = await self._inspect(position.episode_id)
                if not snapshot["entry_terminal"]:
                    return  # Entry cancellation must prove terminal before any protection release.
                handle = self.native.handles.get(position.episode_id)
                terminal_key = handle.base_coid if handle is not None else position.entry_coid + ":no-native"
                if (not values.get("soft_exit_sent") and values.get("killsw1_terminal_key") == terminal_key
                        and not await self._control_receipt_ready(position, values)):
                    return
            if (not values.get("soft_exit_reason")
                    or values.get("soft_exit_reason") == "target_5pct"
                    and reason in {"hard_stop_15pct", "time_300s", "KILLSW1_STOP"}):
                values["soft_exit_reason"] = reason
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
            position.pending_exit_reason = values["soft_exit_reason"]
            mandatory_chase = self._mandatory_chase(position, position.pending_exit_reason)
            # Completion is conservative relative to dispatch. The pre-save journal
            # timestamp cannot pace actual calls when DB latency differs per attempt.
            last = (values.get("soft_exit_dispatch_completed_ns") or values.get("soft_exit_terminal_verified_ns")
                    or values.get("soft_exit_working_verified_ns"))
            if last is not None and self.now_ns()-int(last) < 2_000_000_000:
                return
            cancel_last = values.get("soft_exit_cancel_completed_ns") or values.get("soft_exit_cancel_verified_ns")
            if values.get("soft_exit_sent"):
                snapshot = await self._inspect(position.episode_id)
                exits = [row for row in snapshot["orders"] if row["request"].metadata.get("exit_reason")
                         and (not values.get("soft_exit_coid")
                              or row["request"].client_order_id == values["soft_exit_coid"])]
                if len(exits) != 1:
                    raise Refusal("soft_exit_order_identity_UNKNOWN")
                row = exits[0]
                await self._read_missing_execution_time(position.episode_id, row)
                terminal = {"filled", "cancelled", "rejected", "expired"}
                if (row["status"] not in terminal and self._soft_exit_chase_window()
                        and values.get("soft_exit_cancel_coid") == values.get("soft_exit_coid")
                        and cancel_last is not None and self.now_ns()-int(cancel_last) < 2_000_000_000):
                    return
                if (row["status"] not in terminal
                        or row["request"].metadata.get("momlive_terminal_broker_event") != row["status"]):
                    read_ns = values.get("soft_exit_read_ns")
                    if read_ns is not None and self.now_ns()-int(read_ns) < 1_000_000_000:
                        return
                    values["soft_exit_read_ns"] = self.now_ns()
                    request = row["request"]
                    if self._soft_exit_chase_window():
                        request = replace(request, metadata={**request.metadata, "momlive_fresh_order_read": "true"})
                    report = await self.backend.fetch_entry_update(request, self)
                    if report is None:
                        # Mandatory exits may cancel their known original order on
                        # UNKNOWN; the post-cancel terminal proof still gates SELL.
                        if not mandatory_chase or not self._soft_exit_chase_window():
                            return
                    else:
                        await self._record(row["request"], [report])
                    snapshot = await self._inspect(position.episode_id)
                    row = next(item for item in snapshot["orders"]
                               if item["request"].client_order_id == row["request"].client_order_id)
                if (row["status"] not in terminal
                        or row["request"].metadata.get("momlive_terminal_broker_event") != row["status"]):
                    if (not self._soft_exit_chase_window()
                            or report is not None and (report.origin != "broker"
                                or report.event_type not in {"accepted", "partially_filled"})):
                        return
                    if last is None:
                        values["soft_exit_working_verified_ns"] = self.now_ns()
                        await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
                        return
                    fresh = self._exit_quote(position.symbol)
                    stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
                    if (stamp is None or bid is None or bid <= 0
                            or not 0 <= self.now_ns()-stamp <= 2_000_000_000
                            or stamp <= int(values.get("soft_exit_quote_ns", last))):
                        return
                    if not await self._chase_cancel(position.episode_id, row["request"], values,
                            mandatory=mandatory_chase, reason=position.pending_exit_reason):
                        return
                    request = replace(row["request"], metadata={**row["request"].metadata,
                                                               "momlive_fresh_order_read": "true"})
                    report = await self.backend.fetch_entry_update(request, self)
                    if report is None:
                        return
                    await self._record(row["request"], [report])
                    snapshot = await self._inspect(position.episode_id)
                    row = next(item for item in snapshot["orders"]
                               if item["request"].client_order_id == request.client_order_id)
                    if (row["status"] not in terminal
                            or row["request"].metadata.get("momlive_terminal_broker_event") != row["status"]):
                        return
                if snapshot["outstanding_exit"]:
                    return
                position.exited = snapshot["exited"]
                if position.remaining <= 0:
                    return
                if last is None:
                    # Crash/legacy journal without completion: terminal proof fences
                    # the unknown call, but its actual dispatch time is not known.
                    values["soft_exit_terminal_verified_ns"] = self.now_ns()
                    await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
                    return
                values["soft_exit_sent"] = False
                if position.episode_id not in self.native.handles:
                    position.release_unknown, position.reserved = False, Decimal(0)
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
            gate_started = self._exit_gate_start(trace, "fresh_pre_release")
            fresh = self._exit_quote(position.symbol)
            stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
            if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000
                    or last is not None and stamp <= int(values.get("soft_exit_quote_ns", last))):
                self._exit_gate_end(trace, "fresh_pre_release", gate_started, "wait")
                return
            self._exit_gate_end(trace, "fresh_pre_release", gate_started, "ready")
            if not await self.native.release_for_exit(position) or position.remaining <= 0:
                return
            if reason == "KILLSW1_STOP":
                handle = self.native.handles.get(position.episode_id)
                terminal_key = handle.base_coid if handle is not None else position.entry_coid + ":no-native"
                if values.get("killsw1_terminal_key") != terminal_key:
                    values.update(killsw1_terminal_key=terminal_key,
                                  killsw1_protection_terminal_at=self.clock().isoformat())
                    values.pop("killsw1_receipt_check_ns", None)
                    await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
                if not await self._control_receipt_ready(position, values):
                    return
            gate_started = self._exit_gate_start(trace, "fresh_post_release")
            fresh = self._exit_quote(position.symbol)
            stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
            if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000):
                self._exit_gate_end(trace, "fresh_post_release", gate_started, "wait")
                return  # Durable watched exit stays armed after confirmed release.
            self._exit_gate_end(trace, "fresh_post_release", gate_started, "ready")
            gate_started = self._exit_gate_start(trace, "positions")
            gate_outcome = "exception"
            try:
                holdings = await self.service.broker_adapter.list_account_positions(position.account)
                gate_outcome = "returned"
            except asyncio.CancelledError:
                gate_outcome = "cancelled"
                raise
            finally:
                self._exit_gate_end(trace, "positions", gate_started, gate_outcome)
            if reason == "KILLSW1_STOP":
                matching = [row for row in holdings if row.symbol == position.symbol]
                if len(matching) != 1 or matching[0].broker_account_name != position.account:
                    return  # Missing/duplicate/misrouted holdings is UNKNOWN, never zero.
            total = next((row.quantity for row in holdings if row.symbol == position.symbol), None)
            def foreign(session):
                own = session.scalar(select(Strategy.id).where(Strategy.code == STRATEGY_CODE))
                account = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == position.account))
                rows = session.scalars(select(VirtualPosition).where(VirtualPosition.broker_account_id == account,
                    VirtualPosition.symbol == position.symbol, VirtualPosition.strategy_id != own)).all()
                return sum((max(Decimal(0), row.quantity) for row in rows), Decimal(0))
            if reason == "KILLSW1_STOP":
                reserved_foreign = Decimal(0)
            else:
                gate_started = self._exit_gate_start(trace, "foreign_vp")
                gate_outcome = "exception"
                try:
                    reserved_foreign = await self.service._run_db(foreign, commit=False)
                    gate_outcome = "returned"
                except asyncio.CancelledError:
                    gate_outcome = "cancelled"
                    raise
                finally:
                    self._exit_gate_end(trace, "foreign_vp", gate_started, gate_outcome)
            if reason == "KILLSW1_STOP":
                from project_mai_tai.oms.bot_control_evidence import owned_exit_ceiling, positions_after_protection
                def ceiling(session):
                    terminal_at = datetime.fromisoformat(values["killsw1_protection_terminal_at"])
                    positions_after_protection(session, account_name=position.account,
                                               terminal_at=terminal_at, now=self.clock())
                    own = session.scalar(select(Strategy.id).where(Strategy.code == STRATEGY_CODE))
                    account = session.scalar(select(BrokerAccount.id).where(BrokerAccount.name == position.account))
                    snapshot = self.db.inspect_episode(session, position.episode_id)
                    matching = [item for item in snapshot["orders"]
                                if item["request"].client_order_id == position.entry_coid
                                and item["request"].broker_account_name == position.account
                                and item["request"].symbol == position.symbol]
                    if (len(matching) != 1 or not snapshot["entry_terminal"] or snapshot["outstanding_exit"]
                            or own is None or account is None):
                        raise Refusal("KILLSW1:owned_orders_not_terminal")
                    cap = owned_exit_ceiling(session, strategy_id=own, account_id=account, symbol=position.symbol,
                        remaining=snapshot["quantity"] - snapshot["exited"], broker_total=total)
                    return cap, snapshot["exited"]
                try:
                    total, exited = await self.service._run_db(ceiling, commit=False)
                except ValueError as exc:
                    blocked = str(exc)
                    if values.get("killsw1_wait") != blocked:
                        self.service.logger.warning("[KILLSW1-BLOCKED] episode=%s reason=%s sell=0",
                                                    position.episode_id, blocked)
                        values["killsw1_wait"] = blocked
                    return
                values.pop("killsw1_wait", None)
                position.exited = max(position.exited, exited)
                reserved_foreign = Decimal(0)  # Reviewed22:2x cap is own fills/own VP/broker total.
            gate_started = self._exit_gate_start(trace, "fresh_pre_ceiling")
            fresh = self._exit_quote(position.symbol)
            stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
            if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000):
                self._exit_gate_end(trace, "fresh_pre_ceiling", gate_started, "wait")
                return
            self._exit_gate_end(trace, "fresh_pre_ceiling", gate_started, "ready")
            local = self.clock().astimezone(ET)
            session = "NORMAL" if (9, 30) <= (local.hour, local.minute) < (16, 0) else "AM" if local.hour < 9 else "PM"
            if control_market and session != "NORMAL":
                return  # Session changed while releasing protection; next pass reprices for EH.
            control_market = reason == "KILLSW1_STOP" and session == "NORMAL"
            gate_started = self._exit_gate_start(trace, "owned_ceiling")
            gate_outcome = "exception"
            try:
                quantity = position.claim_exit_quantity(broker_available=total, foreign_reserved=reserved_foreign)
                gate_outcome = "positive" if quantity > 0 else "zero"
            finally:
                self._exit_gate_end(trace, "owned_ceiling", gate_started, gate_outcome)
            if quantity <= 0:
                return
            tick, limit = None, None
            if not control_market:
                tick = Decimal(".0001") if bid < 1 else Decimal(".01")
                limit = (bid/tick).to_integral_value(rounding=ROUND_FLOOR)*tick
                if limit <= 0:
                    raise Refusal("exit_limit_tick_unrepresentable")
            attempt = int(values.get("soft_exit_attempt", 0))+1
            coid = f"ml1-{position.symbol}-x-{position.episode_id.replace('-', '')[:20]}"
            if attempt > 1:
                coid += f"-{attempt}"
            if len(coid) > 38:
                raise Refusal("soft_exit_identity_capacity_UNKNOWN")
            exit_metadata = {"episode_id": position.episode_id, "momlive_strategy": STRATEGY_CODE,
                 "momlive_one_shot": "true", "session": session,
                 "clock_source": position.exit_clock_source, "clock_start_ns": str(position.exit_clock_ns),
                 "extended_hours": "false" if session == "NORMAL" else "true", "exit_reason": reason}
            if not control_market:
                exit_metadata["limit_price"] = str(limit)
                exit_metadata["reference_price"] = str(limit)
            request = OrderRequest(coid,
                position.account, STRATEGY_CODE, position.symbol, "sell", "close", quantity, reason,
                exit_metadata, "market" if control_market else "limit", "day")
            await self._prepare(position.episode_id, request, self.provider(position.account))
            # A new uncertain call must never inherit the preceding call's pace
            # proof if the process dies before persisting its own completion.
            values.pop("soft_exit_dispatch_completed_ns", None)
            values.pop("soft_exit_terminal_verified_ns", None)
            values.pop("soft_exit_working_verified_ns", None)
            if control_market and (stamp is None or not isinstance(bid, Decimal) or not bid.is_finite()
                                   or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000):
                stamp = None
            values.update(soft_exit_sent=True, soft_exit_coid=coid, soft_exit_attempt=attempt,
                          soft_exit_attempt_ns=self.now_ns(), soft_exit_quote_ns=stamp)
            if stamp is None:
                values.pop("soft_exit_quote_ns", None)  # UNKNOWN, not an invented quote timestamp.
            if reason == "KILLSW1_STOP":
                values["soft_exit_reason"] = reason  # New attempt only; prior owned-exit history is unchanged.
            await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
            if trace is not None:
                trace["attempt_journal_saved"] = True  # Only after the original successful _save.
            gate_started = self._exit_gate_start(trace, "fresh_pre_send")
            checked = self._exit_quote(position.symbol)
            checked_stamp, checked_bid = checked.get("observed_ns"), checked.get("bid")
            control_session_closed = False
            if control_market:
                now_local = self.clock().astimezone(ET)
                control_session_closed = not (9, 30) <= (now_local.hour, now_local.minute) < (16, 0)
            if (not control_market and (checked_stamp is None or checked_bid is None or checked_bid <= 0
                    or not 0 <= self.now_ns()-checked_stamp <= 2_000_000_000
                    or (checked_bid/tick).to_integral_value(rounding=ROUND_FLOOR)*tick != limit)
                    or control_session_closed):
                self._exit_gate_end(trace, "fresh_pre_send", gate_started, "wait")
                # Still in this pre-wire frame: known UNSENT, not a broker rejection
                # or ambiguous ACK. Mark only this own order; keep the exit armed.
                await self.service._run_db(lambda unit: self.db.abandon_unsent_exit(unit, request))
                values["soft_exit_sent"] = False
                position.reserved = Decimal(0)
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
                return
            self._exit_gate_end(trace, "fresh_pre_send", gate_started, "ready")
            self._exit_gate_attempt(trace, position, reason, values, "pre_submit")
            self.service.logger.info("[MOMLIVE1-EXIT] episode=%s symbol=%s attempt=%s quantity=%s limit=%s reason=%s",
                position.episode_id, position.symbol, attempt, quantity, limit, values["soft_exit_reason"])
            control = getattr(self.service, "_bot_control_runtime", None)
            if control is not None and self._stop_requested():
                if not await control.begin_cleanup(request, "SUBMIT_EXIT"):
                    return
            try:
                reports = await self._submit(request)
            finally:
                # An exception/cancel is still UNKNOWN ACK, not a terminal order.
                # Keep the durable sent fence; only pace from call completion/read.
                values["soft_exit_dispatch_completed_ns"] = self.now_ns()
                await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
            await self._record(request, reports)
            self.service.logger.info("[MOMLIVE1-EXIT-CHASE] episode=%s attempt=%s action=submit bid=%s quantity=%s result=%s",
                                     position.episode_id, attempt, bid, quantity,
                                     ",".join(report.event_type for report in reports) or "UNKNOWN")
            if any(report.origin == "broker" and report.event_type == "filled" for report in reports):
                current = await self._inspect(position.episode_id)
                filled = next((row for row in current["orders"]
                               if row["request"].client_order_id == request.client_order_id), None)
                if filled is not None:
                    await self._read_missing_execution_time(position.episode_id, filled)
        except BaseException as exc:
            if trace is not None:
                trace["outcome"] = "cancelled" if isinstance(exc, asyncio.CancelledError) else "exception"
            raise
        finally:
            self._exit_gate_finish(trace, position, reason, values)


class OwnedNativeBackend:
    """Existing broker capabilities, scoped to the durable own protection root."""

    async def lookup_missing_entry(self, request, runtime, sync_pass):
        provider = runtime.provider(request.broker_account_name)
        def outcome(kind, reason, report=None):
            return MomentumEntryLookup(kind, request.broker_account_name,
                request.client_order_id, request.symbol, provider, sync_pass,
                runtime.now_ns(), reason, report)
        if (request.strategy_code != STRATEGY_CODE or request.side != "buy" or request.intent_type != "open"
                or request.metadata.get("momlive_strategy") != STRATEGY_CODE
                or request.metadata.get("momlive_one_shot") != "true"
                or type(request.metadata.get("episode_id")) is not str
                or request.client_order_id != "ml1-" + request.symbol + "-e-"
                    + request.metadata["episode_id"].replace("-", "")[:20]):
            return outcome("unknown", "original_momentum_entry_required")
        marker = request.metadata.get("refused_pre_submit") is True
        if marker and provider == "schwab":
            # This read is only a candidate. The DB transaction rechecks the
            # first-attempt seal and every dispatch/execution exclusion locked.
            return outcome("absent", "refused_pre_submit_proven")
        if provider == "schwab":
            # Schwab does not transmit/query the local ml1 coid. An account list
            # (including an empty or under-cap list) is not generic absence proof.
            return outcome("unknown", "schwab_canonical_client_id_lookup_unavailable")
        leaf = self.leaf(request.broker_account_name, runtime)
        if (request.broker_account_name not in getattr(leaf, "accounts_by_name", {})
                or not callable(getattr(leaf, "fetch_momentum_cancel_read", None))):
            return outcome("unknown", "canonical_client_id_reader_unavailable")
        result = await leaf.fetch_momentum_cancel_read(request, allow_absent=marker, lookup_missing_entry=True)
        if type(result) is not MomentumOrderRead:
            return outcome("unknown", "lookup_result_invalid")
        if (marker and result.kind == "absent" and result.report is None
                and result.reason == "webull_exact_client_id_ORDER_NOT_FOUND"):
            return outcome("absent", result.reason)
        if result.kind != "report" or result.report is None:
            return outcome("unknown", result.reason or "lookup_not_conclusive")
        report = await self._fetch_entry_update(request, runtime, supplied_report=result.report)
        report = self._validate_fresh_report(report, request)
        return outcome("present", "webull_exact_client_id_report", report)

    @staticmethod
    def schwab_children(raw, handle, leaf, *, root_id, first_fill_ns, now_ns, allow_unknown_status=False):
        if not isinstance(raw, dict) or str(raw.get("orderId", "")) != str(root_id):
            raise Refusal("native_schwab_root_shape_UNKNOWN")
        pending, children, seen = [raw], [], set()
        while pending:
            node = pending.pop()
            if not isinstance(node, dict) or len(seen) >= 64:
                raise Refusal("native_schwab_tree_bound_UNKNOWN")
            identity = str(node.get("orderId") or "")
            if identity:
                if identity in seen:
                    raise Refusal("native_schwab_tree_identity_UNKNOWN")
                seen.add(identity)
            branches = node.get("childOrderStrategies", [])
            if not isinstance(branches, list) or len(branches) + len(pending) > 64:
                raise Refusal("native_schwab_tree_bound_UNKNOWN")
            pending.extend(branches)
            legs = node.get("orderLegCollection") or []
            sells = [leg for leg in legs if str(leg.get("instruction", "")).upper() == "SELL"]
            if not sells:
                continue
            if (len(sells) != 1 or len(legs) != 1 or not identity
                    or str((sells[0].get("instrument") or {}).get("symbol", "")).upper() != handle.symbol):
                raise Refusal("native_schwab_child_owner_UNKNOWN")
            status = str(node.get("status", "")).upper()
            terminal = status in {"FILLED"} | leaf.CANCELLED_STATUSES | leaf.REJECTED_STATUSES
            unknown_status = not terminal and status not in leaf.ACCEPTED_STATUSES | leaf.PARTIAL_FILL_STATUSES | {"PARTIALLY_FILLED"}
            if unknown_status and not allow_unknown_status:
                raise Refusal("native_schwab_child_status_UNKNOWN")
            quantity, cash, stamps = Decimal(0), Decimal(0), []
            declared = Decimal(str(node.get("filledQuantity", 0)))
            if not declared.is_finite() or declared < 0:
                raise Refusal("native_schwab_execution_coverage_UNKNOWN")
            activities = node.get("orderActivityCollection") or []
            if len(activities) > 256:
                raise Refusal("native_schwab_execution_bound_UNKNOWN")
            for activity in activities:
                executions = activity.get("executionLegs") or []
                if len(executions) > 256:
                    raise Refusal("native_schwab_execution_bound_UNKNOWN")
                for execution in executions:
                    q = Decimal(str(execution.get("quantity", 0)))
                    if not q.is_finite() or q < 0:
                        raise Refusal("native_schwab_execution_quantity_UNKNOWN")
                    if q == 0:
                        continue
                    price = Decimal(str(execution.get("price", 0)))
                    if price == 0 and status in leaf.CANCELLED_STATUSES:
                        # Schwab's cancelled sibling carries a documented qty>0
                        # @0 phantom. Positive executions must still exactly cover
                        # declared fills below; a phantom never supplies that proof.
                        continue
                    stamp = leaf._parse_datetime(execution.get("time"))
                    if (not price.is_finite() or price <= 0 or stamp is None or stamp.tzinfo is None
                            or not first_fill_ns <= int(stamp.timestamp()*1e9) <= now_ns
                            or sells[0].get("legId") is None
                            or str(execution.get("legId", "")) != str(sells[0].get("legId", ""))):
                        raise Refusal("native_schwab_execution_evidence_UNKNOWN")
                    quantity += q
                    cash += q * price
                    stamps.append(stamp)
            if not declared.is_finite() or declared != quantity or not 0 <= quantity <= handle.quantity:
                raise Refusal("native_schwab_execution_coverage_UNKNOWN")
            if status == "FILLED" and quantity == 0:
                raise Refusal("native_schwab_execution_coverage_UNKNOWN")
            event_type = ("unknown" if unknown_status else "filled" if status == "FILLED" else "rejected" if status in leaf.REJECTED_STATUSES
                          else "cancelled" if terminal else "partially_filled" if quantity else "accepted")
            children.append((identity, terminal, quantity, cash / quantity if quantity else None,
                             max(stamps) if stamps else None, event_type))
        if not children or sum(item[2] for item in children) > handle.quantity:
            raise Refusal("native_schwab_children_quantity_UNKNOWN")
        if handle.child_ids and set(handle.child_ids) != {item[0] for item in children}:
            raise Refusal("native_schwab_children_identity_UNKNOWN")
        return children

    async def root(self, handle, runtime):
        snapshot = await runtime._inspect(handle.episode_id)
        rows = [row for row in snapshot["orders"] if row["request"].client_order_id == handle.base_coid]
        if len(rows) != 1 or rows[0]["request"].broker_account_name != handle.account:
            raise Refusal("native_durable_root_identity_UNKNOWN")
        return rows[0], snapshot

    async def _record_child(self, handle, runtime, report, request):
        def unit(session):
            existing = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == request.client_order_id))
            if existing is None:
                if (runtime.provider(handle.account) == "webull" and report.origin == "broker"
                        and report.event_type in {"cancelled", "rejected", "expired"}
                        and report.filled_quantity == 0):
                    return runtime.db.record_terminal_native_child(session, handle.episode_id, request, report)
                runtime.db.prepare_order(session, handle.episode_id, request, provider=runtime.provider(handle.account))
            return runtime.db.record(session, request, [report])
        await runtime.service._run_db(unit)

    async def read(self, handle, runtime, *, fresh=False):
        row, snapshot = await self.root(handle, runtime)
        adapter = runtime.service.broker_adapter
        request = row["request"]
        provider = runtime.provider(handle.account)
        child_ids = handle.child_ids
        working = terminal = False
        if handle.session != "NORMAL":
            report = await self.fetch_entry_update(request, runtime, fresh=fresh)
            if report is None or report.origin != "broker":
                return await self.unknown_read(handle, runtime, child_ids, snapshot)
            await runtime._record(request, [report])
            child_ids = (request.client_order_id,)
            working = report.event_type in {"accepted", "partially_filled"}
            terminal = report.event_type in {"filled", "cancelled", "rejected", "expired", "aborted"}
        elif provider == "webull":
            leaf = adapter._adapter_for_account(handle.account) if hasattr(adapter, "_adapter_for_account") else adapter
            child_ids = tuple(leaf.exit_pair_leg_client_order_ids(handle.base_coid))
            if len(child_ids) != 2:
                raise Refusal("native_webull_child_identity_UNKNOWN")
            reports = []
            unknown = False
            for coid in child_ids:
                durable = next((item for item in snapshot["orders"]
                                if item["request"].client_order_id == coid), None)
                if (not fresh and durable is not None and durable["broker_order_id"]
                        and durable["request"].broker_account_name == handle.account
                        and durable["request"].metadata.get("momlive_native_child") == "true"
                        and durable["status"] in {"filled", "cancelled", "rejected", "expired", "aborted"}
                        and durable["request"].metadata.get("momlive_terminal_broker_event") == durable["status"]):
                    reports.append(durable["status"])
                    continue
                child = replace(request, client_order_id=coid,
                    metadata={"episode_id": handle.episode_id, "momlive_strategy": STRATEGY_CODE,
                              "momlive_native_child": "true",
                              **({"broker_order_id": durable["broker_order_id"]}
                                 if durable and durable["broker_order_id"] else {})})
                report = await self.fetch_entry_update(child, runtime, fresh=fresh)
                if report is None or report.origin != "broker" or report.client_order_id != coid:
                    unknown = True
                    continue
                await self._record_child(handle, runtime, report, child)
                reports.append(report.event_type)
            if unknown:
                # Preserve any independently verified fill even if its sibling
                # is unreadable. The unknown read cannot release protection.
                snapshot = await runtime._inspect(handle.episode_id)
                return await self.unknown_read(handle, runtime, child_ids, snapshot)
            terminal = all(status in {"filled", "cancelled", "rejected", "expired", "aborted"} for status in reports)
            working = not terminal and all(status in {"accepted", "partially_filled", "filled", "cancelled"}
                                          for status in reports)
        else:
            root_id = row["broker_order_id"]
            if not root_id:
                raise Refusal("native_schwab_root_receipt_UNKNOWN")
            leaf = self.leaf(handle.account, runtime)
            account = leaf.accounts_by_name.get(handle.account)
            raw = await self._schwab_fresh_raw(leaf, account, root_id) if fresh else (
                await leaf._fetch_order(account, root_id) if account else None)
            if raw is None:
                return await self.unknown_read(handle, runtime, child_ids, snapshot)
            children = self.schwab_children(raw, handle, leaf, root_id=root_id,
                first_fill_ns=snapshot["first_fill_ns"] or 0, now_ns=runtime.now_ns(),
                allow_unknown_status=True)
            child_ids = tuple(sorted(item[0] for item in children))
            for broker_id, child_terminal, quantity, price, stamp, event_type in children:
                if not quantity or event_type == "unknown":
                    continue
                coid = f"ml1-{handle.symbol}-n-{sha256(broker_id.encode()).hexdigest()[:16]}"
                child = replace(request, client_order_id=coid,
                    metadata={"episode_id": handle.episode_id, "momlive_strategy": STRATEGY_CODE,
                              "momlive_native_child": "true", "broker_order_id": broker_id})
                report = ExecutionReport(event_type, coid, broker_order_id=broker_id,
                    broker_fill_id=f"{broker_id}:{quantity}", symbol=handle.symbol, side="sell", intent_type="close",
                    filled_quantity=quantity, fill_price=price, origin="broker", reported_at=stamp,
                    metadata={"momlive_execution_time_source": "broker_execution_time"})
                await self._record_child(handle, runtime, report, child)
            if any(item[5] == "unknown" for item in children):
                # Validate the entire tree before recording siblings; an unreadable
                # status with intact identity cannot erase a proven sibling fill.
                snapshot = await runtime._inspect(handle.episode_id)
                return await self.unknown_read(handle, runtime, child_ids, snapshot)
            terminal = all(item[1] for item in children)
            working = not terminal
        snapshot = await runtime._inspect(handle.episode_id)
        if terminal:
            def close_root(session):
                root = session.scalar(select(BrokerOrder).where(BrokerOrder.client_order_id == handle.base_coid))
                if (root is None or (root.payload or {}).get("episode_id") != handle.episode_id or root.symbol != handle.symbol
                        or session.get(Strategy, root.strategy_id).code != STRATEGY_CODE
                        or session.get(BrokerAccount, root.broker_account_id).name != handle.account):
                    raise Refusal("native_terminal_root_owner_UNKNOWN")
                if handle.session != "NORMAL":
                    # Outside RTH the root is the actual single sell, not a
                    # synthetic pair reservation. Keep its broker terminal event.
                    if root.status not in {"filled", "cancelled", "rejected", "expired", "aborted"}:
                        raise Refusal("native_single_root_terminal_status_UNKNOWN")
                    return
                root.status = "cancelled"  # Synthetic pair reservation; executions belong to children.
            await runtime.service._run_db(close_root)
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id, handle.base_coid,
                              child_ids, working, terminal, snapshot["exited"])

    async def unknown_read(self, handle, runtime, child_ids, snapshot):
        values = runtime.runtime.setdefault(handle.episode_id, {})
        now = runtime.now_ns()
        last = values.get("protection_unknown_log_ns")
        if last is None or now - last >= 30_000_000_000:
            runtime.service.logger.warning("[MOMLIVE1-PROTECTION-UNKNOWN] episode=%s retry=true reservation=retained",
                                           handle.episode_id)
            values["protection_unknown_log_ns"] = now
        return ProtectionRead(handle.episode_id, handle.account, handle.entry_order_id,
                              handle.base_coid, child_ids, False, False, snapshot["exited"])

    async def _schwab_fresh_raw(self, leaf, account, identity, *, typed=False):
        if account is None:
            return None
        async def read():
            if typed and hasattr(leaf, "_authorized_request_json") and getattr(account, "account_hash", None):
                status, _, raw = await leaf._authorized_request_json("GET",
                    f"/trader/v1/accounts/{quote(account.account_hash, safe='')}/orders/{quote(str(identity), safe='')}")
                if status == 429 or 500 <= status <= 599:
                    raise MomentumReadUnavailable("http_unavailable")
                return raw if status == 200 and isinstance(raw, dict) else None
            # Older/custom adapters can prove a returned tree, but their bare
            # None cannot prove transport failure and never enables fallback.
            return await leaf._fetch_order(account, identity)
        try:
            return await asyncio.wait_for(read(), timeout=2)
        except (TimeoutError, ConnectionError, OSError):
            if typed:
                raise MomentumReadUnavailable("transport") from None
            return None
        except MomentumReadUnavailable:
            if typed:
                raise
            return None
        except Exception:
            return None

    async def _schwab_cancel_children(self, handle, runtime):
        row, snapshot = await self.root(handle, runtime)
        identity = row["broker_order_id"]
        if not identity:
            raise Refusal("native_schwab_root_receipt_UNKNOWN")
        leaf = self.leaf(handle.account, runtime)
        raw = await self._schwab_fresh_raw(leaf, leaf.accounts_by_name.get(handle.account), identity, typed=True)
        if raw is None:
            raise Refusal("native_cancel_fresh_read_UNKNOWN")
        children = self.schwab_children(raw, handle, leaf, root_id=identity,
            first_fill_ns=snapshot["first_fill_ns"] or 0, now_ns=runtime.now_ns(),
            allow_unknown_status=True)
        for broker_id, _, quantity, price, stamp, event_type in children:
            if not quantity:
                continue
            # Identity and executions were validated independently of status.
            # Preserve actual fills even when the venue's status is unknown;
            # this does not prove the remaining reservation terminal.
            if event_type == "unknown":
                event_type = "partially_filled"
            coid = f"ml1-{handle.symbol}-n-{sha256(broker_id.encode()).hexdigest()[:16]}"
            child = replace(row["request"], client_order_id=coid,
                metadata={"episode_id": handle.episode_id, "momlive_strategy": STRATEGY_CODE,
                          "momlive_native_child": "true", "broker_order_id": broker_id})
            report = ExecutionReport(event_type, coid, broker_order_id=broker_id,
                broker_fill_id=f"{broker_id}:{quantity}", symbol=handle.symbol, side="sell", intent_type="close",
                filled_quantity=quantity, fill_price=price, origin="broker", reported_at=stamp,
                metadata={"momlive_execution_time_source": "broker_execution_time"})
            await self._record_child(handle, runtime, report, child)
        return children

    async def _authorize_native_cancel(self, handle, runtime, request, values):
        control = getattr(runtime.service, "_bot_control_runtime", None)
        if control is None or not runtime._stop_requested():
            return
        if not handle.child_ids:
            raise Refusal("KILLSW1:native_children_unknown")
        # A pair remains ONE cleanup attempt even when strict budget exhaustion
        # splits its child dispatches across cycles. Revalidate its lease/binding
        # before every wire; never manufacture a second cleanup journal writer.
        lease = await control.cleanup_lease(request.strategy_code)
        binding = control.cleanup_binding(request, children=handle.child_ids)
        expected = {"command_id": str(lease.command_id), "generation": lease.generation,
                    "executor_id": lease.executor_id, "fence": lease.fence, "owner_hash": binding}
        key = f"native_cancel_cleanup:{handle.base_coid}"
        saved = values.get(key)
        if saved is not None:
            if saved != expected:
                raise Refusal("KILLSW1:native_cancel_cleanup_owner_CHANGED")
            return
        if not await control.begin_cleanup(request, "CANCEL_PROTECTION", children=handle.child_ids):
            raise Refusal("KILLSW1:native_cancel_cleanup_not_authorized")
        # Recheck after the prepare/dispatch awaits, before persisting any wire.
        current = await control.cleanup_lease(request.strategy_code)
        if (current.command_id != lease.command_id or current.generation != lease.generation
                or current.executor_id != lease.executor_id or current.fence != lease.fence):
            raise Refusal("KILLSW1:native_cancel_cleanup_owner_CHANGED")
        values[key] = expected

    @staticmethod
    def _known_cancel_binding(row, handle, identity):
        if row is None:
            return False
        request = row["request"]
        return bool(row["broker_order_id"] and request.client_order_id == identity
            and request.broker_account_name == handle.account and request.strategy_code == STRATEGY_CODE
            and request.symbol == handle.symbol and request.side == "sell" and request.intent_type == "close"
            and request.quantity == handle.quantity and request.metadata.get("episode_id") == handle.episode_id
            and str(request.metadata.get("broker_order_id", "")) == str(row["broker_order_id"])
            and (request.metadata.get("momlive_native_child") == "true"
                 or identity == handle.base_coid and request.metadata.get("momlive_one_shot") == "true"
                 and request.metadata.get("entry_broker_order_id") == handle.entry_order_id))

    async def cancel(self, handle, runtime):
        row, snapshot = await self.root(handle, runtime)
        adapter = runtime.service.broker_adapter
        provider = runtime.provider(handle.account)
        values = runtime.runtime.setdefault(handle.episode_id, {})
        mandatory = values.get("soft_exit_reason") in {
            "hard_stop_15pct", "time_300s", "KILLSW1_STOP"}
        wire_key = f"native_cancel_wires:{handle.base_coid}"
        sent = list(values.get(wire_key, []))
        if len(sent) > 2 or len(set(sent)) != len(sent):
            raise Refusal("native_cancel_wire_identity_UNKNOWN")
        if handle.session != "NORMAL":
            if (self._known_cancel_binding(row, handle, handle.base_coid)
                    and row["status"] in {"filled", "cancelled", "rejected", "expired", "aborted"}
                    and row["request"].metadata.get("momlive_terminal_broker_event") == row["status"]):
                # This is a real single order, not a synthetic pair root. Its
                # exact durable terminal proof needs neither a read nor a cancel;
                # the caller still owns reconciliation/release of the reservation.
                return
            identities = (handle.base_coid,)
        elif provider == "webull":
            identities = tuple(self.leaf(handle.account, runtime).exit_pair_leg_client_order_ids(handle.base_coid))
            if len(identities) != 2 or len(set(identities)) != 2:
                raise Refusal("native_webull_child_identity_UNKNOWN")
        else:
            if not handle.child_ids:
                raise Refusal("native_cancel_child_identity_UNKNOWN")
            identities = handle.child_ids
        if not set(sent).issubset(identities):
            raise Refusal("native_cancel_wire_identity_CHANGED")
        deferred = False
        for identity in identities:
            # Each individual child dispatch gets its own fresh proof. Never call
            # the generic pair canceller, which can cancel a filled sibling.
            if identity in sent:
                continue
            child = row["request"]
            native_child = handle.session == "NORMAL" and provider == "webull"
            if native_child:
                durable = next((item for item in snapshot["orders"]
                    if item["request"].client_order_id == identity), None)
                if (durable is not None and durable["broker_order_id"]
                        and durable["request"].broker_account_name == handle.account
                        and durable["request"].strategy_code == STRATEGY_CODE
                        and durable["request"].symbol == handle.symbol
                        and durable["request"].side == "sell"
                        and durable["request"].intent_type == "close"
                        and durable["request"].quantity == handle.quantity
                        and durable["request"].metadata.get("episode_id") == handle.episode_id
                        and durable["request"].metadata.get("momlive_native_child") == "true"
                        and durable["status"] in {"filled", "cancelled", "rejected", "expired", "aborted"}
                        and durable["request"].metadata.get("momlive_terminal_broker_event") == durable["status"]):
                    # No cancel is needed for this exact terminal child. Do not
                    # spend the working sibling's reserved permit or fake a wire.
                    continue
                child = replace(child, client_order_id=identity,
                    metadata={"episode_id": handle.episode_id, "momlive_strategy": STRATEGY_CODE,
                        "momlive_native_child": "true",
                        **({"broker_order_id": durable["broker_order_id"]} if durable and durable["broker_order_id"] else {})})
            if handle.session == "NORMAL" and provider == "schwab":
                try:
                    children = await self._schwab_cancel_children(handle, runtime)
                except MomentumReadUnavailable:
                    if not mandatory or not self._known_cancel_binding(row, handle, handle.base_coid):
                        raise Refusal("native_cancel_fresh_read_UNKNOWN") from None
                    children = None
                if children is not None:
                    candidates = [item for item in children if item[0] == identity]
                    if len(candidates) != 1:
                        raise Refusal("native_cancel_fresh_read_UNKNOWN")
                    if candidates[0][1] or candidates[0][2] == handle.quantity:
                        continue
                    if candidates[0][5] == "unknown" and not mandatory:
                        raise Refusal("native_cancel_fresh_read_UNKNOWN")
                # Mandatory exits may cancel a proven own ID despite unknown
                # status after the fresh tree read. Missing/mismatched trees
                # still fail above; cancellation NEVER releases the reservation.
            else:
                outcome = await runtime._fresh_cancel_outcome(child, handle=handle if native_child else None)
                if handle.session != "NORMAL" and row["status"] == "filled":
                    # Let the fresh read account/reconcile, but never cancel a
                    # stored filled single root through unavailable fallback or
                    # a regressive working read lacking canonical terminal proof.
                    if not runtime._cancel_read_terminal(outcome.report, child):
                        deferred = True
                    continue
                if outcome.kind != "report" and not (mandatory
                        and outcome.kind in {"unavailable", "status_unknown"}
                        and self._known_cancel_binding(durable if native_child else row, handle, identity)):
                    deferred = True
                    continue
                if runtime._cancel_read_terminal(outcome.report, child):
                    continue
            await self._authorize_native_cancel(handle, runtime, row["request"], values)
            sent.append(identity)
            values[wire_key] = list(sent)
            await runtime._save_handle(handle)
            try:
                callback = getattr(runtime, "_exit_gate_cancel_latched", None)
                if callback is not None:
                    callback(handle)  # Original save succeeded; NOT native ACK.
            except Exception:
                pass
            try:
                if handle.session == "NORMAL" and provider == "schwab":
                    await adapter.cancel_exit_leg_ids(handle.account, [identity])
                else:
                    await adapter.submit_order(replace(child, intent_type="cancel", quantity=Decimal(0)))
            except Exception as exc:
                runtime.service.logger.warning("[MOMORDER1-CANCEL-UNKNOWN] episode=%s coid=%s failure=%s",
                    handle.episode_id, identity, type(exc).__name__)
            finally:
                if handle.session == "NORMAL" and provider == "schwab":
                    await self._schwab_cancel_children(handle, runtime)
                else:
                    await runtime._fresh_cancel_read(child, handle=handle if native_child else None)
        if deferred:
            raise Refusal("native_cancel_fresh_read_UNKNOWN")
    def leaf(self, account, runtime):
        adapter = runtime.service.broker_adapter
        return adapter._adapter_for_account(account) if hasattr(adapter, "_adapter_for_account") else adapter

    @staticmethod
    def _validate_fresh_report(report, request):
        quantity = report.filled_quantity
        if (report.origin != "broker" or report.client_order_id != request.client_order_id
                or report.symbol != request.symbol or report.side != request.side
                or report.intent_type != request.intent_type or not report.broker_order_id
                or (request.metadata.get("broker_order_id")
                    and str(report.broker_order_id) != str(request.metadata["broker_order_id"]))
                or not quantity.is_finite() or not 0 <= quantity <= request.quantity
                or report.event_type not in {"accepted", "partially_filled", "filled", "cancelled", "rejected", "expired", "aborted"}
                or report.event_type == "filled" and quantity != request.quantity):
            raise Refusal("fresh_order_identity_or_quantity_UNKNOWN")
        return report

    async def fresh_cancel_read(self, request, runtime):
        """Per-call evidence for cancellation, never an absence/release proof."""
        request = replace(request, metadata={**request.metadata, "momlive_fresh_order_read": "true"})
        if request.intent_type == "cancel":
            return MomentumOrderRead("invalid", reason="original_request_required")
        leaf = self.leaf(request.broker_account_name, runtime)
        try:
            if runtime.provider(request.broker_account_name) == "schwab":
                identity = request.metadata.get("broker_order_id")
                if not identity or request.metadata.get("momcancel1_ack_identity") == "UNKNOWN":
                    return MomentumOrderRead("invalid", reason="native_identity_missing")
                raw = await self._schwab_fresh_raw(leaf, leaf.accounts_by_name.get(request.broker_account_name),
                    identity, typed=True)
                if not isinstance(raw, dict):
                    return MomentumOrderRead("invalid", reason="body_shape")
                status = raw.get("status")
                if not isinstance(status, str) or not status.strip():
                    return MomentumOrderRead("invalid", reason="status_missing")
                known = leaf.ACCEPTED_STATUSES | leaf.PARTIAL_FILL_STATUSES | leaf.CANCELLED_STATUSES | leaf.REJECTED_STATUSES | {"FILLED", "PARTIALLY_FILLED"}
                kind = "report" if status.upper() in known else "status_unknown"
                if kind == "status_unknown":
                    declared = Decimal(str(raw.get("filledQuantity", 0)))
                    if not declared.is_finite() or declared < 0:
                        return MomentumOrderRead("invalid", reason="execution_quantity")
                    raw = {**raw, "status": "PARTIALLY_FILLED" if declared else "WORKING"}
                report = await self._fetch_entry_update(request, runtime, supplied_raw=raw)
            elif (callable(getattr(leaf, "fetch_momentum_cancel_read", None))
                    and request.broker_account_name in getattr(leaf, "accounts_by_name", {})
                    and getattr(leaf, "_query_budget", None) is not None):
                outcome = await asyncio.wait_for(leaf.fetch_momentum_cancel_read(request), timeout=2)
                if not isinstance(outcome, MomentumOrderRead):
                    return MomentumOrderRead("invalid", reason="outcome_shape")
                if outcome.kind in {"invalid", "unavailable"}:
                    if outcome.report is not None:
                        return MomentumOrderRead("invalid", reason="conflicting_outcome")
                    return outcome
                if outcome.report is None:
                    return MomentumOrderRead("invalid", reason="report_missing")
                kind = outcome.kind
                report = await self._fetch_entry_update(request, runtime, supplied_report=outcome.report)
            else:
                # A legacy adapter's None has no provenance: never infer outage.
                report = await asyncio.wait_for(self._fetch_entry_update(request, runtime), timeout=2)
                kind = "report"
            if report is None:
                return MomentumOrderRead("invalid", reason="report_unusable")
            return MomentumOrderRead(kind, self._validate_fresh_report(report, request))
        except MomentumReadUnavailable as exc:
            return MomentumOrderRead("unavailable", reason=str(exc))
        except QueryBudgetUnavailable:
            return MomentumOrderRead("unavailable", reason="budget")
        except (TimeoutError, ConnectionError, OSError):
            return MomentumOrderRead("unavailable", reason="transport")
        except Exception:
            return MomentumOrderRead("invalid", reason="identity_or_execution")

    async def fetch_entry_update(self, request, runtime, *, fresh=False):
        if not fresh:
            return await self._fetch_entry_update(request, runtime)
        request = replace(request, metadata={**request.metadata, "momlive_fresh_order_read": "true"})
        try:
            if request.intent_type == "cancel":
                raise Refusal("fresh_read_requires_original_order")
            report = await asyncio.wait_for(self._fetch_entry_update(request, runtime), timeout=2)
            if report is None:
                raise Refusal("fresh_order_unavailable")
            return self._validate_fresh_report(report, request)
        except Exception as exc:
            runtime.service.logger.warning("[MOMORDER1-FRESH-UNKNOWN] account=%s coid=%s failure=%s",
                request.broker_account_name, request.client_order_id, type(exc).__name__)
            return None

    async def _fetch_entry_update(self, request, runtime, *, supplied_report=None, supplied_raw=None):
        leaf = self.leaf(request.broker_account_name, runtime)
        if runtime.provider(request.broker_account_name) == "schwab":
            identity = request.metadata.get("broker_order_id")
            if not identity or request.metadata.get("momcancel1_ack_identity") == "UNKNOWN":
                return None
            account = leaf.accounts_by_name.get(request.broker_account_name)
            if account is None:
                raise Refusal("owned_entry_account_UNKNOWN")
            raw = supplied_raw if supplied_raw is not None else await leaf._fetch_order(account, identity)
            if raw is None:
                return None
            legs = raw.get("orderLegCollection") or []
            if (str(raw.get("orderId", "")) != str(identity) or len(legs) != 1
                    or str((legs[0].get("instrument") or {}).get("symbol", "")).upper() != request.symbol
                    or str(legs[0].get("instruction", "")).upper() != request.side.upper()):
                raise Refusal("owned_entry_raw_identity_UNKNOWN")
            raw_status = str(raw.get("status", "")).upper()
            if raw_status not in leaf.ACCEPTED_STATUSES | leaf.PARTIAL_FILL_STATUSES | leaf.CANCELLED_STATUSES | leaf.REJECTED_STATUSES | {"FILLED", "PARTIALLY_FILLED"}:
                raise Refusal("owned_entry_raw_status_UNKNOWN")
            report = leaf._execution_report_from_order(request=request, order=raw,
                event_type=leaf._map_order_status(raw), broker_order_id=identity)
            times, quantity, cash = [], Decimal(0), Decimal(0)
            activities = raw.get("orderActivityCollection") or []
            if len(activities) > 256:
                raise Refusal("owned_entry_execution_bound_UNKNOWN")
            declared = Decimal(str(raw.get("filledQuantity", 0)))
            for activity in activities:
                executions = activity.get("executionLegs") or []
                if len(executions) > 256:
                    raise Refusal("owned_entry_execution_bound_UNKNOWN")
                for leg in executions:
                    q, price = Decimal(str(leg.get("quantity", 0))), Decimal(str(leg.get("price", 0)))
                    if not q.is_finite() or q < 0:
                        raise Refusal("owned_entry_execution_evidence_UNKNOWN")
                    if q == 0 or (price == 0 and declared == 0 and raw_status in leaf.CANCELLED_STATUSES):
                        continue
                    stamp = leaf._parse_datetime(leg.get("time"))
                    if (not q.is_finite() or q <= 0 or not price.is_finite() or price <= 0
                            or legs[0].get("legId") is None
                            or str(leg.get("legId", "")) != str(legs[0].get("legId", ""))
                            or stamp is None or stamp.tzinfo is None
                            or not 0 < int(stamp.timestamp()*1e9) <= runtime.now_ns()):
                        raise Refusal("owned_entry_execution_evidence_UNKNOWN")
                    times.append(stamp)
                    quantity += q
                    cash += q * price
            if (not declared.is_finite() or quantity != declared or not 0 <= quantity <= request.quantity
                    or raw_status == "FILLED" and quantity == 0):
                raise Refusal("owned_entry_execution_coverage_UNKNOWN")
            report = replace(report, filled_quantity=quantity, fill_price=cash/quantity if quantity else None,
                event_type="partially_filled" if raw_status == "PARTIALLY_FILLED" else report.event_type)
            stamp = (min(times) if request.side == "buy" else max(times)) if times else None
        else:
            report = supplied_report if supplied_report is not None else await runtime.service.broker_adapter.fetch_order_update(request)
            if report is None:
                return None
            stamp = leaf._parse_broker_time(report.metadata.get("webull_broker_filled_time"))
            if report.filled_quantity > 0 and request.side == "buy":
                if report.filled_quantity > request.quantity:
                    raise Refusal("owned_broker_execution_quantity_UNKNOWN")
                # Existing adapter exposes LAST execution only. Neither an earlier
                # missed partial nor camel-case metadata absence proves FIRST.
                metadata = {**report.metadata, "momlive_first_execution_time_status": "UNKNOWN"}
                metadata.pop("momlive_execution_time_source", None)
                if stamp is not None and stamp.tzinfo is not None and 0 < int(stamp.timestamp()*1e9) <= runtime.now_ns():
                    metadata["momlive_last_execution_ns"] = str(int(stamp.timestamp()*1e9))
                return replace(report, metadata=metadata)
        if report.filled_quantity > 0:
            if (stamp is None or stamp.tzinfo is None or not 0 < int(stamp.timestamp()*1e9) <= runtime.now_ns()
                    or report.filled_quantity > request.quantity):
                raise Refusal("owned_broker_execution_time_UNKNOWN")
            return replace(report, reported_at=stamp,
                metadata={**report.metadata, "momlive_execution_time_source": "broker_execution_time"})
        return report


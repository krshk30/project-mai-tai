from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, time as daytime
from decimal import Decimal, ROUND_FLOOR
from hashlib import sha256
import json
import re
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select

from project_mai_tai.broker_adapters.protocols import ExecutionReport, OrderRequest
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
        entry_ports = ExecutionPorts(self.clock, self.quote, self.route_for, self._foreign_owner,
            self._claim, self._prepare, self._save, self._submit, self._record, self._route_report,
            self._request_quote_owner, self._wait_quote,
            entry_metadata=self._entry_control_metadata if getattr(self.settings, "bot_control_enabled", False) else None)
        executor = LiveEntryExecutor(self.book, entry_ports, schwab_account=self.accounts[0], webull_account=self.accounts[1])
        ports = ActorPorts(self._entry_enabled, self.now_ns, self._observe,
            self._save, self._cancel_entry, self._protect, self._exit, self._exit_quote,
            stop_requested=self._stop_requested, abandon_unsent=self._abandon_unsent_for_stop,
            diagnostic_account=lambda: self.accounts[0])
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

    async def _prepare(self, identity, request, provider):
        await self.service._run_db(lambda session: self.db.prepare_order(session, identity, request, provider=provider))

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

    async def _observe(self, identity):
        snapshot = await self._inspect(identity)
        entry = None
        for row in snapshot["orders"]:
            request = row["request"]
            if request.side == "buy" and request.intent_type == "open":
                entry = request
                if (row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}
                        or snapshot["quantity"] > 0 and snapshot["first_fill_ns"] is None
                        and self.runtime.get(identity, {}).get("first_execution_time_status") != "UNKNOWN"):
                    sent = self.book.episodes[identity].first_submit_ns
                    if (sent is not None and self.now_ns() - sent >= 2_000_000_000
                            and row["status"] not in {"filled", "cancelled", "rejected", "expired", "aborted"}):
                        request = replace(request, metadata={**request.metadata, "momlive_entry_deadline_read": "true"})
                    report = await self.backend.fetch_entry_update(request, self)
                    if report is not None:
                        await self._record(request, [report])
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
                pending_exit_reason=self.runtime.get(identity, {}).get("soft_exit_reason"))
            self.runtime.setdefault(identity, {}).update(time_exit_clock_source=position.exit_clock_source,
                                                        time_exit_clock_ns=position.exit_clock_ns)
            handle = self.native.handles.get(identity)
            if handle:
                await self.native._observe(position, handle)
                snapshot = await self._inspect(identity)
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
                if self.provider(request.broker_account_name) == "schwab" and (
                        not request.metadata.get("broker_order_id")
                        or request.metadata.get("momcancel1_ack_identity") == "UNKNOWN"):
                    await self._refuse_entry_identity(identity)
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
                await self.service.broker_adapter.submit_order(cancel)

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

    async def _protect(self, position):
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
        local = self.clock().astimezone(ET)
        session = ("AM" if (local.hour, local.minute) < (9, 30)
                   else "NORMAL" if (local.hour, local.minute) < (16, 0) else "PM")
        await self.native.ensure(position, broker=self.provider(position.account), session=session)

    async def _read_protection(self, handle):
        read = await self.backend.read(handle, self)
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
        control = getattr(self.service, "_bot_control_runtime", None)
        if control is not None and self._stop_requested():
            if not handle.child_ids:
                raise Refusal("KILLSW1:native_children_unknown")
            row, _ = await self.backend.root(handle, self)
            if not await control.begin_cleanup(
                row["request"], "CANCEL_PROTECTION", children=handle.child_ids,
            ):
                return
        await self.backend.cancel(handle, self)

    def _soft_exit_chase_window(self):
        local = self.clock().astimezone(ET).time()
        return daytime(4) <= local < daytime(9, 30) or daytime(16) <= local < daytime(20)

    async def _chase_cancel(self, identity, request, values):
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
        values = self.runtime.setdefault(position.episode_id, {})
        if position.remaining <= 0:
            return
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
                    return
                await self._record(row["request"], [report])
                snapshot = await self._inspect(position.episode_id)
                row = next(item for item in snapshot["orders"]
                           if item["request"].client_order_id == row["request"].client_order_id)
            if (row["status"] not in terminal
                    or row["request"].metadata.get("momlive_terminal_broker_event") != row["status"]):
                if (not self._soft_exit_chase_window() or report.origin != "broker"
                        or report.event_type not in {"accepted", "partially_filled"}):
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
                if not await self._chase_cancel(position.episode_id, row["request"], values):
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
        fresh = self._exit_quote(position.symbol)
        stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
        if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000
                or last is not None and stamp <= int(values.get("soft_exit_quote_ns", last))):
            return
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
        fresh = self._exit_quote(position.symbol)
        stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
        if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000):
            return  # Durable watched exit stays armed after confirmed release.
        holdings = await self.service.broker_adapter.list_account_positions(position.account)
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
        reserved_foreign = (Decimal(0) if reason == "KILLSW1_STOP"
                            else await self.service._run_db(foreign, commit=False))
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
        fresh = self._exit_quote(position.symbol)
        stamp, bid = fresh.get("observed_ns"), fresh.get("bid")
        if not control_market and (stamp is None or bid is None or bid <= 0 or not 0 <= self.now_ns()-stamp <= 2_000_000_000):
            return
        local = self.clock().astimezone(ET)
        session = "NORMAL" if (9, 30) <= (local.hour, local.minute) < (16, 0) else "AM" if local.hour < 9 else "PM"
        if control_market and session != "NORMAL":
            return  # Session changed while releasing protection; next pass reprices for EH.
        control_market = reason == "KILLSW1_STOP" and session == "NORMAL"
        quantity = position.claim_exit_quantity(broker_available=total, foreign_reserved=reserved_foreign)
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
            # Still in this pre-wire frame: known UNSENT, not a broker rejection
            # or ambiguous ACK. Mark only this own order; keep the exit armed.
            await self.service._run_db(lambda unit: self.db.abandon_unsent_exit(unit, request))
            values["soft_exit_sent"] = False
            position.reserved = Decimal(0)
            await self._save(next(row for row in self.book.snapshot() if row["episode_id"] == position.episode_id))
            return
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


class OwnedNativeBackend:
    """Existing broker capabilities, scoped to the durable own protection root."""

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

    async def read(self, handle, runtime):
        row, snapshot = await self.root(handle, runtime)
        adapter = runtime.service.broker_adapter
        request = row["request"]
        provider = runtime.provider(handle.account)
        child_ids = handle.child_ids
        working = terminal = False
        if handle.session != "NORMAL":
            report = await self.fetch_entry_update(request, runtime)
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
                if (durable is not None and durable["broker_order_id"]
                        and durable["request"].broker_account_name == handle.account
                        and durable["request"].metadata.get("momlive_native_child") == "true"
                        and durable["status"] in {"filled", "cancelled", "rejected", "expired", "aborted"}
                        and durable["request"].metadata.get("momlive_terminal_broker_event") == durable["status"]):
                    reports.append(durable["status"])
                    continue
                child = replace(request, client_order_id=coid,
                    metadata={"episode_id": handle.episode_id, "momlive_strategy": STRATEGY_CODE,
                              "momlive_native_child": "true"})
                report = await self.fetch_entry_update(child, runtime)
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
            raw = await leaf._fetch_order(account, root_id) if account else None
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

    async def cancel(self, handle, runtime):
        row, _ = await self.root(handle, runtime)
        adapter = runtime.service.broker_adapter
        if handle.session != "NORMAL":
            await adapter.submit_order(replace(row["request"], intent_type="cancel", quantity=Decimal(0)))
        elif runtime.provider(handle.account) == "webull":
            await adapter.cancel_exit_pair(broker_account_name=handle.account, symbol=handle.symbol,
                                            base_client_order_id=handle.base_coid)
        else:
            if not handle.child_ids:
                raise Refusal("native_cancel_child_identity_UNKNOWN")
            await adapter.cancel_exit_leg_ids(handle.account, list(handle.child_ids))
    def leaf(self, account, runtime):
        adapter = runtime.service.broker_adapter
        return adapter._adapter_for_account(account) if hasattr(adapter, "_adapter_for_account") else adapter

    async def fetch_entry_update(self, request, runtime):
        leaf = self.leaf(request.broker_account_name, runtime)
        if runtime.provider(request.broker_account_name) == "schwab":
            identity = request.metadata.get("broker_order_id")
            if not identity or request.metadata.get("momcancel1_ack_identity") == "UNKNOWN":
                return None
            account = leaf.accounts_by_name.get(request.broker_account_name)
            if account is None:
                raise Refusal("owned_entry_account_UNKNOWN")
            raw = await leaf._fetch_order(account, identity)
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
            report = await runtime.service.broker_adapter.fetch_order_update(request)
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


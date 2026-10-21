from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, ROUND_CEILING
import logging
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo

from project_mai_tai.broker_adapters.protocols import OrderRequest
from project_mai_tai.momentum_live.admission import EpisodeLedger, Refusal, Route, STRATEGY_CODE, Signal, price_entry
from project_mai_tai.momentum_live.entry_reprice import (
    MAX_PRICE_UPDATES, UnsentEntryConflict, price_changed, validate_entry_reprice,
)
from project_mai_tai.momentum_live.entry_wire import entry_wire_check

logger = logging.getLogger("momentum-live")


@dataclass
class ExecutionPorts:
    clock: Callable[[], datetime]
    quote: Callable[[str], dict | None]
    route_for: Callable[[str, datetime], object]
    foreign_owner: Callable[[str, str], Awaitable[bool]]
    durable_claim: Callable[[dict, str, str, Signal], Awaitable[bool]]
    durable_order: Callable[[str, OrderRequest, str], Awaitable[None]]
    save_episode: Callable[[dict], Awaitable[None]]
    submit: Callable[[OrderRequest], Awaitable[list]]
    record: Callable[[OrderRequest, list], Awaitable[dict]]
    record_route: Callable[[str, str, str], Awaitable[None]]
    request_quote_owner: Callable[[], Awaitable[None]] | None = None
    wait_quote: Callable[[str], Awaitable[None]] | None = None
    entry_metadata: Callable[[Signal], dict[str, str]] | None = None
    refuse_unsent_entry: Callable[[OrderRequest, str, dict], Awaitable[None]] | None = None
    reprice_unsent_entry: Callable[[OrderRequest, OrderRequest, dict | None], Awaitable[None]] | None = None


@dataclass
class _PreparedEntry:
    request: OrderRequest
    price_updates: int = 0


class LiveEntryExecutor:
    """Runs on the bounded entry worker, never in paper/quote callbacks."""

    def __init__(self, book: EpisodeLedger, ports: ExecutionPorts, *, schwab_account: str, webull_account: str):
        self.book = book
        self.ports = ports
        self.accounts = {Route.SCHWAB: schwab_account, Route.WEBULL: webull_account}
        self.schwab_refusals = 0

    def _snapshot(self, episode_id: str) -> dict:
        return next(row for row in self.book.snapshot() if row["episode_id"] == episode_id)

    async def _refresh_prepared(self, signal, route, prepared, context, *, episode=None):
        while True:
            checked = self._request(signal, prepared.request.metadata["episode_id"], route,
                                    refusal_context=context)
            validate_entry_reprice(prepared.request, checked, route)
            changed = price_changed(prepared.request, checked)
            if not changed and episode is None:
                return
            if changed and prepared.price_updates >= MAX_PRICE_UPDATES:
                raise Refusal("mombuffer1_price_update_budget_exhausted_no_dispatch")
            if self.ports.reprice_unsent_entry is None:
                if changed:
                    raise Refusal("mombuffer1_unsent_reprice_unavailable")
                await self.ports.save_episode(episode)
            else:
                await self.ports.reprice_unsent_entry(prepared.request, checked, episode)
                # Settlement must address this committed version even if the
                # fresh check after the await refuses or the budget expires.
                prepared.request = checked
            prepared.price_updates += int(changed)
            if changed:
                logger.info(
                    "[MOMBUFFER1-REPRICE] symbol=%s episode=%s update=%s ask=%s ask_age_ms=%s "
                    "limit=%s cap=%s quantity=%s order_identity=unchanged",
                    signal.symbol, checked.metadata["episode_id"], prepared.price_updates,
                    context["ask"], context["ask_age_ms"], checked.metadata["limit_price"],
                    checked.metadata["cap_price"], checked.quantity,
                )
            episode = None
            # No await between the last fresh check and returning to submit.

    def _request(self, signal: Signal, episode_id: str, route: Route, *, refusal_context=None) -> OrderRequest:
        if refusal_context is not None:
            refusal_context.update(ask="UNKNOWN", ask_age_ms="UNKNOWN", limit="UNKNOWN")
        now = self.ports.clock()
        now_ns = int(now.timestamp()*1e9)
        if not 0 <= now_ns-signal.observed_ns <= 2_000_000_000:
            raise Refusal("signal_expired_before_dispatch")
        local = now.astimezone(ZoneInfo("America/New_York"))
        start_hour = 7 if route == Route.SCHWAB else 4
        if local.weekday() >= 5 or not start_hour <= local.hour < 16:
            raise Refusal("entry_window_closed")
        quote = self.ports.quote(signal.symbol) or {}
        stamp = quote.get("received_at")
        quote_ns = int(stamp.timestamp()*1e9) if isinstance(stamp, datetime) and stamp.tzinfo is not None else None
        ask = Decimal(str(quote["ask"])) if quote.get("ask") is not None else None
        tick = Decimal(".0001") if ask is not None and ask <= 1 else Decimal(".01")
        if refusal_context is not None:
            # Diagnostic snapshot of this read, not a later/repeated quote lookup.
            try:
                refusal_context.update(
                    ask=ask if ask is not None else "UNKNOWN",
                    ask_age_ms=(now_ns-quote_ns)/1_000_000 if quote_ns is not None else "UNKNOWN",
                    limit=(ask/tick).to_integral_value(rounding=ROUND_CEILING)*tick
                    if ask is not None and ask.is_finite() else "UNKNOWN",
                )
            except Exception:
                pass  # Diagnostics must not change pricing/refusal behavior.
        priced = price_entry(signal.signal_price, ask, quote_ns=quote_ns, now_ns=now_ns, route=route, tick=tick)
        session = "NORMAL" if (local.hour, local.minute) >= (9, 30) and local.hour < 16 else "AM"
        account = self.accounts[route]
        if not account or not account.startswith("live:"):
            raise Refusal("live_account_binding_missing")
        request = OrderRequest(f"ml1-{signal.symbol}-e-{episode_id.replace('-', '')[:20]}",
            account, STRATEGY_CODE, signal.symbol, "buy", "open", Decimal(priced.quantity), "MOMLIVE1_DETECTED",
            {"episode_id": episode_id, "momlive_one_shot": "true", "momlive_strategy": STRATEGY_CODE,
             "order_type": "limit", "time_in_force": "day",
             "limit_price": str(priced.limit), "reference_price": str(priced.limit), "cap_price": str(priced.cap),
             "signal_price": str(signal.signal_price), "session": session,
             "extended_hours": "false" if session == "NORMAL" else "true"}, "limit", "day")
        if self.ports.entry_metadata is not None:
            request.metadata.update(self.ports.entry_metadata(signal))
        return request

    async def admit(self, signal: Signal) -> tuple[str, OrderRequest | None, dict | None]:
        signal.validate()
        now = self.ports.clock()
        if signal.session_date != str(now.astimezone(ZoneInfo("America/New_York")).date()):
            raise Refusal("signal_session_date_mismatch")
        decision = self.ports.route_for(signal.symbol, now) if signal.country_route is None else None
        broker = signal.country_route if signal.country_route is not None else decision.broker
        category = signal.country_category if signal.country_route is not None else decision.reason
        if broker is None:
            raise Refusal("entry_window_closed")
        if not isinstance(category, str) or not category:
            raise Refusal("sealed_country_decision_invalid")
        route = Route(broker)
        if "unknown" in category.lower():
            logger.info("[MOMLIVE1-ROUTE] symbol=%s broker=%s classification=UNKNOWN at_decision=true",
                        signal.symbol, route.value)
        account = self.accounts[route]
        if await self.ports.foreign_owner(account, signal.symbol) is not False:
            raise Refusal("foreign_account_symbol_owner")
        episode, fresh = self.book.claim(signal, now_ns=int(now.timestamp()*1e9))
        if not fresh:
            await self.ports.save_episode(self._snapshot(episode.episode_id))
            return episode.episode_id, None, None
        if not await self.ports.durable_claim(self._snapshot(episode.episode_id), account, route.value, signal):
            raise Refusal("episode_already_durable_no_resubmit")
        if self.ports.request_quote_owner is not None:
            remaining = (signal.observed_ns+2_000_000_000-int(self.ports.clock().timestamp()*1e9))/1e9
            refusal_context = {"ask": "UNKNOWN", "ask_age_ms": "UNKNOWN", "limit": "UNKNOWN", "cap": "UNKNOWN"}
            try:
                refusal_context["cap"] = signal.signal_price*Decimal("1.015")
            except Exception:
                pass  # A diagnostic projection cannot replace the original refusal.
            try:
                async with asyncio.timeout(max(0, remaining)):
                    await self.ports.request_quote_owner()
                    try:
                        self._request(signal, episode.episode_id, route, refusal_context=refusal_context)
                    except Refusal as exc:
                        if str(exc) != "quote_missing_future_or_stale" or self.ports.wait_quote is None:
                            raise
                        # One quote notification, not polling or order retries.
                        await self.ports.wait_quote(signal.symbol)
                        self._request(signal, episode.episode_id, route, refusal_context=refusal_context)
            except (TimeoutError, Refusal) as exc:
                # No order/send exists yet: this is proven unsent, not an unknown
                # broker ACK. Persist expiry and keep source/move idempotence.
                self.book.reconcile(episode.episode_id, cumulative_filled=Decimal(0), cumulative_exited=Decimal(0),
                    now_ns=int(self.ports.clock().timestamp()*1e9), terminal_entry=True)
                await self.ports.save_episode(self._snapshot(episode.episode_id))
                await self.ports.request_quote_owner()
                logger.info(
                    "[MOMLIVE1-REFUSED] symbol=%s reason=%s ask=%s ask_age_ms=%s "
                    "signal=%s cap=%s limit=%s route=%s no_wire=true",
                    signal.symbol, str(exc) or "timeout", refusal_context["ask"],
                    refusal_context["ask_age_ms"], signal.signal_price,
                    refusal_context["cap"], refusal_context["limit"], route.value,
                )
                raise Refusal("quote_wait_expired_or_refused_no_wire") from None
        request = self._request(signal, episode.episode_id, route)
        await self.ports.durable_order(episode.episode_id, request, route.value)
        prepared = _PreparedEntry(request)
        # DB/ownership work may have waited; recheck cached quote and wire invariants.
        refusal_context = {"ask": "UNKNOWN", "ask_age_ms": "UNKNOWN", "limit": "UNKNOWN",
                           "cap": request.metadata["cap_price"]}
        try:
            await self._refresh_prepared(signal, route, prepared, refusal_context)
            self.book.begin_submit(episode.episode_id, route, now_ns=int(self.ports.clock().timestamp()*1e9))
            await self._refresh_prepared(signal, route, prepared, refusal_context,
                                         episode=self._snapshot(episode.episode_id))
        except UnsentEntryConflict as exc:
            # A changed durable preimage is not proof that this call may retire
            # the owner. In particular, never overwrite a permit/terminal race.
            logger.warning(
                "[MOMLIVE1-REFUSED] symbol=%s episode=%s reason=%s ask=%s ask_age_ms=%s "
                "signal=%s cap=%s limit=%s route=%s no_wire=UNKNOWN ownership=retained",
                signal.symbol, episode.episode_id, exc, refusal_context["ask"],
                refusal_context["ask_age_ms"], signal.signal_price, refusal_context["cap"],
                refusal_context["limit"], route.value,
            )
            raise
        except Refusal as exc:
            # This lexical boundary ends BEFORE submit. A timeout/unknown ACK or
            # any report after submit must never reach client-only settlement.
            if self.ports.refuse_unsent_entry is None:
                raise Refusal("prewire_refusal_settlement_unavailable") from exc
            await self.ports.refuse_unsent_entry(prepared.request, str(exc), refusal_context)
            self.book.reconcile(episode.episode_id, cumulative_filled=Decimal(0), cumulative_exited=Decimal(0),
                now_ns=int(self.ports.clock().timestamp()*1e9), terminal_entry=True)
            await self.ports.save_episode(self._snapshot(episode.episode_id))
            logger.info(
                "[MOMLIVE1-REFUSED] symbol=%s episode=%s reason=%s ask=%s ask_age_ms=%s "
                "signal=%s cap=%s limit=%s route=%s no_wire=true settlement=refused_before_wire",
                signal.symbol, episode.episode_id, str(exc), refusal_context["ask"],
                refusal_context["ask_age_ms"], signal.signal_price, refusal_context["cap"],
                refusal_context["limit"], route.value,
            )
            if self.ports.request_quote_owner is not None:
                await self.ports.request_quote_owner()
            raise
        request = prepared.request

        def validate_wire():
            context = {"ask": "UNKNOWN", "ask_age_ms": "UNKNOWN", "limit": "UNKNOWN",
                       "cap": request.metadata["cap_price"]}
            try:
                checked = self._request(signal, episode.episode_id, route, refusal_context=context)
                sealed = replace(request, metadata={key: value for key, value in request.metadata.items()
                                                    if key != "bot_control_permit"})
                validate_entry_reprice(sealed, checked, route)
                if price_changed(sealed, checked):
                    raise Refusal("mombuffer1_quote_changed_after_permit_no_dispatch")
            except Refusal as exc:
                logger.warning(
                    "[MOMLIVE1-REFUSED] symbol=%s episode=%s reason=%s ask=%s ask_age_ms=%s "
                    "signal=%s cap=%s limit=%s route=%s settlement=not_authorized_after_submit",
                    signal.symbol, episode.episode_id, exc, context["ask"], context["ask_age_ms"],
                    signal.signal_price, context["cap"], context["limit"], route.value,
                )
                raise RuntimeError(str(exc)) from exc
            return context

        with entry_wire_check(request, validate_wire):
            reports = await self.ports.submit(request)
        result = await self.ports.record(request, reports)
        if route == Route.SCHWAB and any(r.event_type == "rejected" and r.origin == "broker" for r in reports):
            self.schwab_refusals += 1
            logger.warning("[MOMLIVE1-SCHWAB-REFUSAL] symbol=%s episode=%s count=%s resend=never",
                           signal.symbol, episode.episode_id, self.schwab_refusals)
        await self.ports.record_route(episode.episode_id, signal.session_date, category)
        return episode.episode_id, request, result

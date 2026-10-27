"""Exact-request, owning-task transport capability; never a global POST hook."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import logging


_ENTRY_WIRE = ContextVar("mombuffer1_entry_wire", default=None)


class LocalEntryNoDispatch(RuntimeError):
    """Only a bound native guard BEFORE its first transport can create proof."""


class _NoDispatchProof:
    def __init__(self, attempt, reason, context):
        self.attempt, self.reason, self.context = attempt, reason, dict(context)
        self.permit = attempt.request.metadata.get("bot_control_permit")
        self.used = False

    def consume(self, request):
        if (self.used or self.attempt.dispatched or self.attempt.proof is not self
                or asyncio.current_task() is not self.attempt.task
                or request is not self.attempt.request
                or request.metadata.get("bot_control_permit") != self.permit
                or replace(request, metadata={k: v for k, v in request.metadata.items()
                                             if k != "bot_control_permit"}) != self.attempt.sealed):
            raise RuntimeError("mombuffer1_no_dispatch_proof_invalid")
        self.used = True
        return self.permit


class _WireAttempt:
    def __init__(self, request, validate, episode=None):
        self.request, self.validate = request, validate
        self.sealed = replace(request, metadata=dict(request.metadata))
        self.episode = deepcopy(episode)
        self.task = asyncio.current_task()
        self.dispatched = False
        self.proof = None

    def check(self, *, quantity, limit, symbol):
        context = {}
        try:
            context = self.validate() or {}
            try:
                matches = (Decimal(str(quantity)) == self.request.quantity
                           and Decimal(str(limit)) == Decimal(self.request.metadata["limit_price"])
                           and symbol == self.request.symbol)
            except (ArithmeticError, TypeError, ValueError):
                matches = False
            if not matches:
                raise RuntimeError("mombuffer1_serialized_wire_changed_no_dispatch")
        except RuntimeError as exc:
            context = getattr(exc, "refusal_context", context)
            logging.getLogger("momentum-live").warning(
                "[MOMLIVE1-REFUSED] symbol=%s reason=%s ask=%s ask_age_ms=%s limit=%s cap=%s "
                "wire_limit=%s wire_quantity=%s prior_transport=%s",
                self.request.symbol, exc, context.get("ask", "UNKNOWN"), context.get("ask_age_ms", "UNKNOWN"),
                self.request.metadata.get("limit_price"), self.request.metadata.get("cap_price"),
                limit, quantity, self.dispatched,
            )
            if self.dispatched:
                raise
            self.proof = _NoDispatchProof(self, str(exc), context)
            raise LocalEntryNoDispatch(str(exc)) from exc

    def before_transport(self):
        if self.proof is not None:
            raise RuntimeError("mombuffer1_refused_attempt_cannot_dispatch")
        self.dispatched = True  # BEFORE order I/O, not after its response.


def entry_wire_guard(request):
    bound = _ENTRY_WIRE.get()
    try:
        task = asyncio.current_task()
    except RuntimeError:
        return None
    if bound is None:
        return None
    if bound.request is not request:
        if (request.client_order_id == bound.request.client_order_id
                and request.broker_account_name == bound.request.broker_account_name
                and request.strategy_code == bound.request.strategy_code):
            raise RuntimeError("mombuffer1_bound_entry_object_changed")
        return None
    if bound.task is not task:
        raise RuntimeError("mombuffer1_bound_entry_task_changed")
    if (request.strategy_code != "momentum_warn_live" or request.side != "buy"
            or request.intent_type != "open" or request.metadata.get("momlive_one_shot") != "true"):
        return None
    return bound


def entry_wire_active():
    bound = _ENTRY_WIRE.get()
    return bound is not None and entry_wire_guard(bound.request) is bound


@contextmanager
def entry_wire_check(request, validate, *, episode=None):
    attempt = _WireAttempt(request, validate, episode)
    token = _ENTRY_WIRE.set(attempt)
    try:
        yield attempt
    finally:
        _ENTRY_WIRE.reset(token)


def validate_entry_wire(*, quantity, limit, symbol=None):
    """Owning-task seam. Native threads receive explicit capability instead."""
    bound = _ENTRY_WIRE.get()
    if bound is not None and entry_wire_guard(bound.request) is bound:
        bound.check(quantity=quantity, limit=limit, symbol=symbol)

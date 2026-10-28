"""L12 bounded sync-pass collection/transport, never called by a bar or tick.

Native readers are an explicit capability: missing implementation is UNKNOWN.
Do not substitute list-today, local BrokerOrder rows or cached positions for it.
"""

import asyncio
import logging
import time
from contextvars import Context, ContextVar
from datetime import UTC, datetime
from functools import wraps
from uuid import uuid4

from sqlalchemy import or_, select

from project_mai_tai.brokerdecides1 import (
    AdmissionMemory, MAX_SYMBOLS, NativeAccountBook, NativeBookPublisher, decode, encode,
)
from project_mai_tai.brokerdecides1_accounts import local_accounts
from project_mai_tai.db.models import BotControlAttempt, BrokerOrder, Fill, Strategy, TradeIntent
from project_mai_tai.events import stream_name
from project_mai_tai.fanout_segment_store import current_session_anchor

LOCAL_CENSUS_DIAGNOSTICS = ContextVar("l12_local_census_diagnostics", default=None)


def enabled(settings):
    return getattr(settings, "strategy_schwab_1m_v2_brokerdecides1_enabled", False) is True


def account_names(settings):
    return (str(settings.strategy_schwab_1m_v2_account_name or "").strip(),
            str(settings.strategy_schwab_1m_v2_webull_account_name or "").strip())


def channel(settings):
    return stream_name(settings.redis_stream_prefix, "brokerdecides1-native-book")


def now_ms():
    return int(datetime.now(UTC).timestamp() * 1000)


def refresh_clock():
    return time.monotonic()


def schedule_refresh(service):
    """Called only AFTER the existing successful sync and its publication.

    No native/Redis/DB await in the caller. The original wire timestamps expire
    normally at 20s; the 30s acquisition limit is not an admission-age waiver.
    """
    if not enabled(service.settings) or service.__dict__.get("_brokerdecides1_closing", False):
        return
    task = service.__dict__.get("_brokerdecides1_refresh_task")
    now = refresh_clock()
    last = service.__dict__.get("_brokerdecides1_refresh_started", -float("inf"))
    if task is not None and not task.done() or now - last < 30.0:
        return
    if any(delegate.cancelled() or not delegate.done() for key in ("_brokerdecides1_native_tasks", "_brokerdecides1_db_tasks")
            for delegate in service.__dict__.get(key, ())):
        return  # A cancelled waiter is not evidence that its worker stopped.
    service._brokerdecides1_refresh_started = now
    task = asyncio.create_task(_background_refresh(service), name="l12-native-refresh", context=Context())
    service._brokerdecides1_refresh_task = task
    task.add_done_callback(lambda done: None if done.cancelled() else done.exception())


async def _background_refresh(service):
    from project_mai_tai.brokerdecides1_collect import (
        BACKGROUND_ALLOWED, BACKGROUND_FOREGROUND_ACTIVE, BACKGROUND_TASKS,
    )

    allowed = lambda: not service.__dict__.get("_brokerdecides1_foreground_syncs", 0) and not service.__dict__.get("_brokerdecides1_closing", False)
    priority_token = BACKGROUND_ALLOWED.set(allowed)
    foreground_token = BACKGROUND_FOREGROUND_ACTIVE.set(
        lambda: bool(service.__dict__.get("_brokerdecides1_foreground_syncs", 0)))
    tasks_token = BACKGROUND_TASKS.set(service.__dict__.setdefault("_brokerdecides1_native_tasks", set()))
    try:
        if not allowed():
            return
        generation = await begin_pass(service)
        await finish_pass(service, generation, sync_succeeded=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        service.logger.exception("[BROKERDECIDES1-PUBLISH] background UNKNOWN entry_allowed=0")
    finally:
        BACKGROUND_TASKS.reset(tasks_token)
        BACKGROUND_FOREGROUND_ACTIVE.reset(foreground_token)
        BACKGROUND_ALLOWED.reset(priority_token)


async def shutdown_refresh(service):
    """Stop waiters, then join actual delegates BEFORE a caller can dispose DB.

    C1 16:49: a five-second waiter timeout is not thread termination. Underlying
    statement/provider timeouts are unchanged. Delay shutdown cancellation until
    workers return/raise and their sessions close; never return an ignored False.
    """
    service._brokerdecides1_closing = True
    service._brokerdecides1_shutdown_joined = False
    memory = getattr(getattr(service, "strategy", None), "_brokerdecides1_memory", None)
    if memory is not None:
        memory.snapshot = None
    join = service.__dict__.get("_brokerdecides1_shutdown_task")
    if join is None:
        caller = asyncio.current_task()

        async def drain():
            waiters = set(service.__dict__.get("_brokerdecides1_pass_waiters", ()))
            refresh = service.__dict__.get("_brokerdecides1_refresh_task")
            if refresh is not None:
                waiters.add(refresh)
            waiters.discard(caller)
            for waiter in waiters:
                waiter.cancel()
            await asyncio.gather(*waiters, return_exceptions=True)
            delegates = set(service.__dict__.get("_brokerdecides1_native_tasks", ()))
            delegates.update(service.__dict__.get("_brokerdecides1_db_tasks", ()))
            if any(task.cancelled() for task in delegates):
                raise RuntimeError("L12_delegate_externally_cancelled_join_UNKNOWN_NO_DISPOSE")
            # Never cancel these tasks: their coroutine includes actual SDK /
            # _run_db thread completion, not just the cancelled outer waiter.
            await asyncio.gather(*delegates, return_exceptions=True)
            if any(task.cancelled() for task in delegates):
                raise RuntimeError("L12_delegate_externally_cancelled_join_UNKNOWN_NO_DISPOSE")

        join = asyncio.create_task(drain(), name="l12-shutdown-delegate-join")
        service._brokerdecides1_shutdown_task = join
    cancelled = False
    while True:
        try:
            await asyncio.shield(join)
            break
        except asyncio.CancelledError:
            if join.cancelled():
                raise RuntimeError("L12_shutdown_join_cancelled_UNKNOWN_NO_DISPOSE")
            cancelled = True
    service._brokerdecides1_shutdown_joined = True
    if cancelled:
        raise asyncio.CancelledError()
    return True


async def _owned_db_delegate(service, submit):
    """Shield the actual DB coroutine, not merely its cancellation-prone waiter.

    _run_db/to_thread own their session INSIDE the thread. Cancelling that
    coroutine would mark its task done before the thread/session was closed.
    Do not cancel these tasks, including during shutdown or caller timeout.
    """
    if service.__dict__.get("_brokerdecides1_closing", False):
        raise ValueError("L12_DB_delegate_shutdown_UNKNOWN")
    tasks = service.__dict__.setdefault("_brokerdecides1_db_tasks", set())
    if any(task.cancelled() or not task.done() for task in tasks):
        raise ValueError("L12_DB_delegate_already_running_UNKNOWN")
    task = asyncio.create_task(submit(), name="l12-owned-db-delegate")
    tasks.add(task)

    def finished(done):
        if done.cancelled():
            return  # No actual thread-exit proof: retain UNKNOWN, never claim joined.
        tasks.discard(done)
        done.exception()  # Retrieve errors even if the original waiter left.

    task.add_done_callback(finished)
    return await asyncio.shield(task)


def _owned_pass(function):
    @wraps(function)
    async def tracked(service, *args, **kwargs):
        if service.__dict__.get("_brokerdecides1_closing", False):
            raise ValueError("L12_pass_shutdown_UNKNOWN")
        from project_mai_tai.brokerdecides1_collect import BACKGROUND_TASKS

        native_token = BACKGROUND_TASKS.set(
            service.__dict__.setdefault("_brokerdecides1_native_tasks", set()))
        waiters = service.__dict__.setdefault("_brokerdecides1_pass_waiters", set())
        current = asyncio.current_task()
        waiters.add(current)
        try:
            return await function(service, *args, **kwargs)
        finally:
            waiters.discard(current)
            BACKGROUND_TASKS.reset(native_token)
    return tracked


def local_nonentry_activity(session, account_ids, terminal):
    """Native absence cannot discharge pending SELL/cancel or control effects.

    All strategies in both configured accounts; no time/session cutoff for
    uncertainty. Existing journal finality states are consumed, never rewritten.
    """
    queries = (
        select(TradeIntent.symbol).where(TradeIntent.broker_account_id.in_(account_ids),
            or_(TradeIntent.side != "buy", TradeIntent.intent_type != "open"),
            or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal))),
        select(BrokerOrder.symbol).where(BrokerOrder.broker_account_id.in_(account_ids),
            BrokerOrder.side != "buy",
            or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal))),
        select(BotControlAttempt.symbol).where(BotControlAttempt.account_id.in_(account_ids),
            or_(BotControlAttempt.status.is_(None),
                BotControlAttempt.status.notin_(("TERMINAL", "PROVEN_NOT_SENT")))),
    )
    symbols = set()
    for query in queries:
        rows = session.scalars(query.limit(MAX_SYMBOLS + 1)).all()
        if len(rows) > MAX_SYMBOLS:
            raise ValueError("nonentry_or_control_census_overflow")
        if any(not isinstance(value, str) or not value or value != value.upper() for value in rows):
            raise ValueError("nonentry_or_control_symbol_UNKNOWN")
        symbols.update(rows)
    if len(symbols) > MAX_SYMBOLS:
        raise ValueError("nonentry_or_control_symbol_overflow")
    return symbols


def local_evidence(session, names):
    """Durable OWN uncertainty/fills only. No native-flat claim comes from this DB."""
    from project_mai_tai.cancelack1 import _closed_with_replacements, working_buy_present

    # Runtime Webull registrations share one configured native account across
    # aliases (e.g. live:orb and v2). DB external_account_id is not that proof.
    # Conservatively include ALL live-name aliases for local uncertainty only;
    # no inferred native-flat scope and no active/session/strategy pruning.
    accounts = local_accounts(session, names)
    scope_names = tuple(account.name for account in accounts)
    account_ids = tuple(a.id for a in accounts)
    strategy_id = session.scalar(select(Strategy.id).where(Strategy.code == "schwab_1m_v2"))
    if strategy_id is None:
        raise ValueError("v2_strategy_identity_UNKNOWN")
    terminal = {"cancelled", "canceled", "filled", "rejected", "expired"}
    intent_symbols = session.scalars(select(TradeIntent.symbol).where(
        TradeIntent.broker_account_id.in_(account_ids), TradeIntent.side == "buy",
        TradeIntent.intent_type == "open",
        or_(TradeIntent.status.is_(None), TradeIntent.status.notin_(terminal))).limit(MAX_SYMBOLS + 1)).all()
    order_symbols = session.scalars(select(BrokerOrder.symbol).where(
        BrokerOrder.broker_account_id.in_(account_ids), BrokerOrder.side == "buy",
        or_(BrokerOrder.status.is_(None), BrokerOrder.status.notin_(terminal))).limit(MAX_SYMBOLS + 1)).all()
    if len(intent_symbols) > MAX_SYMBOLS or len(order_symbols) > MAX_SYMBOLS:
        raise ValueError("inflight_census_overflow")
    candidates = set(intent_symbols) | set(order_symbols)
    # Include durable replacement jobs whose old order may already be terminal.
    from project_mai_tai.db.models import DashboardSnapshot
    from project_mai_tai.brokerdecides1_retirement import unresolved_jobs_query, unresolved_query
    from project_mai_tai.db.models import BrokerDecidesDispatchRetirement

    jobs = session.scalars(unresolved_jobs_query(scope_names).limit(MAX_SYMBOLS + 1)).all()
    if len(jobs) > MAX_SYMBOLS:
        raise ValueError("replacement_census_overflow")
    for job in jobs:
        payload = job.payload
        old = payload.get("old") if isinstance(payload, dict) else None
        if not isinstance(old, dict) or not isinstance(old.get("symbol"), str):
            raise ValueError("replacement_identity_UNKNOWN")
        candidates.add(old["symbol"])
    inflight = local_nonentry_activity(session, account_ids, terminal)
    for symbol in candidates:
        try:
            if working_buy_present(session, symbol):
                inflight.add(symbol)
        except (ValueError, TypeError, ArithmeticError):
            inflight.add(symbol)  # Symbol-scoped proof UNKNOWN is never flat.

    attempts = session.scalars(unresolved_query(scope_names).limit(MAX_SYMBOLS + 1)).all()
    if len(attempts) > MAX_SYMBOLS:
        raise ValueError("dispatch_attempt_census_overflow")
    changed_receipts = set(session.scalars(select(BrokerDecidesDispatchRetirement.attempt_id).where(
        BrokerDecidesDispatchRetirement.attempt_id.in_([attempt.id for attempt in attempts])))) if attempts else set()
    tokens = set()
    scoped_attempts = []
    for attempt in attempts:
        data = attempt.payload
        if not isinstance(data, dict):
            raise ValueError("dispatch_attempt_shape_UNKNOWN")
        if data.get("account_name") not in scope_names:
            raise ValueError("dispatch_attempt_account_UNKNOWN")
        token, symbol = data.get("attempt_token"), data.get("symbol")
        try:
            opportunity = int(data.get("opportunity_id", "0"))
        except (ValueError, TypeError):
            opportunity = 0
        if (not isinstance(token, str) or not token or len(token) > 128
                or token in tokens or not isinstance(symbol, str) or not symbol
                or data.get("strategy_code") != "schwab_1m_v2" or opportunity <= 0
                or not isinstance(data.get("account_names"), list)
                or data["account_name"] not in data["account_names"]):
            raise ValueError("dispatch_attempt_identity_UNKNOWN")
        tokens.add(token)
        scoped_attempts.append((symbol, token, data["account_name"], opportunity, attempt.id in changed_receipts))
    attempt_by_token = {attempt.payload["attempt_token"]: attempt for attempt in attempts}
    token_orders = session.scalars(select(BrokerOrder).where(
        BrokerOrder.payload["clearwait_dispatch_token"].as_string().in_(tokens))
        .limit(MAX_SYMBOLS + 1)).all() if tokens else []
    if len(token_orders) > MAX_SYMBOLS:
        raise ValueError("dispatch_order_census_overflow")
    by_token = {}
    for order in token_orders:
        by_token.setdefault(order.payload["clearwait_dispatch_token"], []).append(order)
    account_id_by_name = {a.name: a.id for a in accounts}
    terminal_reservations = set()
    unknown_rows, unknown_examples = {}, {}
    observed = now_ms()
    for symbol, token, account, opportunity, changed_receipt in scoped_attempts:
        own = by_token.get(token, ())
        if (changed_receipt or not own or any(order.symbol != symbol or order.strategy_id != strategy_id
                or order.broker_account_id != account_id_by_name[account]
                or order.side != "buy"
                or str(order.payload.get("fanout_segment_id")) != str(opportunity)
                or not _closed_with_replacements(session, order, observed)
                for order in own)):
            inflight.add(symbol)  # No row / lost XADD ACK is UNKNOWN, including after restart.
            unknown_rows.setdefault(symbol, set()).add((attempt_by_token[token].id, changed_receipt))
            if symbol not in unknown_examples or changed_receipt:
                unknown_examples[symbol] = (attempt_by_token[token], changed_receipt)
        else:
            terminal_reservations.add((symbol, token))
    rows = session.execute(select(Fill, BrokerOrder).join(BrokerOrder, BrokerOrder.id == Fill.order_id)
        .where(Fill.broker_account_id.in_(account_ids), Fill.strategy_id == strategy_id,
               Fill.side == "buy", Fill.filled_at >= current_session_anchor())
        .limit(MAX_SYMBOLS + 1)).all()
    if len(rows) > MAX_SYMBOLS:
        raise ValueError("buy_fill_census_overflow")
    fills = set()
    for fill, order in rows:
        if (fill.symbol != order.symbol or fill.broker_account_id != order.broker_account_id
                or fill.strategy_id != order.strategy_id or order.side != "buy"
                or fill.quantity is None or fill.quantity <= 0):
            raise ValueError("buy_fill_quantity_UNKNOWN")
        md = order.payload if isinstance(order.payload, dict) else {}
        # New L12 explicit line-segment metadata, NEVER fanout opportunity ID.
        try:
            segment = int(md.get("brokerdecides_line_segment_ms", "0"))
        except (ValueError, TypeError):
            segment = 0
        if segment <= 0:
            inflight.add(order.symbol)  # Legacy mapping absent: cannot disprove same-segment fill.
        else:
            fills.add((order.symbol, segment))
    diagnostics = LOCAL_CENSUS_DIAGNOSTICS.get()
    if diagnostics is not None:
        logged_rows = diagnostics["logged_rows"]
        next_logged_rows = {}
        for symbol, members in unknown_rows.items():
            members = frozenset(members)
            next_logged_rows[symbol] = members
            if logged_rows.get(symbol) != members:
                original, changed_receipt = unknown_examples[symbol]
                stamp = original.created_at
                if stamp is not None:
                    stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
                    created_ms = int(stamp.timestamp() * 1000)
                    age = observed - created_ms if 0 < created_ms <= observed else None
                else:
                    age = None
                logging.getLogger(__name__).warning(
                    "[BROKERDECIDES1-UNKNOWN] symbol=%s age_ms=%s changed_projection=%s",
                    symbol, age, changed_receipt)
        # Retain only this bounded successful census, never a growing history.
        logged_rows.clear()
        logged_rows.update(next_logged_rows)
        diagnostics.update(unknown_symbols=len(unknown_rows),
            rows=sum(len(members) for members in unknown_rows.values()))
    return frozenset(inflight), frozenset(fills), frozenset(terminal_reservations)


async def _publish(service, snapshot):
    # Bounded single replacement, not a growing per-tick stream. Consumer also
    # validates wire age; expiry cannot turn a missing packet into flat evidence.
    await asyncio.wait_for(service.redis.set(channel(service.settings), encode(snapshot), ex=60), 1)


@_owned_pass
async def begin_pass(service):
    if not enabled(service.settings):
        return None
    publisher = service.__dict__.get("_brokerdecides1_publisher")
    if publisher is None:
        publisher = NativeBookPublisher(epoch=str(uuid4()), accounts=account_names(service.settings))
        service._brokerdecides1_publisher = publisher
    generation = publisher.begin(now_ms())
    await _publish(service, publisher.snapshot)
    return generation


def _warn_caller_timeout(service, generation, accounts, books, timeouts):
    """One bounded warning/minute for TimeoutError at the existing await boundary."""
    try:
        observed = time.monotonic()
        previous = service.__dict__.get("_brokerdecides1_caller_timeout_warning_at")
        if previous is not None and observed - previous < 60.0:
            return
        aliases = {"live:orb", "live:schwab_1m_v2", "live:webull", "live:schwab"}
        present = {book.account for book in books}
        missing = ",".join(name if name in aliases else "UNKNOWN"
            for name in accounts if name not in present)
        timed_out = ",".join(name if name in aliases else "UNKNOWN" for name, _ in timeouts)
        elapsed_ms = max(elapsed for _, elapsed in timeouts)
        service.__dict__["_brokerdecides1_caller_timeout_warning_at"] = observed
        service.logger.warning(
            "[BROKERDECIDES1-CALLER-TIMEOUT] generation=%d missing_accounts=%s "
            "timeout_accounts=%s exc=TimeoutError elapsed_ms=%d budget_ms=5000 "
            "elapsed_scope=max_timeout_reader_wait",
            generation, missing, timed_out, elapsed_ms)
    except Exception:
        pass  # Diagnostic sink/state failure cannot change the original UNKNOWN result.


@_owned_pass
async def finish_pass(service, generation, *, sync_succeeded):
    if generation is None:
        return
    publisher = service._brokerdecides1_publisher
    books, inflight, fills, terminals = [], frozenset(), frozenset(), frozenset()
    complete, reason = False, "sync_failed"
    census_diagnostics = dict(unknown_symbols="UNKNOWN", rows="UNKNOWN",
        logged_rows=service.__dict__.setdefault("_brokerdecides1_unknown_log_rows", {}))
    if sync_succeeded:
        # Projection creation is one-time stopped-writer INSTALL work. Runtime
        # retains the full dynamic evidence invalidation census below; a stored
        # receipt or boot coverage count never substitutes for current proof.
        try:
            # Source-qualified adapter capability must include gross positions,
            # all native working orders, true wire interval and full pagination.
            reader = getattr(service.broker_adapter, "read_native_entry_book", None)
            if reader is None:
                raise ValueError("complete_native_reader_UNAVAILABLE")
            errors = []
            caller_timeouts = []
            for name in publisher.accounts:
                reader_started = time.monotonic()
                try:
                    book = await asyncio.wait_for(reader(name), 5)
                    if (not isinstance(book, NativeAccountBook) or book.account != name
                            or book.wire_started_ms < publisher.snapshot.started_ms):
                        raise ValueError("native_account_book_UNKNOWN")
                    books.append(book)
                    if not book.complete:
                        errors.append(book.reason)
                except asyncio.CancelledError:
                    raise
                except TimeoutError:
                    caller_timeouts.append((name,
                        max(0, int((time.monotonic() - reader_started) * 1000))))
                    errors.append("native_account_book_UNKNOWN")
                except Exception:
                    errors.append("native_account_book_UNKNOWN")
            if caller_timeouts:
                _warn_caller_timeout(service, generation, publisher.accounts, books, caller_timeouts)
            if errors:
                raise ValueError(";".join(errors))
            from project_mai_tai.brokerdecides1_collect import require_background_priority

            require_background_priority()
            def read_local(session):
                require_background_priority()
                token = LOCAL_CENSUS_DIAGNOSTICS.set(census_diagnostics)
                try:
                    return local_evidence(session, publisher.accounts)
                finally:
                    LOCAL_CENSUS_DIAGNOSTICS.reset(token)

            inflight, fills, terminals = await _owned_db_delegate(service,
                lambda: service._run_db(read_local, commit=False))
            complete, reason = True, "complete_native_and_local_census"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = f"{type(exc).__name__}:{str(exc)[:80]}"
            service.logger.warning("[BROKERDECIDES1-PUBLISH] generation=%d UNKNOWN reason=%s",
                                   generation, reason)
    snapshot = publisher.finish(generation, completed_ms=now_ms(), books=tuple(books),
        inflight=inflight, filled_segments=fills, local_complete=complete, reason=reason,
        terminal_reservations=terminals)
    await _publish(service, snapshot)
    service.logger.info("[BROKERDECIDES1-UNKNOWN-SUMMARY] generation=%d unknown_symbols=%s rows=%s",
        generation, census_diagnostics["unknown_symbols"] if complete else "UNKNOWN",
        census_diagnostics["rows"] if complete else "UNKNOWN")
    ages = [snapshot.completed_ms - b.wire_started_ms for b in books]
    service.logger.info("[BROKERDECIDES1-PUBLISH] generation=%d complete=%s "
        "accounts=%d oldest_wire_age_ms=%s acquisition_ms=%d reason=%s",
        generation, complete, len(books), max(ages) if ages else "UNKNOWN",
        snapshot.completed_ms - snapshot.started_ms, reason)


@_owned_pass
async def refresh_consumer(service):
    """Background position poll only; the bar/quote handler reads this memory."""
    if not enabled(service.settings):
        return
    memory = getattr(service.strategy, "_brokerdecides1_memory", None)
    if memory is None:
        memory = AdmissionMemory()
        service.strategy._brokerdecides1_memory = memory
    try:
        raw = await asyncio.wait_for(service.redis.get(channel(service.settings)), 1)
        memory.apply(decode(raw))
        tokens = memory.reservation_tokens()
        if tokens and memory.snapshot.local_complete:
            from project_mai_tai.brokerdecides1_retirement import load_terminal_tokens

            factory = getattr(service, "session_factory", None)
            if factory is None:
                raise ValueError("reservation_closure_factory_UNKNOWN")

            def read_terminal():
                with factory() as session:
                    return load_terminal_tokens(session, account_names(service.settings), tokens)

            for symbol, token in await _owned_db_delegate(service,
                    lambda: asyncio.to_thread(read_terminal)):
                memory.terminal(symbol, token, proven=True)
    except asyncio.CancelledError:
        raise
    except Exception:
        memory.snapshot = None
        service.strategy._brokerdecides1_memory = memory
        logging.getLogger(__name__).warning("[BROKERDECIDES1-CONSUME] UNKNOWN entry_allowed=0")
    # No provider or additional DB census. Retire only a provably unused owner
    # through its normal persistence path; other owners stay actively fenced.
    from project_mai_tai.brokerdecides1_entry import recover_unused_current_short
    from project_mai_tai.brokerdecides1_owner_watchdog import owner_watchdog_pass

    for state in tuple(getattr(service.strategy, "_symbol_states", {}).values()):
        recover_unused_current_short(service.strategy, state)
    await owner_watchdog_pass(service)

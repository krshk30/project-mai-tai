"""C1 boot-only finite fleet extension of reviewed L11 OwnerClock.

Derived from 94b0814 tools/keepwatch_weekend_owner_clock.py SHA
5eae482762804531275ffdea9f9673b5fa1aaa18d3a2fad4e15021ede5853946.
No initializer, Settings overlay, DB factory, seed latch or provider is added.
"""

"""Explicit reconstructed owner cadence; delegates every APP ownership action.

Only owner Event.wait timeouts use model time. DB/provider/rebuild wait_for
calls still use the real asyncio implementation and their unchanged deadlines.
"""

import asyncio
from contextlib import ExitStack, contextmanager
from decimal import Decimal
from unittest.mock import patch


POLICY = 'RECONSTRUCTED_RETIMED_FLEET_OWNER_UNCHANGED_INTERNAL_CADENCE_V1'
BOOT_DEFERRAL_CLOCK_POLICY = 'RECONSTRUCTED_ELAPSED_SAME_OWNER_CLOCK_NO_SOURCE_RETIMING_V1'
BOOT_CLOCK_REFERENCE_HEAD = '731b96f6a2d88cd181f2aba8f92bb349520d3e7e'
BOOT_CLOCK_REFERENCE_SHA = 'df1f3fb1cd31f3cff4cee78f75da9c89141fb8e30feae51a57d8bb7052a255dd'


def require_boot_deferral_review(review):
    from tools.l12_private_schema_evidence import BOUNDED_L11_APP_COMPONENT

    fields = {'version', 'app_component', 'clock_policy', 'reference_head',
        'reference_file_sha256', 'review_sha256', 'scope'}
    if (type(review) is not dict or set(review) != fields
            or review['version'] != 'L12_4F_INSTANCE_ELAPSED_CLOCK_REVIEW_V1'
            or review['app_component'] != BOUNDED_L11_APP_COMPONENT
            or review['clock_policy'] != BOOT_DEFERRAL_CLOCK_POLICY
            or review['reference_head'] != BOOT_CLOCK_REFERENCE_HEAD
            or review['reference_file_sha256'] != BOOT_CLOCK_REFERENCE_SHA
            or review['scope'] != 'BOOT_ONLY_ELAPSED_NOT_NATIVE_OR_GLOBAL_TIMEOUTS'
            or type(review['review_sha256']) is not str or len(review['review_sha256']) != 64
            or review['review_sha256'] == '0' * 64
            or any(c not in '0123456789abcdef' for c in review['review_sha256'])):
        raise OwnerProtocolRefusal('literal_4f_elapsed_clock_independent_review_required')


@contextmanager
def boot_deferral_clock(bot, clock):
    """Carry Galileo731b's instance-only elapsed port; never alter real deadlines.

    Same clock arithmetic/policy as the reviewed reference. The outer finally
    also records restoration on exceptional exit, not only successful exit.
    """
    original = bot._keepwatch_boot_retry_clock
    start = previous = clock.now_ns
    if type(start) is not int or start < 0 or not callable(original):
        raise OwnerProtocolRefusal('explicit monotonic boot elapsed-clock port required')
    record = {'policy': BOOT_DEFERRAL_CLOCK_POLICY, 'origin_ns': start,
        'scope': 'RECONSTRUCTED_ELAPSED_NOT_HISTORICAL_MONOTONIC',
        'full_state_publisher': 'UNEXERCISED', 'warmup_timeout_monotonic': 'UNMODELED',
        'restored': False}

    def elapsed():
        nonlocal previous
        current = clock.now_ns
        if type(current) is not int or current < previous:
            raise OwnerProtocolRefusal('boot elapsed clock rewind refused; no repair')
        previous = current
        return (current - start) / 1_000_000_000

    try:
        with patch.object(bot, '_keepwatch_boot_retry_clock', elapsed):
            try:
                yield record
            finally:
                record['final_observed_ns'] = previous
    finally:
        record['restored'] = bot._keepwatch_boot_retry_clock is original


async def scheduled(steps, clock, owner=None):
    """Keep original ordering; same-clock native retry follows membership/entry."""
    for step in steps:
        if owner is None:
            clock.advance(step['clock_ns'])
        else:
            await owner.advance_before(step['clock_ns'])
            if step['kind'] not in {'membership', 'scanner_delta', 'entry_input'}:
                await owner.wake_due()
        yield step
        if owner is not None:
            await owner.settle()


class OwnerProtocolRefusal(ValueError):
    pass


class OwnerWait:
    def __init__(self, event):
        self.event = event


class OwnerEvent(asyncio.Event):
    def __init__(self, driver, symbol):
        super().__init__()
        self.driver, self.symbol = driver, symbol

    def wait(self):
        return OwnerWait(self)

    def set(self):
        super().set()
        for task, parked in tuple(self.driver.parked.items()):
            if parked['event'] is self and not parked['future'].done():
                parked['future'].set_result(True)
        self.driver.changed.set()


class AsyncioPort:
    def __init__(self, driver):
        self.driver = driver

    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def wait_for(self, awaitable, timeout):
        if not isinstance(awaitable, OwnerWait):
            return await asyncio.wait_for(awaitable, timeout)
        driver, event = self.driver, awaitable.event
        if event.driver is not driver or Decimal(str(timeout)) * 1000000000 != driver.interval_ns:
            raise OwnerProtocolRefusal('foreign event or changed actual owner poll interval')
        if event.is_set():
            return True
        task = asyncio.current_task()
        if task not in driver.owner_tasks():
            raise OwnerProtocolRefusal('only actual owned APP task may park')
        future = asyncio.get_running_loop().create_future()
        driver.parked[task] = {'future': future, 'event': event,
                               'deadline_ns': driver.clock.now_ns + driver.interval_ns}
        driver.record('OWNER_PARK', event.symbol)
        driver.changed.set()
        try:
            return await future
        finally:
            driver.parked.pop(task, None)
            driver.changed.set()


class OwnerClock:
    def __init__(self, bot, service, clock, symbols, records, *, maximum_actions, before_action):
        if (type(symbols) is not tuple or not symbols or len(set(symbols)) != len(symbols)
                or any(type(name) is not str or not name for name in symbols)):
            raise OwnerProtocolRefusal('explicit finite watched-name scope required')
        if type(maximum_actions) is not int or maximum_actions <= 0:
            raise OwnerProtocolRefusal('explicit positive finite owner budget required')
        self.bot, self.service, self.clock = bot, service, clock
        self.symbols = symbols
        self.symbol = symbols[0]
        self.records, self.before_action = records, before_action
        interval = Decimal(str(bot.settings.strategy_schwab_1m_v2_bar_poll_interval_seconds)) * 1000000000
        if interval <= 0 or interval != interval.to_integral_value():
            raise OwnerProtocolRefusal('exact retained cadence required; no rounding')
        self.interval_ns = int(interval)
        self.maximum_actions = maximum_actions
        self.actions, self.active, self.advances = 0, 0, 0
        self.parked, self.watched = {}, set()
        self.failed_tasks = []
        self.protocol_failure = None
        self.inboxes = []
        self.changed = asyncio.Event()
        self._advance = clock.advance
        self.last_external_clock = None

    def owner_tasks(self):
        if set(self.bot._keepwatch_bar_tasks) - set(self.symbols):
            raise OwnerProtocolRefusal('unbound symbol owner cannot borrow fleet clock')
        return {task for task in self.bot._keepwatch_bar_tasks.values() if not task.done()}

    def snapshot(self, work=None, *, symbol=None):
        bot, symbol = self.bot, self.symbol if symbol is None else symbol
        if symbol not in self.symbols:
            raise OwnerProtocolRefusal('foreign owner snapshot')
        ledger = bot._line_sessions.get(symbol)
        state = bot.strategy._symbol_states.get(symbol)
        inbox = bot._keepwatch_bar_inboxes.get(symbol)
        if inbox is not None and all(inbox is not prior for prior in self.inboxes):
            self.inboxes.append(inbox)
        published = bot._line_published.get(symbol)
        proof = getattr(ledger, '_coverage', None)
        counters = ({key: sum(getattr(box, key) for box in self.inboxes)
            for key in ('received', 'accepted', 'revised', 'consumed', 'covered',
                        'expired', 'rejected', 'failures', 'historical_ignored')} if self.inboxes else {})
        pending = sum(len(box.pending) for box in self.inboxes)
        return {'clock_ns': self.clock.now_ns, 'symbol': symbol,
            'ledger_epoch': getattr(ledger, 'epoch', None), 'ledger_revision': getattr(ledger, 'revision', None),
            'actual_incomplete_reason': getattr(ledger, 'incomplete_reason', None),
            'coverage_present': proof is not None, 'coverage_source': getattr(proof, 'source', None),
            'state_last_bar_ms': state.bars[-1].timestamp_ms if state is not None and state.bars else None,
            'published_bar_ms': published.request.current_bar_ms if published is not None else None,
            'published_spanning_pairs': list(published.request.spanning_pairs) if published is not None else [],
            'source_unknown': symbol in bot._keepwatch_unknown,
            'work': ({'bar_ms': work.bar.timestamp_ms, 'source': work.source,
                'anchor_ms': work.anchor_ms, 'epoch': work.epoch,
                'ohlcv': {key: str(getattr(work.bar, key)) for key in ('open', 'high', 'low', 'close', 'volume')},
                'identity_scope': 'ACTUAL_APP_OWNED_WORK_NOT_NATIVE_EVENT_ID'} if work is not None else None),
            'pending': pending, 'inbox': counters,
            'conservation': (counters['accepted'] == counters['consumed'] + counters['covered']
                + counters['expired'] + pending and counters['received'] == counters['accepted']
                + counters['revised'] + counters['rejected'] + counters['historical_ignored']) if counters else True}

    def record(self, stage, symbol=None, work=None, **fields):
        value = self.snapshot(work, symbol=symbol)
        if symbol is not None and symbol not in self.symbols:
            raise OwnerProtocolRefusal('foreign owner observation')
        if not value['conservation']:
            raise OwnerProtocolRefusal('actual inbox accounting mismatch; no dropped work')
        self.records.append(dict(value, stage=stage, **fields))

    def _unsettled(self):
        if self.protocol_failure is not None:
            raise OwnerProtocolRefusal(self.protocol_failure)
        tasks = self.owner_tasks()
        def completed(task):
            if not self.bot._stop_event.is_set():
                failure = 'CancelledError' if task.cancelled() else (
                    type(task.exception()).__name__ if task.exception() is not None else None)
                if failure:
                    self.failed_tasks.append(failure)
            self.changed.set()
        for task in tasks - self.watched:
            self.watched.add(task)
            task.add_done_callback(completed)
        if self.failed_tasks:
            raise OwnerProtocolRefusal('actual owner task failed; pending work not adopted as success')
        return self.active or any(task not in self.parked or self.parked[task]['future'].done() for task in tasks)

    async def settle(self):
        # Original APP operations have their own unchanged timeouts. If they do
        # not finish, time stays fixed and Main's card watchdog owns disposition.
        while True:
            self.changed.clear()
            if not self._unsettled():
                self.record('OWNER_SETTLED')
                return
            await self.changed.wait()

    def advance_checked(self, value):
        if self._unsettled():
            raise OwnerProtocolRefusal('cannot advance model time while an owner action is unresolved')
        if self.parked and min(row['deadline_ns'] for row in self.parked.values()) < value:
            raise OwnerProtocolRefusal('cannot skip actual reconstructed owner deadline')
        self._advance(value)
        self.advances += 1

    async def advance_before(self, value):
        await self.settle()
        while self.parked:
            due = min(row['deadline_ns'] for row in self.parked.values())
            if due >= value:
                break
            self.advance_checked(due)
            await self.wake_due()
        self.advance_checked(value)
        self.last_external_clock = value

    async def wake_due(self):
        await self.settle()
        due = [row for row in self.parked.values() if row['deadline_ns'] == self.clock.now_ns]
        if due:
            self.before_action({'kind': 'owner_deadline', 'clock_ns': self.clock.now_ns})
            self.record('OWNER_CADENCE_WAKE', count=len(due))
            for row in due:
                if not row['future'].done():
                    row['future'].set_exception(TimeoutError())
            await self.settle()

    async def enqueue(self, bar):
        if bar.symbol not in self.symbols:
            raise OwnerProtocolRefusal('foreign bar cannot borrow fleet owner clock')
        self.bot._enqueue_keepwatch_bar(bar.symbol, bar, 'rest')
        self.record('REAL_ENQUEUE')
        await self.settle()

    @contextmanager
    def bound(self):
        if self.bot._keepwatch_bar_tasks:
            raise OwnerProtocolRefusal('unstarted local APP only; never adopt foreign owner tasks')
        for symbol in self.symbols:
            original_event = self.bot._keepwatch_bar_events.get(symbol)
            event = OwnerEvent(self, symbol)
            if original_event is not None and original_event.is_set():
                event.set()
            self.bot._keepwatch_bar_events[symbol] = event
        dispatch = self.bot._keepwatch_dispatch_bar
        prepare = self.bot._keepwatch_prepare_bar
        hold = self.bot._keepwatch_hold_unknown
        enqueue = self.bot._enqueue_keepwatch_bar

        def observed_enqueue(symbol, bar, source):
            if symbol not in self.symbols:
                raise OwnerProtocolRefusal('foreign symbol cannot borrow owner clock')
            current = self.bot._keepwatch_bar_events.get(symbol)
            if current is None:
                self.bot._keepwatch_bar_events[symbol] = OwnerEvent(self, symbol)
            elif not isinstance(current, OwnerEvent) or current.driver is not self:
                raise OwnerProtocolRefusal('foreign owner event cannot borrow model time')
            return enqueue(symbol, bar, source)

        async def observed_dispatch(*args, **kwargs):
            symbol = args[0] if args else kwargs.get('symbol')
            if symbol not in self.symbols:
                raise OwnerProtocolRefusal('unbound dispatch cannot borrow fleet clock')
            self.actions += 1
            if self.actions > self.maximum_actions:
                self.protocol_failure = 'finite prepared owner action envelope exceeded; no growth'
                self.changed.set()
                raise OwnerProtocolRefusal(self.protocol_failure)
            self.active += 1
            try:
                self.before_action({'kind': 'owner_dispatch', 'clock_ns': self.clock.now_ns,
                                    'action_number': self.actions})
                self.record('DISPATCH_BEGIN', symbol, work=kwargs.get('expected_work'))
                value = await dispatch(*args, **kwargs)
                self.record('DISPATCH_RETURN', symbol, work=kwargs.get('expected_work'), returned=value)
                return value
            finally:
                self.active -= 1
                self.changed.set()

        async def observed_prepare(*args, **kwargs):
            symbol = args[0] if args else kwargs.get('symbol')
            if symbol not in self.symbols:
                raise OwnerProtocolRefusal('unbound prepare cannot borrow fleet clock')
            try:
                value = await prepare(*args, **kwargs)
            except BaseException as exc:
                self.record('PREPARE_RAISED', symbol, work=kwargs.get('expected_work'),
                            exception_type=type(exc).__name__)
                raise
            self.record('PREPARE_RETURN', symbol, work=kwargs.get('expected_work'), returned=value)
            return value

        def observed_hold(symbol, reason):
            value = hold(symbol, reason)
            self.record('ACTUAL_HOLD', symbol, reason=reason)
            return value

        primary = None
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(self.service, 'asyncio', AsyncioPort(self)))
                stack.enter_context(patch.object(self.clock, 'advance', self.advance_checked))
                stack.enter_context(patch.object(self.bot, '_keepwatch_dispatch_bar', observed_dispatch))
                stack.enter_context(patch.object(self.bot, '_keepwatch_prepare_bar', observed_prepare))
                stack.enter_context(patch.object(self.bot, '_keepwatch_hold_unknown', observed_hold))
                stack.enter_context(patch.object(self.bot, '_enqueue_keepwatch_bar', observed_enqueue))
                yield self
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if self.owner_tasks():
                reason = 'actual APP owner shutdown/join required before unbinding'
                self.protocol_failure = self.protocol_failure or reason
                if primary is None:
                    raise OwnerProtocolRefusal(reason)
                BaseException.add_note(primary,
                    'cleanup_refused=OwnerProtocolRefusal; unjoined_owner_tasks=1')


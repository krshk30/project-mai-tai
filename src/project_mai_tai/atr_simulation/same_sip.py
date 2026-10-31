"""Explicit conservative paper rules for previously unknown SIP-tie cases.

No native sequence, eligibility, price/volume, clock or sizing is invented.
The caller retains every non-SIP coverage/eligibility fault. None is legacy.
"""
from .same_sip_eligibility import execution_status

SAME_SIP_CONSERVATIVE_V1 = 'C1_20261028_184635_CONSERVATIVE_TIES_V1'
LABEL = 'tie → conservative'


def report_key(identity, world):
    return ((identity.trading_day, identity.symbol, identity.broker, identity.account_id,
             identity.strategy, identity.flip_id, identity.segment_id), str(world))


def enabled(value):
    if value is None:
        return False
    if value != SAME_SIP_CONSERVATIVE_V1:
        raise ValueError('explicit_supported_same_sip_policy_required')
    return True


class TieTape:
    """Invocation-local immutable tied groups, shared by the cohort's states."""
    def __init__(self, trades):
        self.groups = {}
        self._eligible = {}
        self._unresolved = {}
        self._bounds = {}
        self._exit_summary = {}
        self._bands = {}
        self._unknown_witness = {}
        current, symbols = None, {}
        def retain():
            for symbol, rows in symbols.items():
                if len(rows) > 1:
                    self.groups[symbol, current] = tuple(rows)
        for trade in trades:
            clock = trade.key.event_ns
            if current is not None and clock < current:
                raise ValueError('ascending_sip_tape_required')
            if current != clock:
                retain()
                current, symbols = clock, {}
            symbols.setdefault(trade.symbol, []).append(trade)
        retain()

    def group(self, trade):
        return self.groups.get((trade.symbol, trade.key.event_ns), ())

    def eligible(self, trade):
        group = self.group(trade)
        if not group:
            return ()
        key = trade.symbol, trade.key.event_ns
        if key not in self._eligible:
            self._eligible[key] = tuple(row for row in group
                                        if execution_status(row)[0] is True)
        return self._eligible[key]

    def unresolved(self, trade):
        group = self.group(trade)
        if not group:
            return False
        key = trade.symbol, trade.key.event_ns
        if key not in self._unresolved:
            self._unresolved[key] = any(execution_status(row)[0] is None for row in group)
        return self._unresolved[key]

    def exit_summary(self, trade, round_lot):
        """One immutable group scan; changing live thresholds stay uncached."""
        key = trade.symbol, trade.key.event_ns, bool(round_lot)
        if key not in self._exit_summary:
            rows = tuple(row for row in self.eligible(trade)
                         if not round_lot or row.size >= 100)
            self._exit_summary[key] = (
                min(rows, key=lambda row: (row.price, row.key.sequence, row.key.raw_id), default=None),
                max((row.price for row in rows), default=None),
            )
        return self._exit_summary[key]

    def band_mixed(self, trade, low, high):
        # Each episode's fixed entry band scans a group once, then every print
        # uses a dictionary lookup. Bounds alone cannot prove a price in a gap.
        key = trade.symbol, trade.key.event_ns, low, high
        if key not in self._bands:
            rows = self.eligible(trade)
            self._bands[key] = (any(low <= row.price <= high for row in rows)
                               and any(not low <= row.price <= high for row in rows))
        return self._bands[key]

    def unknown_witness(self, trade):
        key = trade.symbol, trade.key.event_ns
        if key not in self._unknown_witness:
            group = self.group(trade)
            unknown = tuple(row for row in group if execution_status(row)[0] is None)
            self._unknown_witness[key] = (
                min((row.key.sequence for row in group), default=None),
                unknown[0] if unknown else None,
            )
        return self._unknown_witness[key]

    def bounds(self, trade):
        key = trade.symbol, trade.key.event_ns
        if key not in self._bounds:
            rows = self.eligible(trade)
            self._bounds[key] = (min((row.price for row in rows), default=None),
                                 max((row.price for row in rows), default=None))
        return self._bounds[key]


class TieState:
    """One episode/world. Counters never mutate the shared physical ledger."""
    def __init__(self, tape):
        self.tape = tape
        self.blocked = set()
        self.stop_wins = set()
        self.limit_fills = set()
        self.events = []

    @property
    def touched(self):
        return bool(self.blocked or self.stop_wins or self.limit_fills)

    def _record(self, rule, trade):
        if len(self.events) < 32:
            group = self.tape.group(trade)
            self.events.append(dict(rule=rule, symbol=trade.symbol, event_ns=trade.key.event_ns,
                raw_ids=[row.key.raw_id for row in group[:32]], raw_count=len(group),
                raw_ids_truncated=len(group) > 32))

    def entry_allowed(self, trade, limit, *, kind='limit', placed_ns=None):
        group = self.tape.group(trade)
        if not group:
            return True
        key = trade.symbol, trade.key.event_ns, kind
        if key in self.blocked:
            return False
        low, high = self.tape.bounds(trade)
        if kind == 'limit':
            reaches = low is not None and low <= limit
            away = high is not None and high > limit
        elif kind == 'stop':
            reaches = high is not None and high >= limit
            away = low is not None and low < limit
        else:
            raise ValueError('explicit_tie_entry_kind_required')
        blocked = reaches and away or placed_ns is not None and trade.key.event_ns <= placed_ns
        if blocked:
            if key not in self.blocked:
                self.blocked.add(key)
                self._record('ENTRY_MIXED_OR_PLACEMENT_TIMESTAMP_NOT_FILLED', trade)
        return not blocked

    def entry_in_band(self, trade, low, high, *, placed_ns=None):
        if not self.tape.group(trade):
            return True
        key = trade.symbol, trade.key.event_ns, 'band'
        if key in self.blocked:
            return False
        mixed = self.tape.band_mixed(trade, low, high)
        blocked = mixed or placed_ns is not None and trade.key.event_ns <= placed_ns
        if blocked:
            if key not in self.blocked:
                self.blocked.add(key)
                self._record('ENTRY_MIXED_OR_PLACEMENT_TIMESTAMP_NOT_FILLED', trade)
        return not blocked

    def stop_choice(self, trade, *, stop, target, round_lot=True):
        group = self.tape.group(trade)
        if not group or self.tape.unresolved(trade):
            return None
        smallest, largest = self.tape.exit_summary(trade, round_lot)
        if smallest is None or smallest.price > stop or largest < target:
            return None
        key = trade.symbol, trade.key.event_ns
        if key not in self.stop_wins:
            self.stop_wins.add(key)
            self._record('STOP_WINS_TARGET_STOP_TIMESTAMP', trade)
        return smallest

    def at_limit(self, trade):
        if not self.tape.group(trade):
            raise ValueError('actual_tie_group_required_for_limit_pricing')
        key = trade.symbol, trade.key.raw_id
        if key not in self.limit_fills:
            self.limit_fills.add(key)
            self._record('TIE_CASE_LIMIT_NO_IMPROVEMENT', trade)

    def report(self):
        count = len(self.blocked) + len(self.stop_wins) + len(self.limit_fills)
        return dict(policy=SAME_SIP_CONSERVATIVE_V1, label=LABEL,
                    same_timestamp_groups=len(self.tape.groups),
                    entry_groups_blocked=len(self.blocked), exit_stop_wins=len(self.stop_wins),
                    limit_pricing_fills=len(self.limit_fills), events=self.events,
                    events_truncated=count > len(self.events))

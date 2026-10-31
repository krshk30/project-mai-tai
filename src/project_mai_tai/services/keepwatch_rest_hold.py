"""C7-F1 process-local cancel deferral; never entry or native-book permission.

The service must reverify every proof operand before calling this policy.
Only a real PROVEN/BOTH_EMPTY transition may clear an episode. No clocks,
timers, provider reads, persistence, or diagnostic records supply authority.
"""
from dataclasses import dataclass


@dataclass(frozen=True, eq=False)
class RestHoldProof:
    symbol: str
    anchor_ms: int
    epoch: int
    ledger: object
    state: object
    source_identity: object
    work: object
    trade_token: object
    rest_identity: tuple
    attempt: object

    def same_scope(self, other):
        return (type(other) is RestHoldProof and self.symbol == other.symbol
                and self.anchor_ms == other.anchor_ms and self.epoch == other.epoch
                and self.ledger is other.ledger and self.state is other.state
                and self.source_identity == other.source_identity
                and self.work is other.work and self.trade_token == other.trade_token
                and self.rest_identity == other.rest_identity)


@dataclass
class _Pending:
    proof: RestHoldProof | None
    exhausted: bool


class RestHoldPolicy:
    """At most one record per symbol, bounded independently of watchlist churn."""
    MAX_PENDING = 4096

    def __init__(self):
        self._pending = {}

    def cancel_required(self, symbol, proof=None):
        """None means a hard/unproved reason: latch immediate cancellation.

        A proof grants only the first existing-rest cancel deferral. Repeated
        calls from that same owned attempt do not spend another attempt.
        Failed cancel callbacks cannot reset an exhausted record.
        """
        current = self._pending.get(symbol)
        valid = (type(proof) is RestHoldProof and proof.symbol == symbol
                 and type(proof.anchor_ms) is int and proof.anchor_ms > 0
                 and type(proof.epoch) is int and proof.epoch > 0
                 and proof.ledger is not None and proof.state is not None
                 and proof.source_identity is not None and proof.trade_token is not None
                 and proof.attempt is not None and bool(proof.rest_identity))
        if current is None:
            if len(self._pending) >= self.MAX_PENDING:
                return True
            self._pending[symbol] = _Pending(proof if valid else None, not valid)
            return not valid
        if (current.exhausted or not valid or current.proof is None
                or not current.proof.same_scope(proof)
                or current.proof.attempt is not proof.attempt):
            current.exhausted = True
            return True
        return False

    def deferring(self, symbol):
        """Only a live F1 grace record adds an entry fence.

        Ordinary history-pending UNKNOWN marks must retain the preexisting
        current-publication admission path. This query grants no permission.
        """
        current = self._pending.get(symbol)
        return bool(current is not None and not current.exhausted
                    and current.proof is not None)

    def clear_proven(self, symbol):
        self._pending.pop(symbol, None)

    def retain_members(self, symbols):
        for symbol in tuple(self._pending):
            if symbol not in symbols:
                self._pending.pop(symbol)

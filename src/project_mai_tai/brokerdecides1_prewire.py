"""Positive, exact-attempt no-XADD proof. Never infer it from missing OMS rows.

Main owns the emitter's sequencing. Bind before its awaited guard, then validate
that guard's PRE-STAMP draft, run prepare_send/build the payload synchronously,
and invoke XADD through this boundary. Only the refusal path may persist proof
afterwards. No storage or await is added to the successful final admission gate.
"""

from dataclasses import dataclass, field
from hashlib import sha256
import json
from uuid import UUID, uuid4

STRATEGY = "schwab_1m_v2"
REFUSAL_TYPE = "v2_dispatch_prewire_refusal"
PROOF_VERSION = 3
_AUTHORITY = object()
_STAMP_KEYS = frozenset({"brokerdecides_line_segment_ms", "brokerdecides_reservation_token"})


@dataclass(frozen=True)
class AttemptIdentity:
    symbol: str
    account: str
    accounts: tuple[str, ...]
    opportunity: str
    token: str
    draft_sha256: str


def _draft_digest(draft):
    metadata = {k: v for k, v in draft.metadata.items() if k not in _STAMP_KEYS}
    raw = json.dumps([draft.symbol, draft.side, draft.intent_type, str(draft.quantity),
                      draft.reason, metadata], sort_keys=True, separators=(",", ":"),
                     allow_nan=False).encode()
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class BeforeXaddRefusal:
    identity: AttemptIdentity
    receipt_id: UUID
    observed_ms: int
    reason: str
    _issuer: object = field(repr=False, compare=False)

    def validate(self):
        if (not isinstance(self._issuer, PublicationBoundary)
                or self._issuer.phase != "REFUSED"
                or self._issuer.refusal is not self
                or self._issuer.identity != self.identity):
            raise ValueError("positive_prewire_proof_UNKNOWN")
        self._issuer._capability.assert_owner(self._issuer, "REFUSED")

    def payload(self):
        self.validate()
        i = self.identity
        cap = self._issuer._capability
        return {"schema_version": 2, "phase": "BEFORE_XADD", "strategy_code": STRATEGY,
                "symbol": i.symbol, "account_name": i.account, "account_names": list(i.accounts),
                "opportunity_id": i.opportunity, "attempt_token": i.token,
                "draft_sha256": i.draft_sha256, "receipt_id": self.receipt_id.hex,
                "observed_ms": self.observed_ms, "reason": self.reason,
                "attempt_id": cap.attempt_id.hex, "journal_nonce": cap.nonce}


@dataclass(frozen=True)
class CommittedPrewireRefusal:
    identity: AttemptIdentity
    attempt_id: UUID
    receipt_id: UUID
    _proof: BeforeXaddRefusal = field(repr=False, compare=False)
    _authority: object = field(repr=False, compare=False)


class PublicationBoundary:
    """One emitter invocation. No shared ContextVar and no unrelated-task scope.

    Refusal and transport invocation are mutually exclusive, irreversible states.
    A crash, missing receipt, cancellation in XADD, or unknown ACK stays UNKNOWN.
    The owner must never call the transport outside invoke_xadd for this attempt.
    """

    def __init__(self, draft, account, accounts):
        accounts = tuple(accounts)
        token = draft.metadata.get("clearwait_dispatch_token")
        opportunity = draft.metadata.get("fanout_segment_id")
        if (draft.side != "buy" or draft.intent_type != "open"
                or not isinstance(draft.symbol, str) or not draft.symbol
                or draft.symbol != draft.symbol.upper() or len(draft.symbol) > 32
                or len(accounts) != 2 or len(set(accounts)) != 2
                or any(not isinstance(a, str) or not a or len(a) > 64 for a in accounts)
                or account not in accounts
                or not isinstance(token, str) or not token or len(token) > 128
                or not isinstance(opportunity, str) or not opportunity.isdecimal()
                or int(opportunity) <= 0):
            raise ValueError("prewire_attempt_identity_UNKNOWN")
        self._identity = AttemptIdentity(draft.symbol, account, accounts, opportunity, token,
                                         _draft_digest(draft))
        from project_mai_tai.brokerdecides1_prewire_authority import FreshJournalCapability, SIDECAR

        capability = getattr(draft, SIDECAR, None)
        if type(capability) is not FreshJournalCapability:
            raise ValueError("fresh_journal_capability_missing")
        capability.claim(self, self.identity)
        self._capability = capability
        self._draft = draft
        self._phase = "BEFORE_XADD"
        self._refusal = None

    @property
    def identity(self):
        return self._identity

    @property
    def phase(self):
        return self._phase

    @property
    def refusal(self):
        return self._refusal

    def validate_draft(self):
        if _draft_digest(self._draft) != self.identity.draft_sha256:
            raise ValueError("prewire_draft_changed")

    def refuse(self, reason, observed_ms):
        if (self.phase != "BEFORE_XADD" or type(observed_ms) is not int or observed_ms <= 0
                or not isinstance(reason, str) or not reason or len(reason) > 256
                or any(ord(c) < 32 for c in reason)):
            raise ValueError("prewire_refusal_not_proven")
        self._capability.transition(self, "REFUSED")
        self._phase = "REFUSED"
        self._refusal = BeforeXaddRefusal(self.identity, uuid4(), observed_ms, reason, self)
        return self.refusal

    async def invoke_xadd(self, sender, *args, **kwargs):
        if self.phase != "BEFORE_XADD":
            raise ValueError("prewire_boundary_already_consumed")
        self.validate_draft()
        # Before CALLING the transport, not after its first await or reply.
        self._capability.transition(self, "XADD_INVOKED")
        self._phase = "XADD_INVOKED_UNKNOWN"
        result = await sender(*args, **kwargs)
        self._phase = "XADD_ACKNOWLEDGED"
        return result

    def release_after_commit(self, memory, committed):
        if (not isinstance(committed, CommittedPrewireRefusal)
                or committed._authority is not _AUTHORITY
                or self.phase != "REFUSED" or committed._proof is not self.refusal
                or committed.identity != self.identity
                or committed.receipt_id != self.refusal.receipt_id):
            raise ValueError("prewire_commit_identity_UNKNOWN")
        return memory.terminal(self.identity.symbol, self.identity.token, proven=True)


def commit_refusal(session_factory, proof):
    """Refusal-only off-hot-path transaction + independent committed readback.

    Commit/ACK/readback uncertainty never releases RAM. A committed receipt can
    still be discovered by the background census after a crash/restart.
    """
    from sqlalchemy import select, text
    from project_mai_tai.brokerdecides1_prewire_proof import (
        project_refusal, valid_prewire_ids,
    )
    from project_mai_tai.db.models import DashboardSnapshot

    if not isinstance(proof, BeforeXaddRefusal):
        raise ValueError("positive_prewire_proof_required")
    proof.validate()
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        attempt_id = project_refusal(session, proof)
        session.commit()
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        found = session.scalar(select(DashboardSnapshot.id).where(
            DashboardSnapshot.id == attempt_id,
            DashboardSnapshot.id.in_(valid_prewire_ids())))
        if found != attempt_id:
            raise ValueError("prewire_committed_readback_UNKNOWN")
    return CommittedPrewireRefusal(proof.identity, attempt_id, proof.receipt_id, proof, _AUTHORITY)

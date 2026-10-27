"""Fresh journal-commit capability. Durable row reads never mint authority.

No global token registry: the new attempt's deterministic PK prevents concurrent
duplicate creation, while one process-local capability is consumed once. Missing
commit ACK/readback or restart loses capability and therefore retains UNKNOWN.
"""

from copy import deepcopy
from threading import Lock
from uuid import NAMESPACE_URL, uuid4, uuid5

_ISSUER = object()
SIDECAR = "_brokerdecides1_fresh_journal_capability"


class FreshJournalCapability:
    __slots__ = ("_attempt_id", "_scope", "_nonce", "_state", "_owner", "_lock")

    def __init__(self, seal, attempt_id, scope, nonce):
        if seal is not _ISSUER:
            raise ValueError("fresh_journal_capability_not_issued")
        self._attempt_id, self._scope, self._nonce = attempt_id, scope, nonce
        self._state, self._owner = "FRESH", None
        self._lock = Lock()

    @property
    def attempt_id(self):
        return self._attempt_id

    @property
    def nonce(self):
        return self._nonce

    def claim(self, owner, identity):
        scope = (identity.symbol, identity.account, identity.accounts, identity.opportunity, identity.token)
        with self._lock:
            if self._state != "FRESH" or self._scope != scope:
                raise ValueError("fresh_journal_capability_consumed_or_mismatched")
            self._state, self._owner = "BOUND", owner

    def assert_owner(self, owner, phase):
        with self._lock:
            if self._owner is not owner or self._state != phase:
                raise ValueError("fresh_journal_capability_owner_UNKNOWN")

    def transition(self, owner, phase):
        with self._lock:
            if self._owner is not owner or self._state != "BOUND":
                raise ValueError("fresh_journal_capability_owner_UNKNOWN")
            if phase not in {"REFUSED", "XADD_INVOKED"}:
                raise ValueError("fresh_journal_capability_transition_UNKNOWN")
            self._state = phase

    def __copy__(self):
        return self

    def __deepcopy__(self, memo):
        return self

    def __reduce__(self):
        raise TypeError("fresh_journal_capability_cannot_be_serialized_or_restored")


def write_fresh_attempt(session_factory, payload, *, issue_authority=True):
    """Only a newly inserted/committed/read-back attempt issues one capability."""
    from sqlalchemy import select, text
    from project_mai_tai.brokerdecides1_retirement import parse_attempt
    from project_mai_tai.db.models import DashboardSnapshot

    payload = deepcopy(payload)
    names, token = payload.get("account_names"), payload.get("attempt_token")
    if not isinstance(token, str) or not token or len(token) > 128:
        raise ValueError("fresh_journal_token_UNKNOWN")
    if issue_authority is True and (set(payload) != {"schema_version", "strategy_code", "symbol", "opportunity_id",
                        "account_names", "kind", "attempt_token", "account_name"}
            or payload.get("schema_version") != 1 or not isinstance(names, list)
            or len(names) != 2 or any(not isinstance(n, str) or not n or len(n) > 64 for n in names)
            or len(set(names)) != 2):
        raise ValueError("fresh_journal_scope_UNKNOWN")
    row = DashboardSnapshot(snapshot_type="v2_wait_dispatch", payload=payload)
    if issue_authority is True:
        symbol, token, account, opportunity = parse_attempt(row, names)
        if (symbol != symbol.upper() or len(symbol) > 32
                or payload["opportunity_id"] != str(opportunity)):
            raise ValueError("fresh_journal_scope_UNKNOWN")
    # Across new writers, the SAME TOKEN has the SAME primary key. The lookup
    # also rejects legacy journals with random IDs, including old UNKNOWN rows.
    identity = uuid5(NAMESPACE_URL, "project-mai-tai/prewire-attempt/v1/" + token)
    nonce = uuid4().hex
    row.id = identity
    if issue_authority is True:
        row.payload = {**payload, "prewire_authority_version": 1, "prewire_authority_nonce": nonce}
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
            # Every current RemovedWaitStore attempt writer, including flag-OFF
            # callers, uses this same lock + deterministic primary key. The lock
            # serializes the legacy/noncanonical-ID lookup with new insertion.
            session.execute(text("SELECT pg_advisory_xact_lock(hashtext('l12-dispatch-token'), hashtext(:token))"),
                            {"token": token})
        old = session.scalar(select(DashboardSnapshot.id).where(
            DashboardSnapshot.snapshot_type == "v2_wait_dispatch",
            DashboardSnapshot.payload["attempt_token"].as_string() == token).limit(1))
        if old is not None:
            raise ValueError("fresh_journal_token_already_exists")
        session.add(row)
        expected = deepcopy(row.payload)
        session.commit()  # Lost commit reply raises: no capability is returned.
    if issue_authority is not True:
        return None
    with session_factory() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        observed = session.get(DashboardSnapshot, identity)
        if observed is None or observed.snapshot_type != "v2_wait_dispatch" or observed.payload != expected:
            raise ValueError("fresh_journal_readback_UNKNOWN")
    return FreshJournalCapability(_ISSUER, identity,
        (symbol, account, tuple(names), str(opportunity), token), nonce)

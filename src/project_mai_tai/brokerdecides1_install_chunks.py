"""Source-only install chunk API; caller owns stopped-writer proof and commits.

No runtime use, row deletion, schema change or boot permission is introduced.
An install coverage count is deliberately NOT a late-event invalidation fence.
"""

from dataclasses import dataclass
from datetime import UTC
from uuid import UUID

from sqlalchemy import func, select, text

from project_mai_tai.brokerdecides1_accounts import local_accounts
from project_mai_tai.brokerdecides1_retirement import (
    BATCH, _retire_batch, parse_attempt, scope, valid_retirement,
)
from project_mai_tai.db.models import (
    BrokerDecidesDispatchRetirement as Retirement, DashboardSnapshot,
)


@dataclass(frozen=True)
class InstallChunk:
    upper_id: UUID
    after_id: UUID | None
    next_id: UUID | None
    visited: int
    new_retirements: int
    verified: tuple[UUID, ...]
    unknown: tuple[UUID, ...]
    exhausted: bool


@dataclass(frozen=True)
class UnknownFence:
    identity: UUID
    symbol: str
    age_ms: int | None


@dataclass(frozen=True)
class InstallBootReport:
    retireable_remaining: int
    verified_count: int
    unknown: tuple[UnknownFence, ...]

    @property
    def install_complete(self):
        # This is install coverage, NOT an admission/native-flat grant.
        return self.retireable_remaining == 0


def validate_bounds(after_id, upper_id):
    if (type(upper_id) is not UUID or
            (after_id is not None and (type(after_id) is not UUID or after_id.int > upper_id.int))):
        raise ValueError("install_key_bounds_UNKNOWN")


def read_install_chunk(session, names, *, after_id, upper_id, observed_ms):
    """One <=32-source-row transaction, before caller commit/journal advancement.

    All canonical closure checks and750ms statements stay intact. Existing
    receipts are revalidated per exact PK for accounting, never refreshed. A
    changed/missing proof remains UNKNOWN even if no new receipt is written.
    """
    validate_bounds(after_id, upper_id)
    if type(observed_ms) is not int or observed_ms <= 0:
        raise ValueError("install_observation_UNKNOWN")
    cursor, retired, visited = _retire_batch(session, names, None, observed_ms,
        install_bounds=(after_id, upper_id))
    if not visited:
        return InstallChunk(upper_id, after_id, after_id, 0, 0, (), (), True)
    next_id = cursor[1]
    scope_names = tuple(account.name for account in local_accounts(session, names))
    query = select(DashboardSnapshot.id).where(scope(scope_names), DashboardSnapshot.id <= next_id)
    if after_id is not None:
        query = query.where(DashboardSnapshot.id > after_id)
    identities = tuple(session.scalars(query.order_by(DashboardSnapshot.id).limit(BATCH)))
    if len(identities) != visited or identities[-1] != next_id:
        raise ValueError("install_chunk_identity_UNKNOWN")
    verified, unknown = [], []
    for identity in identities:
        proven = session.scalar(select(valid_retirement()).select_from(DashboardSnapshot)
            .where(DashboardSnapshot.id == identity))
        (verified if proven is True else unknown).append(identity)
    return InstallChunk(upper_id, after_id, next_id, visited, retired,
        tuple(verified), tuple(unknown), False)


def run_committed_chunk(session_factory, names, *, after_id, upper_id, observed_ms,
                        require_stopped_writers, journal_committed):
    """Injectable install runner: no service/DB target/clock constructed here.

    Caller supplies a pure held-proof validator, no network/service call inside
    this transaction, for reviewed stopped-writer/epoch authority. Any exception
    propagates; no retry or cursor advance on an unknown commit. Re-running a
    committed chunk is safe: canonical immutable receipts are not rewritten.
    """
    if require_stopped_writers() is not True:
        raise ValueError("install_writers_not_proven_stopped")
    with session_factory() as session:
        result = read_install_chunk(session, names, after_id=after_id,
            upper_id=upper_id, observed_ms=observed_ms)
        if require_stopped_writers() is not True:
            raise ValueError("install_writers_changed_before_commit")
        session.commit()
    # Callback receives UNKNOWN separately; no empty-book/zero-budget inference.
    journal_committed(result)
    return result


def unprojected_install_count(session, eligible_ids):
    """Indexed structural count of EXPLICIT reviewed eligible PKs, <=500.

    Not a bot boot grant: eligibility, stopped-writer coverage and later source
    mutation invalidation require separate reviewed policy. Never use this to
    assert the retained UNKNOWN is retired or that current broker books are flat.
    """
    identities = tuple(eligible_ids)
    if (not identities or len(identities) > 500 or len(set(identities)) != len(identities)
            or any(type(identity) is not UUID for identity in identities)):
        raise ValueError("explicit_install_eligible_ids_UNKNOWN")
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SET LOCAL statement_timeout = '750ms'"))
        session.execute(text("SET LOCAL lock_timeout = '250ms'"))
    present = session.scalar(select(func.count()).select_from(Retirement)
        .where(Retirement.attempt_id.in_(identities)))
    return len(identities) - present


def committed_install_partition(chunks, *, require_complete=True):
    """Validate a complete committed PK traversal; no UNKNOWN omission allowed."""
    chunks = tuple(chunks)
    if not chunks or any(type(chunk) is not InstallChunk for chunk in chunks):
        raise ValueError("committed_install_chunks_UNKNOWN")
    upper, cursor, seen = chunks[0].upper_id, None, set()
    for index, chunk in enumerate(chunks):
        validate_bounds(chunk.after_id, chunk.upper_id)
        if (chunk.upper_id != upper or chunk.after_id != cursor
                or type(chunk.visited) is not int or not 0 <= chunk.visited <= BATCH
                or type(chunk.new_retirements) is not int
                or not 0 <= chunk.new_retirements <= len(chunk.verified)
                or type(chunk.exhausted) is not bool
                or (chunk.exhausted and index != len(chunks) - 1)):
            raise ValueError("committed_install_chain_UNKNOWN")
        identities = (*chunk.verified, *chunk.unknown)
        if (len(identities) != chunk.visited or len(set(identities)) != len(identities)
                or any(type(identity) is not UUID or identity in seen
                    or identity.int > upper.int or (cursor is not None and identity.int <= cursor.int)
                    for identity in identities)):
            raise ValueError("committed_install_partition_UNKNOWN")
        if chunk.exhausted:
            if chunk.visited or chunk.new_retirements or chunk.next_id != cursor:
                raise ValueError("committed_install_end_UNKNOWN")
        elif (not identities or type(chunk.next_id) is not UUID
                or chunk.next_id != max(identities, key=lambda identity: identity.int)):
            raise ValueError("committed_install_cursor_UNKNOWN")
        seen.update(identities)
        cursor = chunk.next_id
    if require_complete and not chunks[-1].exhausted:
        raise ValueError("committed_install_not_complete")
    return chunks


def resume_install(session_factory, names, *, upper_id, committed_chunks, chunk_budget,
                   observe_ms, require_stopped_writers, journal_committed):
    """Finite install run; external immutable committed journal is resume input.

    Budget exhaustion is unfinished work, not boot-ready. The caller supplies
    exact approved head/scope/epoch binding through held-proof authority.
    """
    validate_bounds(None, upper_id)
    if type(chunk_budget) is not int or not 1 <= chunk_budget <= 500:
        raise ValueError("install_chunk_budget_UNKNOWN")
    prior = tuple(committed_chunks)
    if prior:
        committed_install_partition(prior, require_complete=False)
        if prior[0].upper_id != upper_id:
            raise ValueError("install_resume_upper_changed")
        if prior[-1].exhausted:
            return prior
    after_id = prior[-1].next_id if prior else None
    result = list(prior)
    for _ in range(chunk_budget):
        chunk = run_committed_chunk(session_factory, names, after_id=after_id,
            upper_id=upper_id, observed_ms=observe_ms(),
            require_stopped_writers=require_stopped_writers, journal_committed=journal_committed)
        result.append(chunk)
        if chunk.exhausted:
            break
        after_id = chunk.next_id
    return tuple(result)


def verify_install_boot(session, names, committed_chunks, *, observed_ms, report_unknown):
    """Indexed boot coverage only; current source invalidation remains runtime.

    positively-closed install IDs need receipts; UNKNOWN IDs need own-symbol
    fences, not receipts. No global valid_retirement predicate executes here.
    A boot report cannot make AdmissionMemory or native evidence readable.
    """
    if type(observed_ms) is not int or observed_ms <= 0:
        raise ValueError("boot_observation_UNKNOWN")
    chunks = committed_install_partition(committed_chunks)
    scope_names = tuple(account.name for account in local_accounts(session, names))
    remaining, verified, unknown = 0, 0, []
    for chunk in chunks:
        if chunk.verified:
            remaining += unprojected_install_count(session, chunk.verified)
            verified += len(chunk.verified)
        if not chunk.unknown:
            continue
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        rows = session.scalars(select(DashboardSnapshot)
            .where(DashboardSnapshot.id.in_(chunk.unknown)).limit(BATCH)).all()
        if {row.id for row in rows} != set(chunk.unknown):
            raise ValueError("boot_unknown_source_missing")
        for row in rows:
            symbol, _, _, _ = parse_attempt(row, scope_names)
            stamp = row.created_at
            if stamp is not None:
                stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
                created_ms = int(stamp.timestamp() * 1000)
                age = observed_ms - created_ms if 0 < created_ms <= observed_ms else None
            else:
                age = None
            fence = UnknownFence(row.id, symbol, age)
            unknown.append(fence)
            report_unknown(fence)
    return InstallBootReport(remaining, verified, tuple(unknown))


def finalize_install_before_start(session_factory, names, committed_chunks, *, observed_ms,
                                  require_stopped_writers, report_unknown):
    """Install runbook boundary: read-only boot count before any bot start.

    Caller must bind the approved journal/head/epoch/DB and held writer proof.
    No start/service operation or native/admission permission is performed here.
    """
    if require_stopped_writers() is not True:
        raise ValueError("boot_writers_not_proven_stopped")
    with session_factory() as session:
        if session.in_transaction():
            raise ValueError("boot_requires_fresh_readonly_session")
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION READ ONLY"))
            session.execute(text("SET LOCAL statement_timeout = '750ms'"))
            session.execute(text("SET LOCAL lock_timeout = '250ms'"))
        report = verify_install_boot(session, names, committed_chunks,
            observed_ms=observed_ms, report_unknown=report_unknown)
        if not report.install_complete:
            raise ValueError("boot_retireable_old_notes_remain")
        if require_stopped_writers() is not True:
            raise ValueError("boot_writer_epoch_changed")
        session.rollback()
    return report

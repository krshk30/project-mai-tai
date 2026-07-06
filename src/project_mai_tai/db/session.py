from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.settings import Settings


@lru_cache
def build_engine(
    database_url: str,
    *,
    connect_timeout_s: int | None = None,
    statement_timeout_ms: int | None = None,
    lock_timeout_ms: int | None = None,
    pool_timeout_s: int | None = None,
    pool_recycle_s: int | None = None,
):
    """Shared SQLAlchemy engine (lru-cached per distinct arg set).

    The timeout kwargs default to ``None`` → the historical
    ``create_engine(url, pool_pre_ping=True)`` behavior is byte-identical for
    every existing caller and cache entry. When supplied (the OMS, via
    :func:`build_oms_session_factory`) they bound EVERY DB call so a stalled
    connection RAISES within seconds instead of hanging the asyncio event loop
    forever — the cure for the 2026-07-01/02 OMS zombie (a synchronous
    ``session.flush()`` in ``sync_account_positions`` hung on ``psycopg wait``
    with no timeout and froze the whole loop)."""
    engine_kwargs: dict = {"pool_pre_ping": True}
    connect_args: dict = {}
    if connect_timeout_s is not None:
        connect_args["connect_timeout"] = int(connect_timeout_s)
    options: list[str] = []
    if statement_timeout_ms is not None:
        options.append(f"-c statement_timeout={int(statement_timeout_ms)}")
    if lock_timeout_ms is not None:
        options.append(f"-c lock_timeout={int(lock_timeout_ms)}")
    if options:
        # libpq `options` connection parameter (psycopg3) — applies the GUCs to
        # every session opened on this engine, per statement / per lock wait.
        connect_args["options"] = " ".join(options)
    if connect_args:
        engine_kwargs["connect_args"] = connect_args
    if pool_timeout_s is not None:
        engine_kwargs["pool_timeout"] = int(pool_timeout_s)
    if pool_recycle_s is not None:
        engine_kwargs["pool_recycle"] = int(pool_recycle_s)
    return create_engine(database_url, **engine_kwargs)


def build_session_factory(settings: Settings) -> sessionmaker[Session]:
    engine = build_engine(settings.database_url)
    return sessionmaker(bind=engine, expire_on_commit=False)


def build_oms_session_factory(settings: Settings) -> sessionmaker[Session]:
    """OMS-scoped session factory with DB timeouts applied (SPOF hardening).

    Blast radius is deliberately OMS-only: other services keep the untimed
    :func:`build_session_factory` because their legitimate slow queries
    (reconciler scans, warmup backfills) must not be cut off at the OMS's
    aggressive ~5s bound. Fleet-wide rollout with per-service values is a tracked
    fast-follow. Set ``MAI_TAI_OMS_DB_TIMEOUTS_ENABLED=false`` to fall back to the
    untimed engine (rollback lever)."""
    if not settings.oms_db_timeouts_enabled:
        return build_session_factory(settings)
    engine = build_engine(
        settings.database_url,
        connect_timeout_s=settings.oms_db_connect_timeout_s,
        statement_timeout_ms=settings.oms_db_statement_timeout_ms,
        lock_timeout_ms=settings.oms_db_lock_timeout_ms,
        pool_timeout_s=settings.oms_db_pool_timeout_s,
        pool_recycle_s=settings.oms_db_pool_recycle_s,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)

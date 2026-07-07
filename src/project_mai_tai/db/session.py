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


def build_timed_session_factory(
    settings: Settings, *, service: str, profile: str = "fast"
) -> sessionmaker[Session]:
    """PR-E: fleet-wide DB-timeout rollout for the NON-OMS services.

    Rolls #391's timeout treatment (Postgres ``statement_timeout``/``lock_timeout`` +
    connect/pool timeouts) to every long-running service so a stalled DB connection RAISES
    within seconds instead of hanging the service unbounded (the same latent class the OMS
    had). Timeouts ONLY — no off-loop/executor restructuring (that was OMS-specific, closed
    at Option C).

    ``profile``: ``"fast"`` (~5s) for latency-critical asyncio bots (small indexed queries —
    free their loop quickly); ``"slow"`` (~60s) for services with legitimately long queries
    (reconciler scans, strategy-engine bulk, market-capture inserts, the ~5.4s control
    ``/api/overview``) — generous enough to never cut a legit query, still finite.

    Falls back to the untimed :func:`build_session_factory` (byte-identical) when: the fleet
    flag is off (rollback lever), ``service`` is in the per-service disabled list (per-service
    rollback), or the URL is not Postgres (the timeouts are Postgres GUCs — keeps SQLite test
    paths safe). Never changes ``build_session_factory`` or the OMS factory."""
    disabled = {
        s.strip() for s in str(settings.service_db_timeouts_disabled_services).split(",") if s.strip()
    }
    if (
        not settings.service_db_timeouts_enabled
        or service in disabled
        or not str(settings.database_url).startswith("postgresql")
    ):
        return build_session_factory(settings)
    if profile == "slow":
        statement_timeout_ms = settings.service_db_slow_statement_timeout_ms
        lock_timeout_ms = settings.service_db_slow_lock_timeout_ms
        pool_timeout_s = settings.service_db_slow_pool_timeout_s
    else:  # "fast"
        statement_timeout_ms = settings.service_db_fast_statement_timeout_ms
        lock_timeout_ms = settings.service_db_fast_lock_timeout_ms
        pool_timeout_s = settings.service_db_fast_pool_timeout_s
    engine = build_engine(
        settings.database_url,
        connect_timeout_s=settings.service_db_connect_timeout_s,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        pool_timeout_s=pool_timeout_s,
        pool_recycle_s=settings.service_db_pool_recycle_s,
    )
    return sessionmaker(bind=engine, expire_on_commit=False)

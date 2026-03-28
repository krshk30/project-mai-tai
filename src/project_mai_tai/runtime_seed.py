from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from project_mai_tai.db.session import build_session_factory
from project_mai_tai.oms.store import OmsStore
from project_mai_tai.runtime_registry import (
    configured_broker_account_registrations,
    configured_strategy_registrations,
)
from project_mai_tai.settings import Settings, get_settings


@dataclass(frozen=True)
class RuntimeSeedSummary:
    strategies: int
    broker_accounts: int


def seed_runtime_metadata(
    settings: Settings | None = None,
    *,
    session_factory: sessionmaker[Session] | None = None,
    store: OmsStore | None = None,
) -> RuntimeSeedSummary:
    active_settings = settings or get_settings()
    active_store = store or OmsStore()
    factory = session_factory or build_session_factory(active_settings)

    strategy_registrations = configured_strategy_registrations(active_settings)
    broker_account_registrations = configured_broker_account_registrations(active_settings)

    with factory() as session:
        for broker_account in broker_account_registrations:
            active_store.ensure_broker_account(
                session,
                broker_account.name,
                provider=broker_account.provider,
                environment=broker_account.environment,
                external_account_id=broker_account.external_account_id,
                is_active=broker_account.is_active,
            )
        for strategy in strategy_registrations:
            active_store.ensure_strategy(
                session,
                strategy.code,
                name=strategy.display_name,
                execution_mode=strategy.execution_mode,
                metadata_json=dict(strategy.metadata),
                is_enabled=strategy.is_enabled,
            )
        session.commit()

    return RuntimeSeedSummary(
        strategies=len(strategy_registrations),
        broker_accounts=len(broker_account_registrations),
    )


def run() -> None:
    summary = seed_runtime_metadata()
    print(
        f"Seeded {summary.strategies} strategies and {summary.broker_accounts} broker accounts."
    )

from __future__ import annotations

from dataclasses import dataclass, field

from project_mai_tai.settings import Settings


@dataclass(frozen=True)
class StrategyRegistration:
    code: str
    display_name: str
    account_name: str
    interval_secs: int
    runtime_kind: str
    execution_mode: str
    is_enabled: bool = True
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class BrokerAccountRegistration:
    name: str
    provider: str
    environment: str
    external_account_id: str | None = None
    is_active: bool = True


def configured_strategy_registrations(settings: Settings) -> tuple[StrategyRegistration, ...]:
    execution_mode = "paper" if settings.oms_adapter == "alpaca_paper" else "shadow"
    return (
        StrategyRegistration(
            code="macd_30s",
            display_name="MACD Bot",
            account_name=settings.strategy_macd_30s_account_name,
            interval_secs=30,
            runtime_kind="macd",
            execution_mode=execution_mode,
            metadata={
                "account_name": settings.strategy_macd_30s_account_name,
                "interval_secs": 30,
                "runtime_kind": "macd",
            },
        ),
        StrategyRegistration(
            code="macd_1m",
            display_name="MACD Bot 1M",
            account_name=settings.strategy_macd_1m_account_name,
            interval_secs=60,
            runtime_kind="macd",
            execution_mode=execution_mode,
            metadata={
                "account_name": settings.strategy_macd_1m_account_name,
                "interval_secs": 60,
                "runtime_kind": "macd",
            },
        ),
        StrategyRegistration(
            code="tos",
            display_name="TOS Bot",
            account_name=settings.strategy_tos_account_name,
            interval_secs=60,
            runtime_kind="tos",
            execution_mode=execution_mode,
            metadata={
                "account_name": settings.strategy_tos_account_name,
                "interval_secs": 60,
                "runtime_kind": "tos",
            },
        ),
        StrategyRegistration(
            code="runner",
            display_name="Runner Bot",
            account_name=settings.strategy_runner_account_name,
            interval_secs=300,
            runtime_kind="runner",
            execution_mode=execution_mode,
            metadata={
                "account_name": settings.strategy_runner_account_name,
                "interval_secs": 300,
                "runtime_kind": "runner",
            },
        ),
    )


def strategy_registration_map(settings: Settings) -> dict[str, StrategyRegistration]:
    return {
        registration.code: registration
        for registration in configured_strategy_registrations(settings)
    }


def configured_broker_account_registrations(settings: Settings) -> tuple[BrokerAccountRegistration, ...]:
    registrations: dict[str, BrokerAccountRegistration] = {}
    for strategy in configured_strategy_registrations(settings):
        registrations.setdefault(
            strategy.account_name,
            BrokerAccountRegistration(
                name=strategy.account_name,
                provider=settings.broker_default_provider,
                environment=settings.environment,
            ),
        )
    return tuple(registrations.values())

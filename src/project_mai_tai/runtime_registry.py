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
    registrations: list[StrategyRegistration] = []
    if settings.strategy_macd_30s_enabled:
        registrations.append(
            StrategyRegistration(
                code="macd_30s",
                display_name="MACD Bot",
                account_name=settings.strategy_macd_30s_account_name,
                interval_secs=30,
                runtime_kind="macd",
                execution_mode=settings.execution_mode_for_provider(
                    settings.provider_for_strategy("macd_30s")
                ),
                metadata={
                    "account_name": settings.strategy_macd_30s_account_name,
                    "account_display_name": settings.display_account_name(settings.strategy_macd_30s_account_name),
                    "interval_secs": 30,
                    "runtime_kind": "macd",
                    "provider": settings.provider_for_strategy("macd_30s"),
                },
            )
        )
    if settings.strategy_macd_30s_probe_enabled:
        registrations.append(
            StrategyRegistration(
                code="macd_30s_probe",
                display_name="MACD Bot 30S Probe",
                account_name=settings.strategy_macd_30s_probe_account_name,
                interval_secs=30,
                runtime_kind="macd",
                execution_mode=settings.resolved_execution_mode,
                metadata={
                    "account_name": settings.strategy_macd_30s_probe_account_name,
                    "account_display_name": settings.display_account_name(settings.strategy_macd_30s_probe_account_name),
                    "interval_secs": 30,
                    "runtime_kind": "macd",
                    "provider": settings.resolved_broker_provider,
                },
            )
        )
    if settings.strategy_macd_30s_reclaim_enabled:
        registrations.append(
            StrategyRegistration(
                code="macd_30s_reclaim",
                display_name="MACD Bot 30S Reclaim",
                account_name=settings.strategy_macd_30s_reclaim_account_name,
                interval_secs=30,
                runtime_kind="macd",
                execution_mode=settings.resolved_execution_mode,
                metadata={
                    "account_name": settings.strategy_macd_30s_reclaim_account_name,
                    "account_display_name": settings.display_account_name(settings.strategy_macd_30s_reclaim_account_name),
                    "interval_secs": 30,
                    "runtime_kind": "macd",
                    "provider": settings.resolved_broker_provider,
                },
            )
        )
    if settings.strategy_macd_30s_retest_enabled:
        registrations.append(
            StrategyRegistration(
                code="macd_30s_retest",
                display_name="MACD Bot 30S Retest",
                account_name=settings.strategy_macd_30s_retest_account_name,
                interval_secs=30,
                runtime_kind="macd",
                execution_mode=settings.resolved_execution_mode,
                metadata={
                    "account_name": settings.strategy_macd_30s_retest_account_name,
                    "account_display_name": settings.display_account_name(settings.strategy_macd_30s_retest_account_name),
                    "interval_secs": 30,
                    "runtime_kind": "macd",
                    "provider": settings.resolved_broker_provider,
                },
            )
        )
    registrations.extend(
        [
            StrategyRegistration(
            code="macd_1m",
            display_name="MACD Bot 1M",
            account_name=settings.strategy_macd_1m_account_name,
            interval_secs=60,
            runtime_kind="macd",
            execution_mode=settings.resolved_execution_mode,
            metadata={
                "account_name": settings.strategy_macd_1m_account_name,
                "account_display_name": settings.display_account_name(settings.strategy_macd_1m_account_name),
                "interval_secs": 60,
                "runtime_kind": "macd",
                "provider": settings.resolved_broker_provider,
            },
        ),
        StrategyRegistration(
            code="tos",
            display_name="TOS Bot",
            account_name=settings.strategy_tos_account_name,
            interval_secs=60,
            runtime_kind="tos",
            execution_mode=settings.execution_mode_for_provider(
                settings.provider_for_strategy("tos")
            ),
            metadata={
                "account_name": settings.strategy_tos_account_name,
                "account_display_name": settings.display_account_name(settings.strategy_tos_account_name),
                "interval_secs": 60,
                "runtime_kind": "tos",
                "provider": settings.provider_for_strategy("tos"),
            },
        ),
        StrategyRegistration(
            code="runner",
            display_name="Runner Bot",
            account_name=settings.strategy_runner_account_name,
            interval_secs=300,
            runtime_kind="runner",
            execution_mode=settings.resolved_execution_mode,
            metadata={
                "account_name": settings.strategy_runner_account_name,
                "account_display_name": settings.display_account_name(settings.strategy_runner_account_name),
                "interval_secs": 300,
                "runtime_kind": "runner",
                "provider": settings.resolved_broker_provider,
            },
        ),
        ]
    )
    return tuple(registrations)


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
                provider=settings.provider_for_account(strategy.account_name),
                environment=settings.environment,
            ),
        )
    return tuple(registrations.values())

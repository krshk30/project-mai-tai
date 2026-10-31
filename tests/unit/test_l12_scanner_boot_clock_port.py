"""CONTROLLED existing port + actual anchor method; not full scanner startup."""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from project_mai_tai.services.strategy_engine_app import StrategyEngineService
from tools.l12_service_boot_replay import scoped_existing_scanner_boot_clock


def scanner():
    # No service constructor, provider, store or ready-state mutation. Only the
    # actual anchor method's documented existing clock input is exercised.
    value = object.__new__(StrategyEngineService)
    value.session_factory = object()
    def original():
        return datetime(2026, 10, 24, 16, tzinfo=UTC)
    value.state = SimpleNamespace(alert_engine=SimpleNamespace(now_provider=original))
    return value, original


def test_actual_scanner_anchor_uses_scoped_existing_port_then_restores_original():
    value, original = scanner()
    with scoped_existing_scanner_boot_clock(value, value.session_factory,
            lambda: datetime(2026, 10, 26, 8, tzinfo=UTC)) as receipt:
        assert value._current_strategy_session_start_utc() == datetime(2026, 10, 26, 8, tzinfo=UTC)
        assert receipt["original_port_restored"] is False
        assert receipt["native_headers"] == "UNCHANGED"
    assert value.state.alert_engine.now_provider is original
    assert receipt["original_port_restored"] is True
    assert receipt["permission_granted"] is False


def test_body_failure_preserved_and_clock_restored():
    value, original = scanner()
    failure = RuntimeError("CONTROLLED_primary")
    with pytest.raises(RuntimeError) as observed:
        with scoped_existing_scanner_boot_clock(value, value.session_factory,
                lambda: datetime(2026, 10, 26, 8, tzinfo=UTC)):
            raise failure
    assert observed.value is failure
    assert value.state.alert_engine.now_provider is original


def test_foreign_scanner_refused_before_clock_mutation():
    with pytest.raises(ValueError, match="actual_same_factory_scanner"):
        with scoped_existing_scanner_boot_clock(SimpleNamespace(), object(), lambda: None):
            pytest.fail("foreign clock scope must not enter")


def test_foreign_factory_does_not_get_clock_assignment():
    value, original = scanner()
    with pytest.raises(ValueError, match="actual_same_factory_scanner"):
        with scoped_existing_scanner_boot_clock(value, object(), lambda: None):
            pytest.fail("foreign factory scope must not enter")
    assert value.state.alert_engine.now_provider is original


def test_missing_original_clock_does_not_become_a_fabricated_port():
    value, _ = scanner()
    value.state.alert_engine.now_provider = None
    with pytest.raises(ValueError, match="existing_clock_port_missing"):
        with scoped_existing_scanner_boot_clock(value, value.session_factory, lambda: None):
            pytest.fail("missing original clock scope must not enter")
    assert value.state.alert_engine.now_provider is None

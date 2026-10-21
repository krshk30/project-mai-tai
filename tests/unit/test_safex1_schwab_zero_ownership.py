"""SAFEX1 R4: report zero-own/nonzero-broker without granting a sell."""

import logging

import pytest
from sqlalchemy import select

from project_mai_tai.db.models import SystemIncident
from tests.unit.test_ownmix2_exit_caps import fallback, fallback_case


@pytest.mark.asyncio
@pytest.mark.parametrize("projection", ["virtual", "managed", "both"])
async def test_zero_own_positive_broker_pages_once_without_selling(monkeypatch, caplog, projection):
    case = fallback_case(monkeypatch, owned="0", projection=projection, raw="1000")
    case.service.logger.addHandler(caplog.handler)
    try:
        assert await fallback(case) == []
        assert await fallback(case) == []
        assert not case.broker.sent
        with case.factory() as session:
            incident, = session.scalars(select(SystemIncident)).all()
            assert incident.severity == "critical" and incident.status == "open"
            assert incident.payload["source"] == "schwab_fallback_own_zero_broker_nonzero"
            assert incident.payload["client_order_id"] == case.request.client_order_id
            assert incident.payload["symbol"] == case.request.symbol
            assert incident.payload["broker_quantity"] == "1000"
            assert incident.payload["own_quantity"] == "0" and incident.payload["sell_submitted"] is False
        assert any(record.levelno == logging.CRITICAL and "OWN-ZERO-BROKER-NONZERO" in record.getMessage()
                   for record in caplog.records)
    finally:
        case.service.logger.removeHandler(caplog.handler)
        case.factory.kw["bind"].dispose()


@pytest.mark.asyncio
async def test_zero_broker_does_not_report_zero_own_positive_broker(monkeypatch):
    case = fallback_case(monkeypatch, owned="0", raw="0")
    try:
        assert await fallback(case) == []
        assert not case.broker.sent
        with case.factory() as session:
            assert session.scalar(select(SystemIncident)) is None
    finally:
        case.factory.kw["bind"].dispose()


@pytest.mark.asyncio
async def test_zero_own_page_write_failure_still_does_not_sell(monkeypatch, caplog):
    case = fallback_case(monkeypatch, owned="0", raw="1000")
    session_type = case.factory.class_
    original_add = session_type.add
    attempts = []

    def refuse_incident(session, instance, *args, **kwargs):
        if isinstance(instance, SystemIncident):
            attempts.append(instance.payload["source"])
            raise RuntimeError("CONTROLLED SAFEX1 incident writer unavailable")
        return original_add(session, instance, *args, **kwargs)

    monkeypatch.setattr(session_type, "add", refuse_incident)
    case.service.logger.addHandler(caplog.handler)
    try:
        assert await fallback(case) == []
        assert not case.broker.sent
        assert attempts == ["schwab_fallback_own_zero_broker_nonzero"]
        assert any("OWN-ZERO-PAGE-UNKNOWN" in record.getMessage() for record in caplog.records)
        with case.factory() as session:
            assert session.scalar(select(SystemIncident)) is None
    finally:
        case.service.logger.removeHandler(caplog.handler)
        case.factory.kw["bind"].dispose()

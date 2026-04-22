from __future__ import annotations

from project_mai_tai.services.tradingview_alerts_app import run as run_app


SERVICE_NAME = "tradingview-alerts"


def run() -> None:
    run_app()

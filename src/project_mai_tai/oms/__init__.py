"""OMS service and persistence helpers."""

from __future__ import annotations

from typing import Any

__all__ = ["OmsRiskService", "OmsStore"]


def __getattr__(name: str) -> Any:
    if name == "OmsRiskService":
        from project_mai_tai.oms.service import OmsRiskService

        return OmsRiskService
    if name == "OmsStore":
        from project_mai_tai.oms.store import OmsStore

        return OmsStore
    raise AttributeError(name)

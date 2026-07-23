"""The Phase-1 Webull OCO harness (scripts/webull_oco_step1.py) must mint a UNIQUE client_order_id
per run: Webull rejects a reused coid with TRADE_PLACE_ORDER_REPEAT (417). The prior coid uniquified
on time-of-day ONLY (`ocostep1-HHMMSS`), so it collided across days (same HH:MM:SS) and on same-second
retries -- the exact 07-13 failure that left a stale failed harness unit."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "webull_oco_step1",
    Path(__file__).resolve().parents[2] / "scripts" / "webull_oco_step1.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)


def test_coid_is_unique_per_call() -> None:
    """The random suffix defeats same-second / same-time-of-day collisions (the 417 REPEAT bug)."""
    coids = {_mod._make_coid() for _ in range(50)}
    assert len(coids) == 50


def test_coid_and_suffixed_ids_fit_webull_40_char_limit() -> None:
    """coid + the `-combo` combo id + the `M` master id must ALL stay <= 40 chars (Webull cap)."""
    coid = _mod._make_coid()
    assert coid.startswith("ocostep1-")
    assert len(coid) <= 40
    assert len(f"{coid}-combo") <= 40      # combo_id
    assert len(f"{coid}M") <= 40           # master_coid

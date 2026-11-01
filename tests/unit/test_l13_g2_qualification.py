"""Explicit main-only P1/P3 claims, never substituted by utility-unit results."""
from __future__ import annotations

import multiprocessing
import os

import pytest

from tools.l13_g2_controls import (
    assert_p1, exercise_p1, exercise_p3, load_adapter, write_fresh_result,
)


@pytest.mark.timing
@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("L13_G2_CLAIM") != "P1", reason="main must claim exact P1")
async def test_p1_real_worker_cpu_decode_and_trade_publication_60_seconds():
    result = {"gate": "L13-P1", "gate_status": "FAILED_OR_INCOMPLETE"}
    try:
        adapter = load_adapter(os.environ["L13_G2_ADAPTER"])
        await exercise_p1(adapter, multiprocessing.get_context("spawn"), result)
        assert_p1(result)
        result["gate_status"] = "PASSED"
    finally:
        write_fresh_result(os.environ["L13_G2_RESULT"], result)


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("L13_G2_CLAIM") != "P3", reason="main must claim exact P3")
async def test_p3_recorded_batches_real_strategy_consumer_equivalence():
    result = {"gate": "L13-P3", "gate_status": "FAILED_OR_INCOMPLETE"}
    try:
        adapter = load_adapter(os.environ["L13_G2_ADAPTER"])
        await exercise_p3(adapter, os.environ["L13_G2_RECORDED_MANIFEST"], result)
        result["gate_status"] = "PASSED"
    finally:
        write_fresh_result(os.environ["L13_G2_RESULT"], result)

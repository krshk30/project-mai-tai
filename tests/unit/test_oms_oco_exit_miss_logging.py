"""Pin the OCO-exit-poll MISS instrumentation (log-only, 2026-07-31).

⭐⭐ WHY. `_poll_native_oco_exits` logged on SUCCESS and on fetch-FAILURE, but said nothing when it
polled and the broker reported no filled exit leg. That silence is the reason the 07-31 AXTU/AXTX
misses could not be root-caused: from outside the process, "polled and found nothing" was
indistinguishable from "never polled at all".

Live that day: Schwab AXTU and AXTX OCO exits sat unrecorded for 26-90 minutes while FCUV's recorded
correctly every time. The managed rows stayed open, blocked fan-out re-entry via
`fanout_webull_collision_managed`, and were cleared only by an OMS restart -- which found them in
~2 seconds. Two of three AXTU round trips have no exit record at all.

⛔ Three theories were tested against the data and all three FAILED: "the poll never fires"
(disproved -- it recorded FCUV four times), "a cancelled buy shadows the entry lookup" (disproved --
FCUV had MORE cancelled buys and worked), and throttle starvation (ruled out -- 30s/symbol on a 15s
sync). The cause is still unknown, which is precisely why the next occurrence must announce itself.

These tests pin the instrumentation's CONTRACT: it logs the first miss, it does not spam, and it
never changes behaviour or raises.
"""
from __future__ import annotations

import asyncio
import logging

from project_mai_tai.oms.service import OmsRiskService


def _svc(caplog_logger: logging.Logger) -> OmsRiskService:
    svc = OmsRiskService.__new__(OmsRiskService)
    svc.logger = caplog_logger
    svc._oco_exit_miss_log_at = {}
    # `_run_db` is the only collaborator the helper touches; stub it so the test is hermetic.
    async def _run_db(fn, *, commit=True):
        raise RuntimeError("no DB in this test")
    svc._run_db = _run_db
    return svc


def _miss(svc, acct="live:schwab_1m_v2", symbol="AXTU", base_coid="coid-1", reason="broker_reported_no_filled_exit_leg"):
    return asyncio.run(
        svc._log_oco_exit_miss(acct, symbol, base_coid=base_coid, reason=reason)
    )


def test_first_miss_is_logged(caplog) -> None:
    """The blind spot closes: a miss must produce exactly one visible line."""
    logger = logging.getLogger("test.oco.miss.first")
    svc = _svc(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        _miss(svc)
    lines = [r.getMessage() for r in caplog.records if "OMS-OCO-EXIT-MISS" in r.getMessage()]
    assert len(lines) == 1
    assert "AXTU" in lines[0]
    assert "broker_reported_no_filled_exit_leg" in lines[0]
    assert "managed_row_age" in lines[0]      # ⭐ the discriminator: old row = the defect


def test_repeat_miss_is_suppressed(caplog) -> None:
    """⛔ ANTI-SPAM. The poll runs per symbol every ~30s. Logging every miss would add thousands of
    lines a session to a box that already had a 559 MB log problem."""
    logger = logging.getLogger("test.oco.miss.repeat")
    svc = _svc(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        for _ in range(10):
            _miss(svc)
    lines = [r for r in caplog.records if "OMS-OCO-EXIT-MISS" in r.getMessage()]
    assert len(lines) == 1, f"expected 1 line, got {len(lines)} — anti-spam is not holding"


def test_a_new_entry_order_logs_again(caplog) -> None:
    """State that MATTERS must re-log: a different entry order is a different miss, not a repeat."""
    logger = logging.getLogger("test.oco.miss.newcoid")
    svc = _svc(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        _miss(svc, base_coid="coid-1")
        _miss(svc, base_coid="coid-1")     # suppressed
        _miss(svc, base_coid="coid-2")     # new entry order -> logs
    lines = [r for r in caplog.records if "OMS-OCO-EXIT-MISS" in r.getMessage()]
    assert len(lines) == 2


def test_separate_symbols_do_not_suppress_each_other(caplog) -> None:
    """AXTU missing must not hide AXTX missing — the 07-31 case was two symbols at once."""
    logger = logging.getLogger("test.oco.miss.symbols")
    svc = _svc(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        _miss(svc, symbol="AXTU")
        _miss(svc, symbol="AXTX")
    lines = [r for r in caplog.records if "OMS-OCO-EXIT-MISS" in r.getMessage()]
    assert len(lines) == 2


def test_entry_lookup_raised_is_its_own_reason(caplog) -> None:
    """MISS-1 was silent too. It must be distinguishable from MISS-3 in the log."""
    logger = logging.getLogger("test.oco.miss.raised")
    svc = _svc(logger)
    with caplog.at_level(logging.INFO, logger=logger.name):
        _miss(svc, base_coid="", reason="entry_lookup_raised")
    lines = [r.getMessage() for r in caplog.records if "OMS-OCO-EXIT-MISS" in r.getMessage()]
    assert len(lines) == 1
    assert "entry_lookup_raised" in lines[0]


def test_helper_never_raises_even_when_everything_fails() -> None:
    """⛔ Diagnostics must NEVER break the sync that protects the account. A broken logger, a dead
    DB, and missing state must all degrade to silence, not to an exception."""
    svc = OmsRiskService.__new__(OmsRiskService)

    class _Boom:
        def info(self, *a, **k):
            raise RuntimeError("logger exploded")

    svc.logger = _Boom()
    async def _run_db(fn, *, commit=True):
        raise RuntimeError("db down")
    svc._run_db = _run_db
    # no _oco_exit_miss_log_at attribute at all -> the helper must still cope
    asyncio.run(svc._log_oco_exit_miss("acct", "SYM", base_coid="c", reason="r"))

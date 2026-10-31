"""Finite source-directed timing trace; not a rate qualification or native run."""
import asyncio
import gc
import json
import os
import threading
from time import monotonic, thread_time
from types import SimpleNamespace

import pytest
from sqlalchemy import event

from project_mai_tai import brokerdecides1_set_proof as proof
from project_mai_tai.brokerdecides1_sync import local_evidence
from project_mai_tai.services.schwab_1m_v2_bot import SchwabV2BotService
from project_mai_tai.settings import Settings
from tests.unit.wbpa3_episode_port import ACCOUNT_NAMES, OPPORTUNITY, seed_episode

pytestmark = pytest.mark.skipif(not os.environ.get("WBPA3_SOURCE_DIRECTORY"),
    reason="Requires exact private retained CHOW inputs and C1 source-diagnosis authority")


@pytest.mark.asyncio
async def test_ten_fresh_reader_scans_trace_sql_build_gc_threads_and_loop(tmp_path, monkeypatch, record_property):
    case = seed_episode(tmp_path)
    settings = Settings(_env_file=None, strategy_schwab_1m_v2_country_route_enabled=True,
        strategy_schwab_1m_v2_account_name=ACCOUNT_NAMES[0],
        strategy_schwab_1m_v2_webull_account_name=ACCOUNT_NAMES[1])
    bot = SimpleNamespace(settings=settings)
    loop_thread = threading.get_ident()
    scans, builds, collections, sql, pulses = [], [], [], [], []
    gc_starts, query_starts, cursor_starts = {}, {}, {}
    gc_before = (gc.isenabled(), gc.get_threshold())

    def collection(phase, info):
        key = (threading.get_ident(), info["generation"])
        if phase == "start":
            gc_starts[key] = monotonic()
        elif key in gc_starts:
            begin = gc_starts.pop(key)
            collections.append({"start": begin, "end": monotonic(), "thread": key[0],
                "generation": key[1], "collected": info.get("collected")})

    original = proof.validated_retirement_ids

    def observed_build():
        begin, cpu = monotonic(), thread_time()
        try:
            return original()  # Exact SQL graph; no result/gate substitution.
        finally:
            builds.append({"start": begin, "end": monotonic(), "cpu": thread_time()-cpu,
                "thread": threading.get_ident()})

    def before_execute(conn, clause, multiparams, params, options):
        query_starts[threading.get_ident()] = monotonic()

    def before_cursor(conn, cursor, statement, parameters, context, executemany):
        tid = threading.get_ident()
        cursor_starts[tid] = (query_starts.get(tid), monotonic(), str(context.cache_hit), len(statement))

    def after_cursor(conn, cursor, statement, parameters, context, executemany):
        tid = threading.get_ident()
        begin, cursor_at, cache, length = cursor_starts.pop(tid)
        sql.append({"start": begin, "cursor_at": cursor_at, "end": monotonic(),
            "thread": tid, "cache": cache, "sql_chars": length})

    monkeypatch.setattr(proof, "validated_retirement_ids", observed_build)
    gc.callbacks.append(collection)
    event.listen(case.engine, "before_execute", before_execute)
    event.listen(case.engine, "before_cursor_execute", before_cursor)
    event.listen(case.engine, "after_cursor_execute", after_cursor)

    def scan():
        begin, cpu, tid = monotonic(), thread_time(), threading.get_ident()
        with case.factory() as session:
            local_at = monotonic()
            inflight, fills, tokens = local_evidence(session, ACCOUNT_NAMES)
            local_end = monotonic()
            terminal, evaluated = SchwabV2BotService._terminal_unfilled_first_rest_opportunities(
                bot, session, account_names=ACCOUNT_NAMES, unknown_opportunities={"CHOW": OPPORTUNITY})
            terminal_end = monotonic()
        assert not inflight and not fills and len(tokens) == 4
        assert terminal == {"CHOW": frozenset({OPPORTUNITY})} and evaluated == 1
        assert tid != loop_thread
        scans.append({"start": begin, "end": monotonic(), "thread": tid,
            "cpu": thread_time()-cpu, "local_start": local_at, "local_end": local_end,
            "terminal_end": terminal_end})

    async def actor():
        for _ in range(10):
            await asyncio.to_thread(scan)
            await asyncio.sleep(.1)

    task = asyncio.create_task(actor(), name="WBPA3-finite-reader-diagnostic")
    due = monotonic()
    try:
        while not task.done():
            due += .005
            await asyncio.sleep(max(0, due-monotonic()))
            pulses.append({"due": due, "received": monotonic()})
        task.result()
    finally:
        # Physically join the owned thread before detaching observers/disposing.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
        task.result()
        gc.callbacks.remove(collection)
        event.remove(case.engine, "before_execute", before_execute)
        event.remove(case.engine, "before_cursor_execute", before_cursor)
        event.remove(case.engine, "after_cursor_execute", after_cursor)
        case.engine.dispose()
        evidence = {"loop_thread": loop_thread, "scans": scans, "SQL_graph_builds": builds,
            "natural_gc": collections, "sql_spans": sql, "loop_pulses": pulses,
            "GC_enabled_thresholds_before": gc_before,
            "GC_enabled_thresholds_after": (gc.isenabled(), gc.get_threshold()),
            "scope": "FINITE_DIAGNOSIS_ONLY_NOT_60S_RATE_NATIVE_CURRENT_RUNTIME_OR_PG"}
        print("WBPA3_DIAGNOSTIC_EVIDENCE=" + json.dumps(evidence, sort_keys=True))
        record_property("WBPA3_DIAGNOSTIC_EVIDENCE", json.dumps(evidence, sort_keys=True))
    assert len(scans) == 10
    assert gc_before == (gc.isenabled(), gc.get_threshold())

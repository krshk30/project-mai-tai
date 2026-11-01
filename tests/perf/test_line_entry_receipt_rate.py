"""Actual readiness + publication-log operands, CONTROLLED prerequisites only.

This is a per-bar receipt rate check, not full v2/OMS/native qualification.
"""
import json
import logging
import time
from types import SimpleNamespace as NS

import pytest

from project_mai_tai.services import schwab_1m_v2_bot as service
from tests.unit.test_line_entry_receipt import Controlled, publication_log_statements


@pytest.mark.timing
def test_publication_receipt_240eps_60seconds(record_property):
    formatted = []
    class Format(logging.Handler):
        def emit(self, record):
            text = record.getMessage()
            assert 'first_false_predicate=' in text and ' branch=' in text
            formatted.append(text)
    logger = logging.Logger('CONTROLLED-publication-rate')
    logger.addHandler(Format())
    contexts = []
    for branch, failure in [('ordinary', 'none'), ('current_publication', 'awaiting_sell'),
                            ('line_live_fallback', 'none'), ('ordinary', 'spanning_unclean')]:
        controlled = Controlled(branch, failure)
        ledger = controlled.bot._line_sessions['TNMG']
        ledger.revision = 129
        contexts.append(dict(service.__dict__, self=controlled.bot, symbol='TNMG',
            ledger=ledger, result=NS(request=NS(bars=[1], spanning_pairs=[1])),
            adjacent_live=True, period=5, logger=logger))
    actual_log = publication_log_statements()
    latencies = []
    wake_lateness = []
    counts = [0, 0, 0, 0]
    start = time.perf_counter()
    for index in range(14_401):
        deadline = start + index / 240
        time.sleep(max(0, deadline - time.perf_counter()))
        begin = time.perf_counter()
        wake_lateness.append(max(0, begin - deadline) * 1000)
        selected = index % len(contexts)
        exec(actual_log, contexts[selected])
        latencies.append((time.perf_counter() - begin) * 1000)
        counts[selected] += 1
    elapsed = time.perf_counter() - start
    ordered = sorted(latencies)
    metrics = dict(seconds=elapsed, events=len(latencies), events_per_second=len(latencies)/elapsed,
        callback_p99_ms=ordered[int((len(ordered)-1)*0.99)], callback_max_ms=max(latencies),
        wake_lateness_max_ms=max(wake_lateness), branch_case_counts=counts,
        formatted_logs=len(formatted), scope='CONTROLLED_readiness_and_actual_publication_log_statements_not_full_v2_OMS_native')
    record_property('line_entry_receipt_rate', json.dumps(metrics, sort_keys=True))
    assert elapsed >= 60 and len(latencies) == len(formatted) == 14_401
    assert len(latencies) / elapsed >= 200
    assert max(latencies) < 50 and max(wake_lateness) < 50
    assert all(count >= 3_600 for count in counts)

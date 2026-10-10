"""Private audit INSERT equivalence and failure isolation on PostgreSQL."""

import pytest

import test_cancel_terminal_runtime as runtime
from tests.unit import test_core_report_audit as control

sessions = runtime.sessions


@pytest.mark.parametrize("use_core", [False, True])
@pytest.mark.parametrize("origin", ["broker", "client", "unknown"])
def test_pg_audit_provenance_and_fanout(sessions, use_core, origin):
    control.test_audit_rows_and_fanout_identity_equal(sessions, use_core, origin)


@pytest.mark.parametrize("use_core", [False, True])
def test_pg_nfq_feedback_scope(sessions, use_core):
    control.test_nfq_terminal_report_keeps_only_audit_before_nfq_feedback(sessions, use_core)


def test_pg_audit_pending_order_and_rollback(sessions):
    control.test_core_audit_flushes_pending_order_and_rolls_back(sessions)


@pytest.mark.parametrize("use_core", [False, True])
def test_pg_missing_timestamp_and_origin_defaults(sessions, use_core):
    control.test_missing_timestamp_and_origin_keep_existing_defaults(sessions, use_core)


@pytest.mark.asyncio
async def test_pg_audit_failure_keeps_ledger_and_census(sessions, caplog):
    await control.test_real_core_insert_failure_keeps_ledger_and_counts_drop(sessions, caplog)

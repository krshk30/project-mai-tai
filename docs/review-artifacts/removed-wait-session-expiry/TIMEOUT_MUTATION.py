"""[codex] Semantic controls for bounded empty-history tests and fail-closed request expiry."""
from pathlib import Path
import importlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linesrc1"))
import MUTATIONS as harness  # noqa: E402

STRATEGY = "project_mai_tai.strategy_core.schwab_1m_v2"
TEST = "tests/unit/test_removed_wait_session_expiry.py::"
harness.CASES = {
    "empty_worker_timeout_removed": (
        "tests.unit.test_linesrc1_event_driven",
        "    bot._request_line_repairs(_scanner(bot, {symbol}))\n"
        "    await asyncio.wait_for(bot._line_source_events_pass(), timeout=2.0)\n",
        "    bot._request_line_repairs(_scanner(bot, {symbol}))\n"
        "    await bot._line_source_events_pass()\n",
        "tests/unit/test_linesrc1_event_driven.py::test_recorded_empty_control_times_out_when_queue_refuses",
    ),
    "current_request_date_waived": (STRATEGY,
        "                or not self._removed_wait_is_prior_session(request)):\n",
        "                or False):\n",
        TEST + "test_incomplete_or_failed_expiry_keeps_request_and_ownership[request_current]"),
    "unknown_identity_is_superseded": (STRATEGY,
        "        superseded = bool(state is not None and self._flip_owned_first_entry_enabled\n"
        "                          and current == state.fanout_segment_id and current != request.opportunity_id\n"
        "                          and session_start_ts_ms(now) <= current <= now)\n",
        "        superseded = state is not None\n",
        TEST + "test_recorded_olox_episode_restarts_no_stale_qty_barrier_but_unknown_stays_closed"),
    "future_episode_admitted": (STRATEGY,
        "                          and session_start_ts_ms(now) <= current <= now)\n",
        "                          and current > 0)\n",
        TEST + "test_incomplete_or_failed_expiry_keeps_request_and_ownership[future]"),
    "post_proof_fill_guard_removed": (STRATEGY,
        "                        and (state is None or not (self._removed_wait_has_owner(state) or state.position_qty)))\n",
        "                        )\n",
        TEST + "test_fill_or_position_arriving_after_proof_keeps_prior_episode_owned"),
    "stale_proof_age_waived": (STRATEGY,
        "                        and 0 <= now - proof.observed_at_ms <= FLIP_OWNER_EVIDENCE_MAX_AGE_MS\n",
        "                        and True\n",
        TEST + "test_incomplete_or_failed_expiry_keeps_request_and_ownership[stale_proof]"),
    "unrelated_cancel_token_dropped": (STRATEGY,
        "                        and draft.metadata.get(\"clearwait_removal_token\") == request.token\n",
        "",
        TEST + "test_clock_roll_drops_only_prior_token_barriers_and_keeps_unknown_owner"),
    "stale_restart_cancel_returns": (STRATEGY,
        "        if self._removed_wait_episode_is_prior(request):\n            return\n",
        "",
        TEST + "test_recorded_olox_episode_restarts_no_stale_qty_barrier_but_unknown_stays_closed"),
    "failed_persist_reports_expiry": (STRATEGY,
        "            logger.exception(\"[V2-REMOVED-WAIT] %s verdict=UNKNOWN reason=obsolete_request_write_failed\", request.symbol)\n"
        "            return False\n",
        "            logger.exception(\"[V2-REMOVED-WAIT] %s verdict=UNKNOWN reason=obsolete_request_write_failed\", request.symbol)\n"
        "            return True\n",
        TEST + "test_incomplete_or_failed_expiry_keeps_request_and_ownership[write_failure]"),
}
harness.__file__ = __file__
# Pytest's prepend-mode collection uses the flat alias for this tests directory.
original_configure = harness.Recorder.pytest_configure


def configure(self):
    original_configure(self)
    sys.modules["test_linesrc1_event_driven"] = importlib.import_module("tests.unit.test_linesrc1_event_driven")


harness.Recorder.pytest_configure = configure
if __name__ == "__main__":
    raise SystemExit(harness.main())

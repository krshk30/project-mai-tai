"""Offline assertion mutations; never edits source, services, Redis or broker state."""

import inspect
import textwrap

import pytest

from project_mai_tai.services import control_plane as cp
from project_mai_tai.services import orb_schwab_app as live
from tests.unit import test_orblive1_reporting as checks


def run_mutation(label, owner, name, before, after, exercise):
    original = getattr(owner, name)
    source = textwrap.dedent(inspect.getsource(original))
    if before not in source:
        raise AssertionError(f"mutation target missing: {label}")
    module = inspect.getmodule(original)
    namespace = {}
    exec(compile(source.replace(before, after, 1), f"<mutation:{label}>", "exec"), module.__dict__, namespace)
    with pytest.MonkeyPatch.context() as patch:
        exercise(patch)
    setattr(owner, name, namespace[name])
    try:
        with pytest.MonkeyPatch.context() as patch:
            exercise(patch)
    except (AssertionError, TimeoutError):
        print(f"RED {label}")
    else:
        raise AssertionError(f"SURVIVED {label}")
    finally:
        setattr(owner, name, original)


def main():
    run_mutation(
        "missing-stale-heartbeat-gate", cp, "_build_orb_live_listening_status",
        "elif age is None or not 0 <= age <= 60 or status != \"healthy\":", "elif False:",
        lambda patch: checks.test_missing_or_stale_heartbeat_is_stopped_even_with_ticks_or_held_book(patch, None, False),
    )
    run_mutation(
        "waiting-phase-wrong", cp, "_build_orb_live_listening_status",
        'elif details.get("phase") == "waiting_for_open":\n        state, color = "WAITING FOR 09:27", "#5fff8d"',
        'elif details.get("phase") == "waiting_for_open":\n        state, color = "EVALUATING", "#5fff8d"',
        lambda patch: checks.test_listening_phase_is_live_report_not_inferred_from_clock_or_decisions(patch, "waiting_for_open", "WAITING FOR 09:27"),
    )
    run_mutation(
        "producer-phase-boundary", live.OrbSchwabService, "_live_phase",
        "if now < opening - timedelta(minutes=5):", "if False:",
        lambda patch: checks.test_live_heartbeat_phase_boundaries_do_not_move_trading_window(patch, 13 * 60 + 24, "waiting_for_open"),
    )
    run_mutation(
        "producer-service-identity", live.OrbSchwabService, "_publish_live_heartbeat",
        "service_name=_SERVICE", 'service_name="orb"',
        checks.test_live_heartbeat_wire_owns_only_successfully_announced_symbols_and_no_paper_state,
    )
    run_mutation(
        "fallback-masks-stale-heartbeat", cp, "_orb_unit_fallback_fresh",
        'if service.get("observed_at") or service.get("observed_at_raw"):', "if False:",
        lambda patch: checks.test_unit_fallback_cannot_adopt_bad_evidence_or_mask_stale_heartbeat(patch, "has_heartbeat"),
    )
    run_mutation(
        "date-labels-removed", cp, "_render_bot_detail_page",
        "section_date = (", 'section_date = ""\n    unused_section_date = (',
        checks.test_paused_orb_tab_dates_every_data_section_and_new_day_drops_old_rows,
    )
    run_mutation(
        "heartbeat-blocks-trading-task", live.OrbSchwabService, "run",
        'heartbeat_task = asyncio.create_task(self._live_heartbeat_loop(), name="orb-schwab-heartbeat")',
        'await self._publish_live_heartbeat()\n    heartbeat_task = asyncio.create_task(self._live_heartbeat_loop(), name="orb-schwab-heartbeat")',
        checks.test_real_run_heartbeat_task_cannot_block_bar_or_exit_processing,
    )


if __name__ == "__main__":
    main()

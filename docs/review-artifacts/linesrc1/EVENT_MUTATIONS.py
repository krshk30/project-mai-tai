#!/usr/bin/env python3
"""[codex] New-card semantic controls; old polling mutation receipts are superseded."""
import MUTATIONS as harness

TEST = "tests/unit/test_linesrc1_event_driven.py::"
LEDGER, BOT, CLIENT = harness.LEDGER, harness.BOT, harness.CLIENT
old = harness.CASES
REVIEW = "tests/unit/test_linesrc1_review_regressions.py::"
harness.CASES = {
    "db_completion_before_reconciliation": (BOT,
        "            self._line_reconcile_pending.add(symbol)\n            try:\n",
        "            self._line_reconcile_pending.discard(symbol)\n"
        "            self._line_db_checked.add((symbol, ledger.epoch))\n            try:\n",
        REVIEW + "test_stale_reconciliation_does_not_skip_positive_tape_or_mark_db_complete"),
    "callback_ingest_wakeup_lost": (BOT,
        "        self._wake_line_after_ingest(symbol)\n", "",
        REVIEW + "test_persist_paused_callback_rewakes_after_strategy_ingest_and_delivers_sell_once"),
    "suffix_correction_invalidates_seed": (LEDGER,
        "                if (bar.timestamp_ms <= endpoint\n"
        "                        and (bar.timestamp_ms not in self._coverage.closed_ids or old is not None)):\n",
        "                if ((bar.timestamp_ms <= endpoint\n"
        "                     and bar.timestamp_ms not in self._coverage.closed_ids) or old is not None):\n",
        REVIEW + "test_validated_suffix_correction_preserves_seed_and_never_replays_old_sell"),
    "initial_membership_becomes_readd": (BOT,
        "            if initial:\n                self._line_startup_pending.add(sym)\n",
        "            if initial:\n                self._line_startup_pending.discard(sym)\n"
        "                self._line_readd_pending.add(sym)\n",
        REVIEW + "test_initial_0701_scanner_without_candles_waits_then_first_actual_empty_is_one_fetch"),
    "periodic_full_fetch_returns": (CLIENT, "        cycle = itertools.cycle(symbols)\n",
        "        if self._session_request is not None:\n"
        "            await self._anchored_bar_loop_pass(symbols, interval)\n            return\n"
        "        cycle = itertools.cycle(symbols)\n",
        TEST + "test_rest_incremental_fallback_not_shortcircuited_by_history_hook"),
    "closed_callback_not_recorded": (BOT, "                ledger.observe_closed_live(bar)\n",
        "                ledger.observe(bar)\n",
        TEST + "test_one_real_parser_fetch_then_live_callbacks_advance_immutable_seed_math_and_ready"),
    "unknown_tail_sparsity_certified": (LEDGER,
        "        tail_ids = tuple(range(endpoint + 60_000, self.current_bar_ms + 1, 60_000))\n",
        "        tail_ids = tuple(sorted(ts for ts in self._bars if ts > endpoint))\n",
        TEST + "test_ten_later_closed_bars_cannot_certify_unknown_tail_even_without_tape"),
    "prefix_correction_reversion_releases": (LEDGER,
        "        if self._seed_invalidated:\n            self.incomplete_reason = \"seed_invalidated\"\n            return None\n",
        "", TEST + "test_prefix_changed_then_reverted_still_requires_new_qualifying_event"),
    "provider_value_hash_waived": (LEDGER,
        "        if proof.bars_sha256 != history_fingerprint(prefix):\n",
        "        if False:\n",
        TEST + "test_db_fill_only_repairs_provider_listed_original_values_not_new_tail[value]"),
    "db_tail_becomes_live_provenance": (BOT,
        "            for bar in stored:\n                ledger.observe(bar)\n",
        "            for bar in stored:\n                ledger.observe_closed_live(bar)\n",
        TEST + "test_db_fill_only_repairs_provider_listed_original_values_not_new_tail[db_tail]"),
    "readd_old_permission_rebuilds": (BOT,
        "        if symbol in self._line_readd_needs_live:\n            return False\n        state = self.strategy._symbol_states.get(symbol)\n",
        "        state = self.strategy._symbol_states.get(symbol)\n",
        "tests/unit/test_line_restore_acceptance_factory.py::test_factory_readd_replaces_epoch_without_replaying_old_permission"),
    "stale_endpoint_ready": (BOT,
        "                    and ledger.current_bar_ms == self.strategy._now_ms() // 60_000 * 60_000 - 60_000\n",
        "", TEST + "test_stale_endpoint_does_not_allow_quote_buy_without_new_bar"),
    "publication_is_reconfirmation": (BOT,
        "        reconfirmed = selected - previous\n", "        reconfirmed = set(selected)\n",
        TEST + "test_recorded_empty_event_is_once_waiting_without_callback_retry"),
    "stale_event_identity_waived": (BOT,
        "                    and self._line_source_identity.get(symbol) == identity)\n",
        "                    )\n", TEST + "test_stale_event_readd_response_cannot_attest_new_epoch"),
    "unclosed_callback_consumes_startup": (BOT,
        "and 60_000 <= self.strategy._now_ms() - bar.timestamp_ms <= 180_000)",
        "and 0 <= self.strategy._now_ms() - bar.timestamp_ms <= 180_000)",
        TEST + "test_first_actual_closed_bar_triggers_startup_once_even_quiet_names"),
    "quiet_name_requires_exact_0700": (BOT,
        "and bar.timestamp_ms >= ledger.anchor_ms + 3 * 3_600_000):",
        "and bar.timestamp_ms == ledger.anchor_ms + 3 * 3_600_000):",
        TEST + "test_first_actual_closed_bar_triggers_startup_once_even_quiet_names"),
    "db_whole_session_each_append": (BOT,
        "if self.session_factory is not None and source_proof is not None and reconcile:",
        "if self.session_factory is not None and source_proof is not None:",
        TEST + "test_one_real_parser_fetch_then_live_callbacks_advance_immutable_seed_math_and_ready"),
    "full_replay_each_append": (BOT,
        "build_session_line(request, period, factor, previous=mathematical_previous)",
        "build_session_line(request, period, factor, previous=None)",
        TEST + "test_one_real_parser_fetch_then_live_callbacks_advance_immutable_seed_math_and_ready"),
    "c3_callback_loses_live_close": (BOT,
        "self._observe_line_bar(symbol, bar, source_callback=True)\n        was_warmed",
        "self._observe_line_bar(symbol, bar)\n        was_warmed",
        TEST + "test_one_real_parser_fetch_then_live_callbacks_advance_immutable_seed_math_and_ready[c3]"),
    "held_confirmation_never_expires": (BOT,
        "if not published and ledger.current_bar_ms in self._line_live_bars.get(symbol, set()):",
        "if False:",
        TEST + "test_confirmation_exact_once_on_live_append_and_held_target_expires_unanswerable"),
    "empty_becomes_exception": old["empty_becomes_exception"],
    "empty_manufactures_proof": old["empty_manufactures_proof"],
    "provider_first_close": old["provider_first_close"],
    "duplicate_validation": old["duplicate_validation"],
    "foreign_validation": old["foreign_validation"],
    "epoch_fence": old["epoch_fence"],
    "traded_hole_waived": old["traded_hole_waived"],
    "r6_clean_live_waived": old["r6_clean_live_waived"],
}
# Reuse isolated full-module compilation/reporting, but dispatch children here.
harness.__file__ = __file__
if __name__ == "__main__":
    raise SystemExit(harness.main())

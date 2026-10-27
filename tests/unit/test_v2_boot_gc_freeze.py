"""C10659 source-proposal controls. Main-only execution; no real GC mutation."""

import ast
import hashlib
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from project_mai_tai.services import schwab_1m_v2_bot as service


BASELINE = Path("/private/tmp/oct26-gibbs-c11859-primary-l12-excluded-20261026/src/project_mai_tai/services/schwab_1m_v2_bot.py")
BASELINE_SHA = "37aee964bbfb3999085e97ebf92beb507a42c353783ef965cf8aebf585dc2841"


@pytest.fixture(autouse=True)
def controlled_gc(monkeypatch):
    freeze, count = Mock(), Mock(return_value=123)
    monkeypatch.setattr(service.gc, "freeze", freeze)
    monkeypatch.setattr(service.gc, "get_freeze_count", count)
    monkeypatch.setattr(service, "_BOOT_GC_FREEZE_ATTEMPTED", False)
    monkeypatch.setattr(service.time, "monotonic", lambda: 100.0)
    return freeze, count


def subject(*, restored=True, held=True, dangerous=False, enabled=True,
            keepwatch=True, complete_on_check=False):
    bot = object.__new__(service.SchwabV2BotService)
    segments = ([dict(symbol="LNKS", dangerous=True, entries_this_flip=0, max_entries=2)]
                if dangerous else [])
    bot.strategy = SimpleNamespace(_cw_armed_segment_safety_enabled=enabled,
        _entries_held=held, cw_armed_segments=lambda: segments)
    bot._keepwatch_enabled = keepwatch
    bot._boot_state_restoration_complete = restored
    bot._boot_restore_last_hold_log_at = None
    bot._keepwatch_boot_retry_deferred_reason = None
    bot._keepwatch_boot_retry_deferred_symbol = None
    bot._watchlist = {"LNKS"}
    bot._retry_keepwatch_boot_exclusions = Mock()

    def complete(*args, **kwargs):
        if complete_on_check:
            bot._boot_state_restoration_complete = True

    bot._try_complete_boot_state_restoration = Mock(side_effect=complete)
    return bot


def baseline_check():
    if not BASELINE.exists():
        pytest.skip(f"requires local rig artifact {BASELINE}")
    raw = BASELINE.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == BASELINE_SHA
    tree = ast.parse(raw, filename=str(BASELINE))
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == "SchwabV2BotService")
    method = next(node for node in cls.body
                  if isinstance(node, ast.FunctionDef) and node.name == "_cw_boot_hold_check")
    namespace = dict(time=service.time, logger=service.logger,
        BOOT_RESTORE_HOLD_LOG_INTERVAL_SECONDS=service.BOOT_RESTORE_HOLD_LOG_INTERVAL_SECONDS)
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(BASELINE), "exec"), namespace)
    return namespace["_cw_boot_hold_check"]


def state(bot):
    return (bot.strategy._entries_held, bot._boot_state_restoration_complete,
        bot._boot_restore_last_hold_log_at,
        bot._retry_keepwatch_boot_exclusions.call_args_list,
        bot._try_complete_boot_state_restoration.call_args_list)


@pytest.mark.parametrize("options", [
    dict(restored=False, held=True),
    dict(restored=False, held=False),
    dict(restored=True, held=True),
    dict(restored=True, held=False),
    dict(restored=True, held=True, dangerous=True),
    dict(restored=True, held=False, dangerous=True),
    dict(restored=False, complete_on_check=True),
    dict(restored=False, enabled=False),
    dict(restored=True, keepwatch=False),
    dict(restored=False, keepwatch=False),
])
def test_same_release_state_and_guard_logs_as_literal_ecd2(options, controlled_gc, caplog):
    old, new = subject(**options), subject(**options)
    with caplog.at_level(logging.INFO, logger=service.__name__):
        baseline_check()(old)
        old_logs = [(record.levelno, record.getMessage()) for record in caplog.records]
        caplog.clear()
        new._cw_boot_hold_check()
        new_logs = [(record.levelno, record.getMessage()) for record in caplog.records
                    if not record.getMessage().startswith("[V2-GC-FREEZE]")]
    assert state(new) == state(old)
    assert new_logs == old_logs
    released = any("[V2-BOOT-HOLD] released" in message for _, message in old_logs)
    assert controlled_gc[0].call_count == int(released)
    assert controlled_gc[1].call_count == int(released)


def test_freeze_follows_actual_release_log_and_restoration(controlled_gc, monkeypatch, caplog):
    bot = subject()

    def frozen():
        assert bot._boot_state_restoration_complete
        assert not bot.strategy._entries_held
        assert any("[V2-BOOT-HOLD] released" in record.getMessage() for record in caplog.records)

    controlled_gc[0].side_effect = frozen
    with caplog.at_level(logging.INFO, logger=service.__name__):
        bot._cw_boot_hold_check()
    assert [record.getMessage() for record in caplog.records][-1] == "[V2-GC-FREEZE] frozen=123"


def test_once_across_repeated_checks_rehold_and_multiple_instances(controlled_gc):
    first = subject()
    first._cw_boot_hold_check()
    first._cw_boot_hold_check()
    first.strategy.cw_armed_segments = lambda: [dict(
        symbol="LNKS", dangerous=True, entries_this_flip=0, max_entries=2)]
    first._cw_boot_hold_check()
    assert first.strategy._entries_held
    first.strategy.cw_armed_segments = lambda: []
    first._cw_boot_hold_check()
    second = subject()
    second._cw_boot_hold_check()
    assert not first.strategy._entries_held and not second.strategy._entries_held
    controlled_gc[0].assert_called_once_with()
    controlled_gc[1].assert_called_once_with()


def test_incomplete_then_complete_and_dangerous_then_safe(controlled_gc):
    bot = subject(restored=False, dangerous=True)
    bot._cw_boot_hold_check()
    bot._boot_state_restoration_complete = True
    bot._cw_boot_hold_check()
    controlled_gc[0].assert_not_called()
    assert bot.strategy._entries_held
    bot.strategy.cw_armed_segments = lambda: []
    bot._cw_boot_hold_check()
    controlled_gc[0].assert_called_once_with()


def test_freeze_failure_is_visible_and_never_retried(controlled_gc):
    bot = subject()
    failure = RuntimeError("controlled freeze failure")
    controlled_gc[0].side_effect = failure
    with pytest.raises(RuntimeError) as error:
        bot._cw_boot_hold_check()
    assert error.value is failure
    assert not bot.strategy._entries_held
    bot.strategy._entries_held = True
    bot._cw_boot_hold_check()
    controlled_gc[0].assert_called_once_with()
    controlled_gc[1].assert_not_called()


def test_count_failure_is_visible_without_second_freeze(controlled_gc):
    bot = subject()
    controlled_gc[1].side_effect = RuntimeError("controlled count failure")
    with pytest.raises(RuntimeError, match="count failure"):
        bot._cw_boot_hold_check()
    bot.strategy._entries_held = True
    bot._cw_boot_hold_check()
    controlled_gc[0].assert_called_once_with()


def test_no_other_gc_policy_or_boot_release_site_in_source():
    tree = ast.parse(Path(service.__file__).read_bytes())
    gc_calls = [node.func.attr for node in ast.walk(tree)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "gc"]
    assert sorted(gc_calls) == ["freeze", "get_freeze_count"]
    calls = [node for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
             and node.func.id == "_freeze_gc_after_boot_once"]
    assert len(calls) == 1

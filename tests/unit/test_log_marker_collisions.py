from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import textwrap

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "check_log_marker_collisions.py"
SPEC = importlib.util.spec_from_file_location("check_log_marker_collisions", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
guard = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = guard
SPEC.loader.exec_module(guard)


def _python_tree(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "python"
    root.mkdir()
    (root / "service.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def _shell_tree(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "shell"
    root.mkdir()
    (root / "consumer.sh").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def _marker_population(tmp_path: Path) -> Path:
    return _python_tree(
        tmp_path,
        '''
        import logging
        logger = logging.getLogger(__name__)
        logger.info("[OMS-V2-MIRROR] base")
        logger.info("[OMS-V2-MIRROR-EH] extended")
        logger.info("[V2-DB-SEED-GAP] refusal")
        logger.info("[V2-DB-SEED-GAP-CENSUS] denominator")
        logger.info("[V2-FANOUT-REACTIVE-LATCHED] latch claimed")
        logger.info("[V2-FANOUT-REACTIVE-SUPPRESSED] duplicate prevented")
        logger.info("[ORDER-CREATED] broker accepted it")
        logger.info("[REFUSED-NO-ORDER-CREATED] broker never saw it")
        ''',
    )


def test_known_positive_collect_deploy_evidence_count_is_refused(
    tmp_path: Path, capsys,
) -> None:
    python_root = _marker_population(tmp_path)
    shell_root = _shell_tree(
        tmp_path,
        '''
        echo "failures: $(cnt 'OMS-V2-MIRROR.*fail' oms)"
        ''',
    )

    assert guard.run([python_root], [shell_root]) == 1
    err = capsys.readouterr().err
    assert "ambiguous substring" in err
    assert "OMS-V2-MIRROR-EH" in err


def test_anchored_base_and_specific_longer_siblings_pass_without_comment_self_match(
    tmp_path: Path, capsys,
) -> None:
    python_root = _marker_population(tmp_path)
    shell_root = _shell_tree(
        tmp_path,
        r'''
        # cnt 'OMS-V2-MIRROR.*fail' is the historical defect, not a count.
        echo "failures: $(cnt 'OMS-V2-MIRROR\].*fail' oms)"
        echo "census: $(cnt 'V2-DB-SEED-GAP-CENSUS' schwab-1m-v2)"
        BASE=$(logcount "$LOG" '[V2-DB-SEED-GAP]')
        SPECIFIC=$(logcount "$LOG" '[V2-DB-SEED-GAP-CENSUS]')
        evidence.sh count --marker '[OMS-V2-MIRROR]'
        echo "specific suffix: $(cnt 'REFUSED-NO-ORDER-CREATED' oms)"
        ''',
    )

    assert guard.run([python_root], [shell_root]) == 0
    assert "PASS: 6 literal count consumers" in capsys.readouterr().out


def test_embedded_order_created_family_refuses_the_bare_metric(tmp_path: Path, capsys) -> None:
    python_root = _marker_population(tmp_path)
    shell_root = _shell_tree(tmp_path, "echo \"created: $(cnt 'ORDER-CREATED' oms)\"\n")

    assert guard.run([python_root], [shell_root]) == 1
    err = capsys.readouterr().err
    assert "ORDER-CREATED" in err and "REFUSED-NO-ORDER-CREATED" in err


def test_known_bad_latched_line_that_names_suppressed_remains_a_secondary_guard(
    tmp_path: Path, capsys,
) -> None:
    python_root = _python_tree(
        tmp_path,
        '''
        import logging
        logger = logging.getLogger(__name__)
        logger.info(
            "[V2-FANOUT-REACTIVE-LATCHED] denominator for "
            "[V2-FANOUT-REACTIVE-SUPPRESSED]"
        )
        ''',
    )
    shell_root = _shell_tree(tmp_path, "grep -cF '[V2-FANOUT-REACTIVE-LATCHED]' log\n")

    assert guard.run([python_root], [shell_root]) == 1
    err = capsys.readouterr().err
    assert "LATCHED" in err and "SUPPRESSED" in err


def test_dynamic_log_format_is_could_not_tell(tmp_path: Path, capsys) -> None:
    python_root = _python_tree(
        tmp_path,
        '''
        import logging
        logger = logging.getLogger(__name__)
        message = "selected at runtime"
        logger.info(message)
        ''',
    )
    shell_root = _shell_tree(tmp_path, "grep -cF '[CONTROL-MARKER]' log\n")

    assert guard.run([python_root], [shell_root]) == 3
    assert "COULD_NOT_TELL" in capsys.readouterr().err


def test_parse_failure_is_could_not_tell(tmp_path: Path, capsys) -> None:
    python_root = _python_tree(tmp_path, "def broken(:\n")
    shell_root = _shell_tree(tmp_path, "grep -cF '[CONTROL-MARKER]' log\n")

    assert guard.run([python_root], [shell_root]) == 3
    assert "COULD_NOT_TELL" in capsys.readouterr().err


CANCEL_PREFIX = ("[WBATTACH1-CANCEL] account=%s symbol=%s entry=%s entry_id=%s base=%s "
                 "reason=%s attempt=%s/%s call=%s actor=ATR_OWNER ")


def _cancel_source(tmp_path: Path, *, prefix: str = CANCEL_PREFIX, extra: str = "",
                   suffix: str = "'action=confirmed terminal=PROVEN'") -> Path:
    path = tmp_path / "src/project_mai_tai/oms/webull_attach_riskoff.py"
    path.parent.mkdir(parents=True)
    path.write_text(
        "async def strict_atr_handoff(service, row):\n"
        f"    cancel_format = {prefix!r}\n"
        f"{extra}"
        f"    service.logger.info(cancel_format + {suffix}, row)\n",
        encoding="utf-8",
    )
    return path


def test_reviewed_cancel_format_resolves_actual_literal(tmp_path: Path) -> None:
    findings, calls, population = guard.inspect_python_file(_cancel_source(tmp_path))
    assert findings == []
    assert calls == 1
    assert population == {"WBATTACH1-CANCEL"}


@pytest.mark.parametrize("extra", [
    "    cancel_format = row\n",
    "    cancel_format += '[OTHER-MARKER]'\n",
    "    del cancel_format\n",
    "    import other as cancel_format\n",
    "    def cancel_format(): pass\n",
])
def test_reviewed_cancel_format_rebinding_stays_unknown(tmp_path: Path, extra: str) -> None:
    with pytest.raises(guard.PopulationUnknown):
        guard.inspect_python_file(_cancel_source(tmp_path, extra=extra))


@pytest.mark.parametrize("prefix", ["[OTHER-MARKER]", CANCEL_PREFIX + "[OTHER-MARKER]"])
def test_reviewed_cancel_format_changed_literal_stays_unknown(tmp_path: Path, prefix: str) -> None:
    with pytest.raises(guard.PopulationUnknown):
        guard.inspect_python_file(_cancel_source(tmp_path, prefix=prefix))


def test_reviewed_cancel_format_suffix_marker_still_fails(tmp_path: Path) -> None:
    findings, calls, population = guard.inspect_python_file(
        _cancel_source(tmp_path, suffix="'[OTHER-MARKER]'"),
    )
    assert calls == 1
    assert population == {"WBATTACH1-CANCEL", "OTHER-MARKER"}
    assert len(findings) == 1


def test_reviewed_cancel_format_dynamic_suffix_stays_unknown(tmp_path: Path) -> None:
    with pytest.raises(guard.PopulationUnknown):
        guard.inspect_python_file(_cancel_source(tmp_path, suffix="row"))


@pytest.mark.parametrize("change", ["file", "function", "nested"])
def test_reviewed_cancel_format_does_not_escape_scope(tmp_path: Path, change: str) -> None:
    path = _cancel_source(tmp_path)
    if change == "file":
        other = path.with_name("other.py")
        other.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        path = other
    elif change == "function":
        path.write_text(path.read_text(encoding="utf-8").replace("strict_atr_handoff", "other"), encoding="utf-8")
    else:
        path.write_text(path.read_text(encoding="utf-8").replace(
            "    service.logger.info", "    def nested():\n        service.logger.info"), encoding="utf-8")
    with pytest.raises(guard.PopulationUnknown):
        guard.inspect_python_file(path)


@pytest.mark.parametrize("declaration", ["def nested():", "class Nested:"])
def test_nested_binding_cannot_whitelist_outer_format(tmp_path: Path, declaration: str) -> None:
    path = _cancel_source(tmp_path)
    path.write_text(
        "cancel_format = '[OTHER-MARKER]'\n"
        "async def strict_atr_handoff(service, row):\n"
        f"    {declaration}\n"
        f"        cancel_format = {CANCEL_PREFIX!r}\n"
        "    service.logger.info(cancel_format + 'action=request', row)\n",
        encoding="utf-8",
    )
    with pytest.raises(guard.PopulationUnknown):
        guard.inspect_python_file(path)

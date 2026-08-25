from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import textwrap


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

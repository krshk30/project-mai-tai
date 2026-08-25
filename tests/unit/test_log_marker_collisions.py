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


def _tree(tmp_path: Path, source: str) -> Path:
    root = tmp_path / "subject"
    root.mkdir()
    (root / "service.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return root


def test_known_bad_latched_line_that_names_suppressed_is_refused(
    tmp_path: Path, capsys,
) -> None:
    root = _tree(
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

    assert guard.run([root]) == 1
    err = capsys.readouterr().err
    assert "LATCHED" in err and "SUPPRESSED" in err


def test_known_good_separate_markers_and_regex_consumers_do_not_self_match(
    tmp_path: Path, capsys,
) -> None:
    root = _tree(
        tmp_path,
        r'''
        import logging
        logger = logging.getLogger(__name__)

        # A consumer regex and a guard fixture are not emitted log messages.
        order_created_regex = r"\[OMS-ORDER-CREATED\]"
        known_bad_fixture = "[V2-FANOUT-REACTIVE-LATCHED] [V2-FANOUT-REACTIVE-SUPPRESSED]"

        logger.info("[OMS-ORDER-CREATED] order accepted")
        logger.info("[V2-FANOUT-REACTIVE-LATCHED] latch claimed")
        logger.info("[V2-FANOUT-REACTIVE-SUPPRESSED] duplicate prevented")
        ''',
    )

    assert guard.run([root]) == 0
    out = capsys.readouterr().out
    assert "PASS: 3 emitted markers in 3 logging calls" in out


def test_dynamic_log_format_is_could_not_tell(tmp_path: Path, capsys) -> None:
    root = _tree(
        tmp_path,
        '''
        import logging
        logger = logging.getLogger(__name__)
        message = "selected at runtime"
        logger.info(message)
        ''',
    )

    assert guard.run([root]) == 3
    assert "COULD_NOT_TELL" in capsys.readouterr().err


def test_parse_failure_is_could_not_tell(tmp_path: Path, capsys) -> None:
    root = _tree(tmp_path, "def broken(:\n")

    assert guard.run([root]) == 3
    assert "COULD_NOT_TELL" in capsys.readouterr().err

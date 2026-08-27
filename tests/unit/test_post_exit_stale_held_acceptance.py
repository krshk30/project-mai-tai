from __future__ import annotations

from datetime import UTC, datetime, timedelta
import importlib.util
from pathlib import Path
import subprocess
import sys


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "ops"
    / "health"
    / "post_exit_stale_held_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location("post_exit_stale_held_acceptance", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
tool = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = tool
SPEC.loader.exec_module(tool)


def test_four_day_counts_state_episode_denominator_and_baseline() -> None:
    rows = tool.parse_rows(
        "2026-08-24|37|37|8|0\n"
        "2026-08-25|25|25|6|0\n"
        "2026-08-26|49|49|11|0\n"
        "2026-08-27|2|2|1|0\n"
    )

    verdict = tool.evaluate(rows)

    assert verdict.exit_code == tool.MEASURED
    output = "\n".join(verdict.lines)
    assert "2026-08-27=2 2026-08-26=49 2026-08-25=25 2026-08-24=37" in output
    assert "date=2026-08-26 refused_sells=49 classified_post_exit=49 post_exit_episodes=11" in output
    assert "refused_sells=113 post_exit_episodes=26" in output


def test_zero_refusals_and_zero_episodes_is_unexercised_not_fixed() -> None:
    verdict = tool.evaluate(())

    assert verdict.exit_code == tool.UNEXERCISED
    assert "refused_sells=0 post_exit_episodes=0" in "\n".join(verdict.lines)
    assert "not proof" in "\n".join(verdict.lines)


def test_unclassified_no_preceding_fill_stays_out_of_settlement_bucket() -> None:
    verdict = tool.evaluate((tool.DayCounts("2026-08-27", 2, 0, 0, 2),))

    assert verdict.exit_code == tool.MEASURED
    assert "classified_post_exit=0" in "\n".join(verdict.lines)
    assert "no_preceding_sell_fill=2" in "\n".join(verdict.lines)


def test_inconsistent_denominator_is_could_not_tell() -> None:
    verdict = tool.evaluate((tool.DayCounts("2026-08-27", 2, 2, 1, 1),))

    assert verdict.exit_code == tool.COULD_NOT_TELL
    assert "do not equal denominator" in "\n".join(verdict.lines)


def test_query_uses_stdin_and_real_psql_variable_interpolation(monkeypatch) -> None:
    since = datetime(2026, 8, 26, tzinfo=UTC)
    until = since + timedelta(days=1)
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout="2026-08-26|49|49|11|0\n", stderr="")

    monkeypatch.setattr(tool.subprocess, "run", fake_run)

    rows = tool.query_database(since, until)

    assert rows[0].post_exit_episodes == 11
    assert captured["command"][-2:] == ["-f", "-"]
    assert f"window_since={since.isoformat()}" in captured["command"]
    assert f"window_until={until.isoformat()}" in captured["command"]
    assert captured["input"] == tool.SQL
    assert ":'window_since'" in captured["input"]


def test_malformed_window_refuses() -> None:
    assert tool.main(["--since", "not-a-date", "--until", "2026-08-27T00:00:00Z"]) == 2
    assert tool.main(
        ["--since", "2026-08-27T01:00:00Z", "--until", "2026-08-27T00:00:00Z"]
    ) == 2

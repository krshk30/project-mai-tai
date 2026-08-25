"""Three-outcome controls for the daily Schwab refresh-count watch."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import gzip
import importlib.util
from pathlib import Path
import sys


SCRIPT = Path(__file__).resolve().parents[2] / "ops" / "health" / "schwab_refresh_count_check.py"
SPEC = importlib.util.spec_from_file_location("schwab_refresh_count_check", SCRIPT)
mod = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = mod
SPEC.loader.exec_module(mod)

DAY = date(2026, 8, 24)
START, END = mod.eastern_window(DAY)
NOW = END + timedelta(hours=2)


def _line(stamp: datetime, text: str) -> str:
    return f"{stamp.astimezone(UTC):%Y-%m-%d %H:%M:%S},000 INFO [test] {text}\n"


def _write_population(tmp_path: Path, refreshes: int, *, bracket: bool = True) -> list[Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    before = START - timedelta(minutes=10) if bracket else START + timedelta(minutes=1)
    after = END + timedelta(minutes=10) if bracket else END - timedelta(minutes=1)
    lines = [_line(before, "control evidence before window")]
    spacing = (END - START) / (refreshes + 1)
    lines.extend(_line(START + spacing * (idx + 1), mod.MARKER) for idx in range(refreshes))
    lines.append(_line(after, "control evidence after window"))
    plain = tmp_path / "control.log"
    plain.write_text("".join(lines), encoding="utf-8")
    return [plain]


def test_known_good_complete_day_is_healthy(tmp_path: Path) -> None:
    result = mod.evaluate(_write_population(tmp_path, 48), DAY, NOW)
    assert (result.code, result.verdict) == (0, "HEALTHY")
    assert "refreshes=48" in result.detail
    assert "healthy_range=46..52" in result.detail


def test_low_complete_day_is_not_healthy(tmp_path: Path) -> None:
    result = mod.evaluate(_write_population(tmp_path, 30), DAY, NOW)
    assert (result.code, result.verdict) == (1, "NOT_HEALTHY")
    assert "refreshes=30" in result.detail
    assert "~8.7 cadence-hours" in result.detail


def test_threshold_boundary_is_data_derived(tmp_path: Path) -> None:
    assert mod.evaluate(_write_population(tmp_path / "at", 46), DAY, NOW).verdict == "HEALTHY"
    assert mod.evaluate(_write_population(tmp_path / "under", 45), DAY, NOW).verdict == "NOT_HEALTHY"
    assert mod.evaluate(_write_population(tmp_path / "top", 52), DAY, NOW).verdict == "HEALTHY"
    assert mod.evaluate(_write_population(tmp_path / "over", 53), DAY, NOW).verdict == "NOT_HEALTHY"


def test_missing_logs_are_could_not_tell_not_zero() -> None:
    result = mod.evaluate([], DAY, NOW)
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "missing evidence is not zero" in result.detail


def test_corrupt_gzip_is_could_not_tell(tmp_path: Path) -> None:
    broken = tmp_path / "control.log-20260824.gz"
    broken.write_bytes(b"not gzip")
    result = mod.evaluate([broken], DAY, NOW)
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "cannot read" in result.detail


def test_plain_and_gzip_rotations_are_both_counted(tmp_path: Path) -> None:
    plain = tmp_path / "control.log"
    zipped = tmp_path / "control.log-20260824.gz"
    plain.write_text(_line(END + timedelta(minutes=10), "after"), encoding="utf-8")
    with gzip.open(zipped, "wt", encoding="utf-8") as handle:
        handle.write(_line(START - timedelta(minutes=10), "before"))
        for idx in range(48):
            handle.write(_line(START + timedelta(minutes=29 * (idx + 1)), mod.MARKER))
    result = mod.evaluate([plain, zipped], DAY, NOW)
    assert (result.code, result.verdict) == (0, "HEALTHY")
    assert "files=2" in result.detail


def test_unbracketed_population_is_could_not_tell_not_low(tmp_path: Path) -> None:
    result = mod.evaluate(_write_population(tmp_path, 30, bracket=False), DAY, NOW)
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "does not bracket" in result.detail


def test_missing_middle_log_slice_is_could_not_tell_not_low(tmp_path: Path) -> None:
    path = tmp_path / "control.log"
    lines = [_line(START - timedelta(minutes=10), "before")]
    for hour in range(24):
        if hour != 10:  # model a missing rotation slice, not a quiet refresh outcome
            lines.append(_line(START + timedelta(hours=hour, minutes=15), mod.MARKER))
    lines.append(_line(END + timedelta(minutes=10), "after"))
    path.write_text("".join(lines), encoding="utf-8")

    result = mod.evaluate([path], DAY, NOW)
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "hourly window bucket(s) 10" in result.detail
    assert "must not become a low refresh count" in result.detail


def test_day_must_be_complete_plus_one_refresh_cadence(tmp_path: Path) -> None:
    result = mod.evaluate(
        _write_population(tmp_path, 48),
        DAY,
        END + timedelta(minutes=20),
    )
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "35-minute evidence lag" in result.detail


def test_refresh_marker_without_timestamp_is_could_not_tell(tmp_path: Path) -> None:
    path = tmp_path / "control.log"
    path.write_text(
        _line(START - timedelta(minutes=10), "before")
        + f"INFO {mod.MARKER} no timestamp\n"
        + _line(END + timedelta(minutes=10), "after"),
        encoding="utf-8",
    )
    result = mod.evaluate([path], DAY, NOW)
    assert (result.code, result.verdict) == (3, "COULD_NOT_TELL")
    assert "without a parseable UTC timestamp" in result.detail


def test_dst_days_scale_the_24_hour_threshold() -> None:
    spring_start, spring_end = mod.eastern_window(date(2026, 3, 8))
    fall_start, fall_end = mod.eastern_window(date(2026, 11, 1))
    assert (spring_end - spring_start) == timedelta(hours=23)
    assert (fall_end - fall_start) == timedelta(hours=25)
    assert mod.limits_for_window(spring_start, spring_end) == (45, 49)
    assert mod.limits_for_window(fall_start, fall_end) == (48, 54)


def test_workflow_schedules_complete_previous_day_and_pages_failures() -> None:
    workflow = (
        Path(__file__).resolve().parents[2]
        / ".github"
        / "workflows"
        / "schwab-refresh-count-watch.yml"
    ).read_text(encoding="utf-8")
    assert 'cron: "15 6 * * *"' in workflow
    assert "ops/health/schwab_refresh_count_check.py" in workflow
    assert "https://ntfy.sh/mai-tai-preopen-28806a5a97b7" in workflow
    assert "if: ${{ failure() }}" in workflow
    assert "systemctl restart" not in workflow
    assert "git pull" not in workflow

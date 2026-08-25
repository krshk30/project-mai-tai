import os
from pathlib import Path
import shutil
import subprocess


REPO = Path(__file__).resolve().parents[2]
COLLECTOR = REPO / "ops" / "health" / "collect_deploy_evidence.sh"


def _render(tmp_path: Path, lines: list[str]) -> str:
    fixture = tmp_path / "schwab-1m-v2.tl"
    fixture.write_text("\n".join(lines) + "\n", encoding="utf-8")
    git_bash = Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Git/bin/bash.exe"
    bash = str(git_bash) if git_bash.is_file() else shutil.which("bash")
    assert bash is not None, "the shell collector needs bash"
    completed = subprocess.run(
        [bash, str(COLLECTOR), "--render-seed-gap-census", str(fixture)],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def test_census_names_its_coverage_end_and_excludes_later_events(tmp_path: Path) -> None:
    output = _render(
        tmp_path,
        [
            "2026-08-25 04:00:01,100 INFO [V2-DB-SEED-GAP-CENSUS] "
            "truncations=2 of 7 seed evaluations since boot",
            "2026-08-25 16:34:00,000 WARNING [V2-DB-SEED-GAP] "
            "boundary session-calendar lookup failed",
        ],
    )

    assert "truncations=2 of 7 seed evaluations since boot" in output
    assert "counter_window=that process boot (start not encoded)..2026-08-25 04:00:01 UTC" in output
    assert "COVERAGE END: 2026-08-25 04:00:01 UTC" in output
    assert "Events after that snapshot are NOT covered by 6c" in output
    assert "not a current-day or per-day census" in output
    assert "read 6b for fail-opens" in output
    assert "16:34" not in output, "a later fail-open must not be laundered into the 04:00 census"


def test_census_without_a_snapshot_is_unmeasured_not_clean(tmp_path: Path) -> None:
    output = _render(
        tmp_path,
        ["2026-08-25 16:34:00,000 WARNING [V2-DB-SEED-GAP] lookup failed"],
    )

    assert "NO CENSUS SNAPSHOT" in output
    assert "UNMEASURED" in output
    assert "COVERAGE END" not in output

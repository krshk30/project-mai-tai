from __future__ import annotations

from pathlib import Path

from project_mai_tai.settings import Settings
from scripts.replay_schwab_tick_capture import _resolve_input_path


def test_resolve_input_path_uses_archive_root_when_date_provided(tmp_path) -> None:
    class Args:
        input = None
        date = "2026-04-20"
        root = None

    settings = Settings(schwab_tick_archive_root=str(tmp_path))
    path = _resolve_input_path(Args(), settings, "EFOI")
    assert path == Path(tmp_path) / "2026-04-20" / "EFOI.jsonl"

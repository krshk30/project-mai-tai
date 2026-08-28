from pathlib import Path


INSTALLER = Path(__file__).parents[2] / "ops" / "health" / "install_eod_counts.sh"


def test_installer_is_bound_to_existing_root_cron_location() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert "target_dir=/home/trader/entry_fix_watch" in source
    assert 'target_report="$target_dir/eod_counts.py"' in source
    assert 'target_cron="$target_dir/eod_cron.sh"' in source
    assert 'crontab -l | grep -Fq "$target_cron"' in source
    assert "REFUSED: root crontab does not reference" in source
    assert "REFUSED: installed cron wrapper is missing" in source


def test_installer_preserves_bytes_mode_owner_and_previous_copy() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    assert 'compile(path.read_text(encoding="utf-8")' in source
    assert 'cp -a "$target_report" "$target_report.pre-versioned-$stamp"' in source
    assert 'install -o root -g root -m 0755 "$source_report" "$target_report"' in source
    assert 'cmp "$source_report" "$target_report"' in source
    assert "cron_schedule_unchanged=1" in source


def test_installer_refuses_non_root_before_any_copy() -> None:
    source = INSTALLER.read_text(encoding="utf-8")

    root_guard = source.index("REFUSED: run as root")
    copy = source.index('cp -a "$target_report"')
    install = source.index('install -o root -g root -m 0755 "$source_report"')
    assert root_guard < copy < install

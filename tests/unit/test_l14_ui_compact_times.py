"""C12039 SIM-only time/layout controls; author UNRUN, no native/page claim."""

from copy import deepcopy
from html import unescape
import re

import pytest

from project_mai_tai.atr_simulation import cp_sim_display as ui
from project_mai_tai.services import control_plane as cp
from tests.unit.test_l14_ui_actual_skip_outcomes import render


def expected_time(label):
    """Independent display expectation from the canonical renderer's ET text."""
    match = re.fullmatch(r'\d{4}-(\d{2}-\d{2}) (\d{2}):(\d{2}):\d{2}(?: (AM|PM))? ET', label)
    if match is None:
        return label
    day, hour, minute, period = match.groups()
    hour = int(hour) if period is None else int(hour) % 12 + (12 if period == 'PM' else 0)
    return f'{day} {hour:02d}:{minute}'


def assert_only_sim_time_cells_changed(original, rendered):
    pattern = r'(<td\b[^>]*>)(.*?)(</td>)'
    before = list(re.finditer(pattern, original, re.S))
    after = list(re.finditer(pattern, rendered, re.S))
    assert len(before) == len(after) == 9
    for index in range(9):
        if index not in (3, 5):
            assert after[index][0] == before[index][0]
            continue
        full = unescape(before[index][2])
        assert unescape(after[index][2]) == expected_time(full)
        assert unescape(re.search(r' title="([^"]*)"', after[index][1])[1]) == full
        assert f'data-sim-time="{"entry" if index == 3 else "exit"}"' in after[index][1]
    # The outer row, indentation and seven non-time cells are not reformatted.
    def without_times(raw, cells):
        for index in (5, 3):
            cell = cells[index]
            raw = raw[:cell.start()] + 'TIME_CELL' + raw[cell.end():]
        return raw
    assert without_times(original, before) == without_times(rendered, after)


@pytest.mark.parametrize('label,expected', [
    ('2026-09-22 09:36:03 AM ET', '09-22 09:36'),
    ('2026-09-22 01:36:03 PM ET', '09-22 13:36'),
    ('2026-09-22 12:01:59 AM ET', '09-22 00:01'),
    ('2026-09-22 12:01:59 PM ET', '09-22 12:01'),
    ('2026-10-21 15:59:01 ET', '10-21 15:59'),
    ('UNKNOWN', 'UNKNOWN'), ('-', '-'), ('invalid retained clock', 'invalid retained clock'),
])
def test_compact_label_is_24h_ET_with_unknown_unchanged(label, expected):
    assert ui._compact_time(label) == expected


@pytest.mark.parametrize('entry,exit,expected_entry,expected_exit', [
    ('2026-09-22T13:36:03+00:00', '2026-09-22T14:00:01+00:00', '09-22 09:36', '09-22 10:00'),
    ('2026-12-01T14:36:03+00:00', '2026-12-01T15:00:01+00:00', '12-01 09:36', '12-01 10:00'),
    ('2026-09-22 09:36:03 ET', 'UNKNOWN', '09-22 09:36', 'UNKNOWN'),
    ('<script>bad</script>', 'UNKNOWN', '<script>bad</script>', 'UNKNOWN'),
])
def test_only_two_sim_cells_change_full_title_and_source_input_are_preserved(
        entry, exit, expected_entry, expected_exit):
    row = dict(ticker='CONTROLLED', path='webull', quantity='7', entry_time=entry,
        exit_time=exit, entry_price='$1.23', exit_price='$1.45', pnl='1.54',
        pnl_label='$+1.54', pnl_pct_label='+17.9%', summary='TARGET_5', pnl_label_known=True)
    before = deepcopy(row)
    original = cp._render_completed_position_row(row)
    actual = ui._sim_row(row, cp._render_completed_position_row)
    assert_only_sim_time_cells_changed(original, actual)
    cells = re.findall(r'<td\b[^>]*>(.*?)</td>', actual, re.S)
    assert (unescape(cells[3]), unescape(cells[5])) == (expected_entry, expected_exit)
    assert row == before and cp._render_completed_position_row(row) == original
    assert '<script>' not in actual


def test_all_seven_modes_keep_549_rows_226_skip_audit_and_full_time_titles():
    html = render()
    sections = re.findall(r'<section\b[^>]*data-sim-mode="([^"]+)"[^>]*>(.*?)</section>', html, re.S)
    counts = {}
    for mode, section in sections:
        body = re.search(r'<tbody>(.*?)</tbody>', section, re.S)[1]
        rows = re.findall(r'<tr>.*?</tr>', body, re.S)
        counts[mode] = len(rows)
        for row in rows:
            cells = re.findall(r'<td\b([^>]*)>(.*?)</td>', row, re.S)
            assert len(cells) == 9
            for index in (3, 5):
                attrs, text = cells[index]
                full = unescape(re.search(r' title="([^"]*)"', attrs)[1])
                assert unescape(text) == expected_time(full)
                if full != 'UNKNOWN':
                    assert re.fullmatch(r'\d\d-\d\d \d\d:\d\d', unescape(text))
    assert counts == dict(FIRSTBAR=140, FIRSTBAR_X3=140, AGGREGATE_ENTRY=253,
        **{'LIVE:ACTUAL': 4, 'TODAY:SUPPLIED': 4, 'VOLBODY:QUEUE_AWARE': 4, 'VOLBODY:OPTIMISTIC': 4})
    assert sum(counts.values()) == 549 and html.count('data-firstbar-kind="SKIP"') == 226
    assert html.count('<summary>113 signals skipped (no trade)</summary>') == 2


def test_fixed_width_nine_column_layout_is_scoped_and_mobile_scroll_is_retained():
    html = render()
    assert ui.COLUMN_WIDTHS == (6, 10, 5, 11, 8, 11, 8, 17, 24)
    assert len(ui.COLUMN_WIDTHS) == 9 and sum(ui.COLUMN_WIDTHS) == 100
    assert html.count(ui.COLGROUP) == html.count(ui.HEADER) == 7
    assert '#atr-sim-compact table{min-width:760px;width:100%;table-layout:fixed}' in html
    assert '#atr-sim-compact .table-wrap{max-width:100%;overflow:auto}' in html
    assert ui.COLUMNS[-2:] == ('P&L', 'Exit reason')
    assert '<style>table{' not in html and '<style>td{' not in html

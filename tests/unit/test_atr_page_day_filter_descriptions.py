"""ATR SIM page: approved table descriptions and a presentation-only day filter (operator 10-27)."""

from html import escape
import re

from project_mai_tai.atr_simulation import cp_sim_display as ui
from tests.unit.test_l14_ui_default_compact import TableParser, rendered


def test_every_sim_table_and_the_overrides_carry_one_plain_description():
    html = rendered()
    sections = re.findall(r'<section\b[^>]*data-sim-mode="([^"]+)"[^>]*>(.*?)</section>', html, re.S)
    assert len(sections) == 7
    for _mode, section in sections:
        title = re.search(r'<h3>(.*?)</h3>', section)[1]
        assert section.count('<div class="sim-desc">') == 1
        assert escape(ui.DESCRIPTIONS[title]) in section
    overrides = re.search(r'<details class="panel full sim-overrides">.*?</details>', html, re.S)[0]
    assert escape(ui.OVERRIDES_DESCRIPTION) in overrides
    assert "SIMULATION - NOT REAL TRADES" not in "".join(ui.DESCRIPTIONS.values())


def test_day_filter_is_presentation_only_every_row_stays_in_the_html():
    html = rendered()
    assert html.count('data-sim-dayfilter="true"') == 1
    assert html.count('id="atr-sim-day"') == 1 and html.count('id="atr-sim-all"') == 1
    parser = TableParser()
    parser.feed(html)
    # Same 775 rows as before the filter: history is kept, the browser only hides rows.
    assert sum(map(len, parser.tables.values())) == 775
    assert 'style="display:none"' not in html
    # The filter keys on the entry/exit ET title the canonical row already carries.
    assert 'td[data-sim-time="entry"]' in ui.DAY_FILTER_SCRIPT
    assert 'timeZone:"America/New_York"' in ui.DAY_FILTER_SCRIPT
    # Controls above the tables; the script after the last table, so its first pass sees every row.
    assert html.index('data-sim-dayfilter="true"') < html.index('data-firstbar-mount="true"')
    last_row = html.rindex('</tbody>')
    assert html.count(ui.DAY_FILTER_SCRIPT) == 1 and html.index(ui.DAY_FILTER_SCRIPT) > last_row


def test_real_recorded_table_never_wears_the_simulation_badge_or_caption():
    html = rendered()
    sections = dict(re.findall(r'<section\b[^>]*data-sim-mode="([^"]+)"[^>]*>(.*?)</section>', html, re.S))
    real = sections.pop(ui.REAL_MARKER)
    assert ui.REAL_BADGE in real and escape(ui.REAL_CAPTION) in real
    assert ui.SIM_BADGE not in real and escape(ui.CAPTION) not in real
    assert len(sections) == 6
    for section in sections.values():
        assert ui.SIM_BADGE in section and escape(ui.CAPTION) in section and ui.REAL_BADGE not in section

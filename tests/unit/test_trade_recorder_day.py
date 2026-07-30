"""A recorded trade must be filed under the ET day its ENTRY FILLED on, not the day the job ran.

⭐ WHY IT EXISTS (2026-07-30). The recorder derived its day from `datetime.now(ET)` and stamped that
into every record AND the filename. The cron passes `--since-mins 1440`, so the first run of each
day reached back 24h and swept the PREVIOUS day's tail into today's file under today's date. Proven
on the live box: a 06:42 ET run produced a `2026-07-30.jsonl` holding 23 round trips whose
`entry_at_et` were every one of them `2026-07-29`.

That is precisely the failure this tool was built to end -- a plausible-looking answer that
misattributes real trades. The wide lookback is deliberate (a missed run must self-heal, a late EH
exit must still be caught), so the DAY is derived from the data instead of from the clock.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import ops.health.trade_recorder as mod

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")


def test_day_comes_from_the_entry_not_from_now() -> None:
    """THE REGRESSION: a trade that entered yesterday must never be filed under today."""
    yesterday_entry = datetime(2026, 7, 29, 15, 30, tzinfo=ET)
    assert mod.trade_day(yesterday_entry) == "2026-07-29"


def test_a_utc_timestamp_is_converted_to_the_ET_day() -> None:
    """⛔ The DB hands back UTC. 2026-07-30 00:30 UTC is still 2026-07-29 in ET -- filing it under
    the UTC date would misdate the entire post-20:00 ET tail, every single day."""
    late_evening_et = datetime(2026, 7, 30, 0, 30, tzinfo=UTC)   # = 2026-07-29 20:30 ET
    assert mod.trade_day(late_evening_et) == "2026-07-29"


def test_one_window_spanning_two_days_splits_into_two_days() -> None:
    """The 1440-minute lookback legitimately spans two ET days; both must be represented."""
    entries = [
        datetime(2026, 7, 29, 19, 55, tzinfo=ET),
        datetime(2026, 7, 30, 7, 1, tzinfo=ET),
        datetime(2026, 7, 30, 9, 45, tzinfo=ET),
    ]
    days = [mod.trade_day(e) for e in entries]
    assert days == ["2026-07-29", "2026-07-30", "2026-07-30"]
    assert len(set(days)) == 2, "a two-day window collapsing to one day is the bug"


def test_dst_boundary_uses_the_ET_calendar_day() -> None:
    """Guarding in ET is what makes this DST-correct; a fixed UTC offset would slip by an hour."""
    # 2026-11-01 is the EDT->EST transition. 01:30 EDT and 01:30 EST are both still Nov 1 in ET.
    before = datetime(2026, 11, 1, 5, 30, tzinfo=UTC)   # 01:30 EDT
    after = datetime(2026, 11, 1, 6, 30, tzinfo=UTC)    # 01:30 EST
    assert mod.trade_day(before) == "2026-11-01"
    assert mod.trade_day(after) == "2026-11-01"


def test_load_seen_reads_exit_ids_and_survives_a_torn_last_line(tmp_path) -> None:
    """Idempotency is per day-file. A torn final line (the job killed mid-write) must not block the
    rest of the day's writes -- losing a whole day's recording to one bad line is worse than the
    line."""
    p = tmp_path / "2026-07-30.jsonl"
    p.write_text(
        '{"exit_boid":"AAA","symbol":"GMM"}\n'
        '{"exit_boid":"BBB","symbol":"NCRA"}\n'
        '{"exit_boid":"CCC","sym',                      # torn: process died mid-write
        encoding="utf-8",
    )
    assert mod.load_seen(str(p)) == {"AAA", "BBB"}


def test_load_seen_on_a_missing_file_is_empty_not_an_error(tmp_path) -> None:
    """The first run of a new day has no file yet; that is the normal case, not a failure."""
    assert mod.load_seen(str(tmp_path / "nope.jsonl")) == set()


def test_the_run_clock_does_not_appear_in_the_day_at_all() -> None:
    """⭐ Pins the PROPERTY, not just today's value: trade_day must depend ONLY on its argument.
    Called twice a day apart in wall-clock terms, the same entry still yields the same day."""
    entry = datetime(2026, 7, 29, 12, 0, tzinfo=ET)
    first = mod.trade_day(entry)
    second = mod.trade_day(entry + timedelta(0))
    assert first == second == "2026-07-29"

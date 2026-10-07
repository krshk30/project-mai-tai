# Timing diagnostic addendum

User reported parent exact-994 focused suite launch at 2026-10-07T18:42:00Z,
including HOTFIX1 retained ON/OFF 60-second benchmarks, with possible CPU
overlap until approximately 18:46:00Z. This is a user-reported overlap window,
not an independently measured contention cause. No stall is blamed on it.

Pre-addendum frozen test HEAD ba265afe4085f301b0182cf800bfa38cdab2c80f,
tree d2236af7bd35044639c695a8496380d9a87933ec:

| Receipt | Runner metadata UTC | Events | Seconds | Max loop stall ms | Result |
| --- | --- | --- | --- | --- | --- |
| benchmark-01 | 2026-10-07T18:40:35.697282+00:00 | 14400 | 60.000104708 | 48.385249989 | PASS |
| benchmark-02 | 2026-10-07T18:41:50.173898+00:00 | 14400 | 60.000434500 | 35.860499993 | PASS |

Both have one held NFQ2 owner for every tick, zero retry messages, zero tick SQL,
12 periodic NFQ2 +12 periodic drift-cache transactions, and all 84 SQL statements
off-loop. 14,400 real drift-reader calls and 7,200 real cached price checks each;
drift tolerance 1 cent. Neither performance gate failed.

These original runs recorded exact monotonic duration but not exact wall-clock
performance-gate start/end. Setup log timestamps are 14:40:36.408 ET and the
benchmark-02 setup line retained in its raw log. Do not invent exact gate UTC
boundaries retrospectively. Run 2's duration overlaps the user's reported
18:42 launch; it nevertheless passes. Runner launch timing is not gate timing.

The test-only diagnostic change adds actual UTC timestamps immediately around
each performance gate and actual per-second quote counts, without changing
runtime, pacing, GC, assertions or thresholds. Runner records subprocess
start/end UTC as well. A new exact test/tree freeze will identify subsequent
receipts. Both pre-addendum receipts and their original hashes remain intact.
Further performance runs wait until after the user's expected parent window;
completion/absence of other host activity must not be assumed solely from time.

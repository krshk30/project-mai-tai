# C11810 Changed Two Fixture Clocks

Test-only successor to frozen f02c093f39481ff3395a959527d3de372335c6ac.
Main's original FIRST30 receipt remains 28 PASS / 2 FAIL / 1.32s, including
the retained log/XML at /private/tmp/OCT24-F02C-C11810-WINDOWS-FIRST30-20261024.*.

Static cause: SOURCE_ANCHOR_MS=1792742400000 is Friday 08:00 UTC / 04:00 ET.
The controlled fixture bar starts at anchor+60000 and closes at 08:02 UTC.
The two former observations at 04:13 UTC and 04:00 UTC were outside that
window, so no seed observation was recorded. This is not an actual input
clock correction or a production policy defect.

Only two observation literals change: 08:13 UTC exercises the unchanged
11-minute elapsed failure assertion; 08:00 UTC exercises the unchanged
before-first-recorded-bar UNKNOWN assertion. All assertions, raw inputs,
thresholds, helper/APP/schema code and session mapping are unchanged.

Main's changed-only selection, from this exact clean successor root:

    tests/unit/test_l12_c11810_carried_windows.py::test_carried_seed_elapsed_after_first_recorded_closed_bar_over_ten_fails
    tests/unit/test_l12_c11810_carried_windows.py::test_early_seed_before_first_bar_is_not_clamped_to_zero_minutes

Both changed controls are UNRUN by the author. No repeat of the 28 passes,
no actual timeline/PG/native/boot qualification, and no reuse of an expired
copy claim. Existing source f02 and its raw failures remain available in Git.

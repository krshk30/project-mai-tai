# 2026-09-29: Restart Bar-Continuity Gate Correction

## Operator rule 1

Before reporting a failure, read both the check and the system design it claims to verify. Call
the result REAL FAILURE when the design was violated, EXPECTED BY DESIGN when the check is
mis-scoped, or UNKNOWN when evidence cannot decide. Never relay a raw rc or RED row as the
system's status, and do not impose an operating restriction that the design already covers.
Apply this to pre-open gates, first-session reads, and deployment checks.

## My correction of the 06:24 ET read

I reported the 2026-09-29 pre-open gate rc=1 and its 8/9 rows as a restart failure. I checked the
generated report and the approved PID/flag pins, but did not read the continuity check against
the v2 REST warmup/gap-fill code before using its verdict. That was my error. The old row looked
only at persisted `source='live'` adjacent pairs and required a live pair to bracket the restart.
At the 2026-09-28 18:42 ET restart, no symbol was watched at stop; ONFO was added at 18:58.
The absence of a bracketing live pair was EXPECTED BY DESIGN, not proof of a bar hole. REST warmup
and literal BOOT-HOLD release were separately observed, but those aggregate markers alone do not
prove every minute of ONFO's strategy-memory series.

The corrected gate grades each symbol watched at stop or added later in the restart session. It
uses a fresh pre-stop isolated-bot watchlist snapshot when available, complete watchlist log
updates, the per-symbol fresh REST/streamer warmup marker as the first current-bar boundary,
persisted bar minutes, and independent aggregate-eligible tape minutes. It reports covered,
uncovered printed, and quiet minutes per symbol. A missing source or unverifiable in-memory-only
REST replay remains COULD_NOT_TELL. The persisted strategy-bar-history gap census is INFO; it
cannot by itself overrule evidence that REST repaired the strategy's active series.

No trading service, flag, or broker path changes are part of this gate correction. The 06:24
artifact remains at `/home/trader/known_defect_regression_watch/v2-restart-evidence-20260929.md`;
it must not be retrospectively relabelled a 9/9 PASS without per-symbol coverage evidence.

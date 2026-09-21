# Momentum PATH control: source-order correction

Acceptance: **PASS** across 30/30 sessions and 60 strategy/session rows.

The old timestamp-only control counted prints that shared the detection millisecond but arrived before the detecting print. The corrected control begins at that print's source ordinal and retains exact `PATH == union` equality.

| Session | Bot | PATH | Old union | Old extra | Same-ms before detect | Corrected union | Exact |
|---|---|---:|---:|---:|---:|---:|---|
| 2026-08-05 | momentum_30s | 131489 | 131501 | 12 | 12 | 131489 | yes |
| 2026-08-06 | momentum_30s | 211839 | 211855 | 16 | 16 | 211839 | yes |
| 2026-08-06 | momentum_60s | 370928 | 370930 | 2 | 2 | 370928 | yes |
| 2026-08-07 | momentum_30s | 361145 | 361274 | 129 | 129 | 361145 | yes |
| 2026-08-07 | momentum_60s | 441128 | 441374 | 246 | 246 | 441128 | yes |
| 2026-08-10 | momentum_30s | 396121 | 396130 | 9 | 9 | 396121 | yes |
| 2026-08-10 | momentum_60s | 450168 | 450184 | 16 | 16 | 450168 | yes |
| 2026-08-11 | momentum_30s | 286378 | 286510 | 132 | 132 | 286378 | yes |
| 2026-08-11 | momentum_60s | 314191 | 314306 | 115 | 115 | 314191 | yes |
| 2026-08-12 | momentum_30s | 38065 | 38070 | 5 | 5 | 38065 | yes |
| 2026-08-12 | momentum_60s | 44563 | 44568 | 5 | 5 | 44563 | yes |
| 2026-08-13 | momentum_30s | 166859 | 166864 | 5 | 5 | 166859 | yes |
| 2026-08-13 | momentum_60s | 248462 | 248468 | 6 | 6 | 248462 | yes |
| 2026-08-14 | momentum_30s | 262276 | 262290 | 14 | 14 | 262276 | yes |
| 2026-08-14 | momentum_60s | 348413 | 348426 | 13 | 13 | 348413 | yes |
| 2026-08-17 | momentum_30s | 200446 | 200448 | 2 | 2 | 200446 | yes |
| 2026-08-17 | momentum_60s | 300054 | 300074 | 20 | 20 | 300054 | yes |
| 2026-08-18 | momentum_30s | 349265 | 349338 | 73 | 73 | 349265 | yes |
| 2026-08-18 | momentum_60s | 417086 | 417110 | 24 | 24 | 417086 | yes |
| 2026-08-19 | momentum_30s | 399602 | 399651 | 49 | 49 | 399602 | yes |
| 2026-08-19 | momentum_60s | 530651 | 530672 | 21 | 21 | 530651 | yes |
| 2026-08-20 | momentum_30s | 241720 | 241723 | 3 | 3 | 241720 | yes |
| 2026-08-20 | momentum_60s | 364736 | 364751 | 15 | 15 | 364736 | yes |
| 2026-08-21 | momentum_30s | 178024 | 178057 | 33 | 33 | 178024 | yes |
| 2026-08-21 | momentum_60s | 257664 | 257676 | 12 | 12 | 257664 | yes |
| 2026-08-24 | momentum_30s | 233971 | 233981 | 10 | 10 | 233971 | yes |
| 2026-08-24 | momentum_60s | 228021 | 228031 | 10 | 10 | 228021 | yes |
| 2026-08-25 | momentum_30s | 402137 | 402145 | 8 | 8 | 402137 | yes |
| 2026-08-25 | momentum_60s | 574662 | 574702 | 40 | 40 | 574662 | yes |
| 2026-08-26 | momentum_30s | 333945 | 333959 | 14 | 14 | 333945 | yes |
| 2026-08-26 | momentum_60s | 404878 | 404889 | 11 | 11 | 404878 | yes |
| 2026-08-27 | momentum_30s | 261346 | 261349 | 3 | 3 | 261346 | yes |
| 2026-08-27 | momentum_60s | 308384 | 308387 | 3 | 3 | 308384 | yes |
| 2026-08-28 | momentum_30s | 52285 | 52288 | 3 | 3 | 52285 | yes |
| 2026-08-28 | momentum_60s | 89161 | 89172 | 11 | 11 | 89161 | yes |
| 2026-08-31 | momentum_30s | 208395 | 208399 | 4 | 4 | 208395 | yes |
| 2026-08-31 | momentum_60s | 276658 | 276676 | 18 | 18 | 276658 | yes |
| 2026-09-01 | momentum_30s | 123748 | 123749 | 1 | 1 | 123748 | yes |
| 2026-09-01 | momentum_60s | 149114 | 149115 | 1 | 1 | 149114 | yes |
| 2026-09-02 | momentum_30s | 160521 | 160522 | 1 | 1 | 160521 | yes |
| 2026-09-02 | momentum_60s | 160660 | 160668 | 8 | 8 | 160660 | yes |
| 2026-09-03 | momentum_30s | 33533 | 33534 | 1 | 1 | 33533 | yes |
| 2026-09-03 | momentum_60s | 37851 | 37853 | 2 | 2 | 37851 | yes |
| 2026-09-04 | momentum_30s | 92741 | 92750 | 9 | 9 | 92741 | yes |
| 2026-09-04 | momentum_60s | 92747 | 92758 | 11 | 11 | 92747 | yes |
| 2026-09-09 | momentum_30s | 22477 | 22481 | 4 | 4 | 22477 | yes |
| 2026-09-09 | momentum_60s | 63796 | 63799 | 3 | 3 | 63796 | yes |
| 2026-09-11 | momentum_30s | 7436 | 7437 | 1 | 1 | 7436 | yes |
| 2026-09-11 | momentum_60s | 32731 | 32733 | 2 | 2 | 32731 | yes |
| 2026-09-14 | momentum_30s | 34572 | 34583 | 11 | 11 | 34572 | yes |
| 2026-09-14 | momentum_60s | 52826 | 52842 | 16 | 16 | 52826 | yes |
| 2026-09-15 | momentum_30s | 36298 | 36302 | 4 | 4 | 36298 | yes |
| 2026-09-15 | momentum_60s | 65004 | 65019 | 15 | 15 | 65004 | yes |
| 2026-09-16 | momentum_30s | 96557 | 96629 | 72 | 72 | 96557 | yes |
| 2026-09-16 | momentum_60s | 103663 | 103685 | 22 | 22 | 103663 | yes |

Zero-discrepancy sessions: 2/30.

The prior expectation of 29 zero-discrepancy sessions and 12 extras only on 2026-08-05 was **not met**. The old control had 1296 classified extras across 28/30 sessions; this does not change the corrected control's exact 30/30 result.

Known limit: REST page order for prints sharing one millisecond is not guaranteed to match live websocket arrival order; the detecting print within that millisecond may differ.

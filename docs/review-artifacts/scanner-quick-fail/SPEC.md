# SPEC — QUICK-FAIL: a confirmed stock that gives the spike back within 10 minutes is dropped for the day (SCANNER level)

**Written by `claude-1` 2026-09-23 ~13:45 ET, operator's words: "write the spec to codex on quick fail… we will do backtest
again and make sure if correct… this is on scanner level not at the bot level." For `codex-2` to build after a second,
independent backtest confirms it. Nothing built yet; nothing deploys until both backtests agree and the operator says GO.**

## The pattern, in the operator's words

> "If a stock goes very quickly below the ATR line after the spike — like 5 to 10 minutes — it is a bad one for us."

Of the five spike patterns he described, this is the ONE the month supports (the other four had too few cases or went
the other way: a stock still rising 30 min after confirm did WORSE, 45% vs 57%).

## The rule

At the **scanner** (`MomentumConfirmedScanner`, `strategy_core/momentum_confirmed.py`), for every ticker it CONFIRMS:

- remember `confirmed_at` (already stored: `track["confirmed_at"]`) and `confirmed_price` (already stored:
  `track["confirmed_price"]`);
- on every subsequent snapshot within **`quick_fail_window_minutes` = 10** of `confirmed_at`, if the **1-minute bar CLOSE**
  (not a tick) is **below `confirmed_price`**, mark the ticker **QUICK_FAIL for the trade date**;
- a QUICK_FAIL ticker is removed from the confirmed set the same way a FADE is (`prune_faded_candidates` path, so the bots'
  watchlists purge it through the existing `_purge_faded_symbols_from_bot_watchlists`) and **may not be re-confirmed that
  day** (`allow_reconfirmation` is NOT called for it; a day-scoped `_quick_failed: set[str]` cleared at the 04:00 ET roll);
- write a `QUICK_FAIL` row to `scanner_confirmed_events` (new `event_type`; `confirm_path`, `price` = the failing close,
  `change_pct` = close vs confirmed_price) so the backtest feed and the grade read the same event;
- log `[CONFIRMED-QUICK-FAIL] SYM confirmed_at= confirmed_price= close= minutes= -> dropped for the day`.

Setting: `scanner_quick_fail_enabled: bool = False` (default OFF = byte-identical) · `scanner_quick_fail_window_minutes: int = 10`.
The bots (v2, momentum) are NOT changed: they simply never see the symbol because the scanner never hands it over.

## Why "close below the confirm price" and not "below the ATR line"

The ATR trail is not stored per bar (`strategy_bar_history.indicators` is `{}`), so the month's test used the scanner's
`confirmed_price` as the line. **The second backtest must test BOTH definitions** and report them side by side: (a) the
confirm price (what this spec builds, because the scanner has it at the moment it needs it) and (b) the rebuilt ATR trail
(the operator's literal words). If (b) is materially better, the spec changes to "the bot reports its trail to the scanner"
before anything is built.

## Evidence so far (claude-1, one month, Schwab v2, 158 round trips, 45 stock-days, 08-24 → 09-22; replay, not a backtest)

| | trades | winners | losers | win % | month sum |
|---|---|---|---|---|---|
| as traded | 158 | 83 | 75 | 53% | −50.8 |
| **quick-fail alone** | 116 | 68 | 48 | 59% | **+4.5** |
| 0.5% entry offset alone (deployed 09-23) | 128 | 83 | 45 | 65% | −24.4 |
| **offset + quick-fail** | **95** | **68** | **27** | **72%** | **+31.0** (9 up days / 6 down, worst −9) |

By time-to-first-close-below-confirm: ≤ 5 min 36% winners (n=39) · 5–10 min 33% (n=3) · 10–30 min 69% (n=13) · 30–120 min 33%
(n=9) · > 2 h or never 60% (n=94). Drop-one by name: the quick-fail group stays at 30–42% winners without any single name.
Skipped stock-days (14 of 45): LUCY 08-24, CELU + USDE 08-27, LIDR 09-01, LHAI 09-02, CHPT 09-03, FTFT 09-11, IPW 09-15,
QCLS + BENF 09-16, GIPR 09-18, NCPL 09-21, DCOY + RAIN 09-22. **Winners it forgoes:** QCLS +14, CHPT +8, LUCY +5.5.

## What the SECOND backtest (codex-2, independent) must do before building

1. Its own code, from `scanner_confirmed_events` (CONFIRM rows) + `strategy_bar_history` 1-min bars + `fills`, for the
   same month AND for 08-01 → 08-23 (a window I have not looked at — the pre-registered out-of-sample).
2. Report the table above for both definitions (confirm price / rebuilt ATR trail) and windows {5, 10, 15} min.
3. Count what the rule would have done to Webull legs too (I measured Schwab only).
4. Name the forgone winners; state the fixture matches production (`confirmed_fade_remove_below_change_pct` as deployed).
5. If the out-of-sample month does not show the same direction (quick-fail group ≤ 45% winners vs rest ≥ 55%), the rule is
   NOT built and this spec says so.

## What it does NOT change
Entry offset (live), target +5 / stop −8, retries (RETRY-ONE, boarded next), Webull mechanics, the bots' own logic.

## Deploy / pass mark
Flag ON on its own day, never with another flag. Pass mark over 5 sessions: `[CONFIRMED-QUICK-FAIL]` count per day, and for
each dropped name what our bots would have traded (the bot's own `[V2-CW-ARM]` still fires on unsubscribed data? — no: once
purged, the bot has no bars; so the grade is the STOCK's later path from bars, not our trades). UNEXERCISED until then.

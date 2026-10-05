# GAPKEEP1 - independent Step 0, 2026-10-05

## Verdict Before Any Build

**AGREE: APUS is an old, deliberate GAPHOLD reseed losing a continuous-line
BUY transition, not a regression from Saturday's installs.** Own tape census
reproduces 27 holds since September 28: nine with zero retained prints in both
feeds and eighteen with prints. That is a historical census, not yet a proven
decision-time market-silence classifier.

**DISAGREE that skipping the reset alone satisfies bar-for-bar chart parity;
UNMEASURED that the requested change is exit-neutral. Build STOPPED.** There is
also an older 90-second true-range gap clamp. Confirmation and ATR SELL exits
read the same ATR state that the card changes. A Schwab-only silence detector
would carry through two genuine missing-data holes. Those scope/safety issues
must be acknowledged before a source build. No GAPKEEP PR or install is claimed.

No production service, flag, subscription, database or ledger was changed.
OWNMIX1, RPGSTUCK1 and PMPRINT1/PMFLIP1 are independent released build lanes;
this assessment does not gate them.

## Own Sources And Method

- Code baseline: `a80b51816abf0aefc269f0fdfc473468fd3fe62c`.
- Own low-priority SSH read: 14:56:12Z, population frozen at 14:35Z (10:35 ET).
  Thirty-one retained v2 log files scanned in chronological order, line by line.
- SQL is read-only, with a five-second statement timeout per query. Tape counts
  use indexed symbol/event-time bounds; bars <=1,000/session; managed rows <=200;
  MI confirmation decisions <=20. No Redis reads, no snapshot-batches reads.
- The requested census bounds are `(last bar close, detection minus 10 seconds)`.
  The ten seconds is the reviewer's retrospective census bound, NOT a new live
  timeout or classification rule. Counts are NOT summed as unique trades:
  the two sources can contain the same market trade.
- Every positive census count was also checked with `received_at <= detection`:
  all eighteen positive cases were already received by at least one recorder.
  This does not prove that v2 retained them in decision memory.
- Bars are from `strategy_bar_history`, strategy `schwab_1m_v2`, interval 60.
  Pure oracle comparison uses period 5 / factor 3.5 / SMA5 seed and the same bars.
  Separate reset/carry-only experiments call the UNMODIFIED production ATR method.
  Those experiments use stored bar time, not a reconstruction of live delivery,
  watchlist history or restart state. Recovery-gap classification is deliberately
  NOT invented; those secondary holes reset in that experiment.
- Live v2 PID26811, start `2026-10-04 02:53:29 UTC`, NRestarts0; selected `/proc`
  read confirms `MAI_TAI_STRATEGY_SCHWAB_1M_V2_GAP_HOLD_ENABLED=true`. Period/factor
  5/3.5 and detect90 are effective Settings read from the deployed env, not
  separately present values in `/proc`.

Own immutable local raw directory:
`/Users/velkris/.codex/gapkeep1-evidence-20261005/`.

| Raw file | SHA256 |
|---|---|
| gapkeep1-own-evidence.json | 6d80817ada00a014f7b42bf0a61c07c701a7f1b561f5c52b6f5a55cec96d3b9b |
| gapkeep1-own-exit-evidence.json | 7e9b3da8ea72d309b78c85c17004c8afc0f1b6805794dda2929e00b752bacde0 |
| gapkeep1-own-replay.json | 844e4eba6556aee5ed14566379958afba8232383a8c1208f6a06cfb4c2f61644 |

The own pull/replay scripts are committed alongside this document. They must
NOT be used as a production classifier or a fill/outcome simulation.

## G1-G11 Assessment

| Claim | Verdict | Own evidence / limit | New Or Old |
|---|---|---|---|
| G1 APUS short before pause | AGREE | Oracle SELL09:05; actual09:33 probe close4.26, short, trail4.974365. Stored bars omit09:34-09:37. | Old GAPHOLD interaction, today's occurrence |
| G2 detect and reset | AGREE | Own09:38:44.721 hold: age284.7s, print age1.0s; `begin_gap_hold` calls reset unconditionally. | Since #1038 |
| G3 long seed/resume | AGREE | Actual09:47:02.291 reads bar09:46 long/trail4.005/close4.635; actual09:48:02.964 resumes after10 bars. | Intended fresh reseed |
| G4 continuous APUS BUY | AGREE, bar proxy only | Same-bar oracle SELL09:05, short across pause, BUY10:11; preceding short trail4.7951 gives4.8190755;10:11 low4.71/high5.04,next high5.44. Own frozen session has146 bars, not the supplied130; today's reset experiment has no10:11 BUY, while suppressing the initial silent reset restores that BUY in the mathematical experiment. | Lost signal, not proven broker fill |
| G5 every reseed starts long | AGREE mechanism; denominator differs | Own67 initial holds:65 have a later probe, all65 long;64 are same-day, one SDEV probe is next-day and excluded from same-session claims. Supplied62/62 is not my denominator. Oracle and production seed long explicitly. | Old seed design |
| G6 #1038 deliberate | AGREE | `23568d786e9ac331538fdb13b0943ec8706149d4`, merged09-23; blame binds both reset sites; original reseed and BENF tests unchanged/green. | Old, not10-03 |
| G7 nine/eighteen split | AGREE census; UNMEASURED live proof | 67 initial holds,40 on09-24,27 since09-28;9 zero/18 positive in dual retained tape, all9 RTH. Three premarket positive holes. There are also23 recovery-gap hold lines; those are not additional initial-hold population rows. | Existing operational holes |
| G8 three/three/three | AGREE state categories with limits | At first logged seed, CLRO/NXL/APUS long versus continuous short; DXST/AMOD/MI both long; KNRX/ZNB/JAGX have no same-day pre-hold probe. Lack of a probe is not proof of an empty in-memory indicator. JAGX's previous probe is09-25, not10-05. | New comparison, old reseed |
| G9 four continuous BUY transitions | AGREE oracle transitions; DISAGREE all are recoverable entries | CLRO10:16 trigger4.928118; NXL09:55 trigger9.154746 and10:51 trigger8.2995915; APUS10:11 trigger4.8190755. NXL09:55 lies INSIDE its existing wait, which does not resume until10:25. Card must continue to suppress it, without late trading. | Signals, not executions |
| G10 thirteen filled-entry bar comparisons | AGREE state/rounded line comparison | Own retained live probe at each of the13 bar timestamps matches continuous oracle state and trail to four decimals. Some entries rest while state is short; these are not13 BUY-flip bars. No claim that every future entry/exit survives. | Observed historical control |
| G11 one held position | AGREE log census | Exactly1/67 initial hold lines has held_qty>0: MI09:39:39.656,180. Ledger/ownership corrections are out of scope here. | Today's held-position exposure |

## Own 27-Hold Table

Kinds below reproduce the **offline** dual-tape census only. They do not assert
exchange halt status or prove uninterrupted feed coverage. Today's after-state
is the first same-day logged seed, not necessarily the resume bar; delayed and
backfilled bars are identified separately. The final column is the continuous
oracle at that seed's stored bar, a requested mathematical target, **NOT an
implemented or proven after-change result**. For BARS MISSING, the proposed
result must remain today's behavior, regardless of the oracle column.

| Date ET | Symbol / detection ET | Offline kind | Schwab / capture prints | Before, same-day log | After today | Continuous target at same bar |
|---|---|---|---:|---|---|---|
|09-28|CLRO09:42:46|MARKET SILENT|0/0|short|long|short5.5168|
|09-28|NAMI12:30:48|BARS MISSING|10/258|long|long|long2.1031; keep today|
|09-28|BNKK13:19:42|BARS MISSING|19/349|UNMEASURED|long|long1.7679; keep today|
|09-28|EGG13:39:33|BARS MISSING|0/10|short|long|short4.3263; keep today|
|09-28|KNRX14:01:17|MARKET SILENT|0/0|no same-day reading|long|existing seed required; full-history oracle long0.996 is not evidence of a restored live line|
|09-28|EGG14:06:33|BARS MISSING|1/13|short|long|short4.3147; keep today|
|09-28|EGG14:36:57|BARS MISSING|0/18|short|long|short4.4643; keep today|
|09-28|EGG14:49:34|BARS MISSING|1/23|long|UNMEASURED, no later probe|UNMEASURED; keep today|
|09-29|DXST10:34:09|MARKET SILENT|0/0|long|long|long3.7691; line-level drift matters|
|09-29|PFSA15:35:34|BARS MISSING|27/2829|long|long|long1.7396; keep today|
|09-29|SDEV15:38:35|BARS MISSING|20/919|long|UNMEASURED same-day|next-day probe cannot certify this session; keep today|
|09-30|VBIO08:53:34|BARS MISSING|46/1967|long|long|long3.1165; keep today|
|09-30|TGE09:39:31|BARS MISSING|6/183|short|long|long1.1652; keep today|
|09-30|CMCT10:25:31|BARS MISSING|31/3112|UNMEASURED|long|long2.4459; keep today|
|09-30|MSGY13:21:09|BARS MISSING|32/889|long|long from later/backfilled bar|UNMEASURED target; keep today|
|09-30|CMCT13:28:35|BARS MISSING|3/27|long|long|long2.5295; keep today|
|10-01|MEDS09:32:42|BARS MISSING|11/45|short|long|long3.9955; keep today|
|10-01|NXL09:40:25|MARKET SILENT|0/0|long|long|short8.9606; secondary holes require classification|
|10-01|CMCT15:20:48|BARS MISSING|5/42|short|long|long3.6335; keep today|
|10-02|AIXI08:07:55|BARS MISSING|4/176|UNMEASURED|long|long1.5714; keep today|
|10-02|AMOD09:46:39|MARKET SILENT|0/0|long|long|long3.5192|
|10-02|ZNB11:51:29|MARKET SILENT|0/0|no same-day reading|long|existing seed required; full-history oracle long1.0307 not proof of prior live line|
|10-02|ZNB12:20:48|BARS MISSING|20/1103|long|long from earlier backfill|long0.9267 at delivered bar; keep today|
|10-05|SDEV07:14:17|BARS MISSING|27/1646|UNMEASURED|long|long10.6401; keep today|
|10-05|APUS09:38:44|MARKET SILENT|0/0|short|long4.005|short4.9744|
|10-05|MI09:39:39|MARKET SILENT|0/0|long|long3.690977 at10:22|long3.5717; exit proof below|
|10-05|JAGX09:57:55|MARKET SILENT|0/0|no same-day reading|long3.296070, bar10:08 delivered10:32|existing seed required; oracle long3.2961|

## Decision-Time Signal And Its Proof

**Historical proof:** dual-tape counts give9/18. Restricting positive rows to
received-by-detection leaves9/18. **Schwab-only counts give11/16, WRONG**:
EGG13:39 has0/10 and EGG14:36 has0/18. Each capture count was already recorded
by the hold detection time. The difference is feed coverage, not later lookahead.

**Current decision-memory proof: UNMEASURED / absent.**
`schwab_1m_v2_bot.py` stores only `_gap_last_print_at_ms`, updated BEFORE
`_evaluate_gap_holds`. The ending print overwrites evidence about earlier
prints. It retains neither a per-hole print count nor a continuous-coverage
epoch across a disconnect/restart/watchlist addition. Even adding the prior
Schwab timestamp cannot distinguish the two EGG missing-data cases safely.
The independent capture recorder is not currently a v2 decision-memory input.

Proposed safe signal for review, not built: retain bounded per-symbol event
history or equivalent per-bar summary from the live inputs with event AND
receive times, a last-bar-close anchor, the first returning-print boundary,
and a subscription/connection coverage epoch. Any received print inside the
hole proves BARS MISSING. MARKET SILENT requires zero prints across the proven
covered interval on the relevant independent inputs; incomplete coverage,
restart, overflow or unavailable independent evidence means CANNOT TELL and
today's reset. Multiple prints in the return burst must not erase the saved
first-return boundary. No DB query, retroactive tape read, hole-length-only
heuristic, or new live timeout is proposed.

The exact reviewed coverage contract and sufficient live input have not yet
been demonstrated. Consequently I cannot honestly claim a decision-time
9/18 replay PASS from the data the current bot retains. A latest-print-only
implementation is DISAGREE/unsafe and will not be built.

## Mathematics And Scope Disagreement

Besides `begin_gap_hold` and `_prepare_gap_hold_bar` resets, production
`_update_atr_state` has `_ATR_MAX_BAR_GAP_MS=90000`: a longer bar gap uses
`tr=hilo`, NOT `max(hilo,href,lref)` over the prior close. The oracle always
uses the latter. This is a deliberate older July30 outage safeguard, not
GAPHOLD and not Saturday's regression.

Own DXST09-29 10:42 example: suppress only the initial silent-hole reset and
leave production math untouched => long/trail3.84088115295. Same stored bars,
continuous oracle => long/trail3.7691. Today reseed's actual first reading is
long/trail3.72968. Three long states are NOT line parity. APUS happens to
converge at the requested10:11 transition; that does not certify all pauses.

Two versions therefore differ: the card's **exact continuous-oracle line**
versus **preserve current production gap mathematics, omit reset only**.
Meeting the former requires an explicitly silence-scoped true-range decision,
while preserving the old clamp/reset for missing/unknown data. That expands
the mathematical change beyond reset removal and requires acknowledgement.
No global gap-clamp removal, duration-only inference or bars-missing carry.

Also, resetting `atr_fired_in_short_seg` and entry ownership at hold detection
is a separate existing action. A future carry fix must preserve already-fired
segment ownership and consume flips during the unchanged wait. NXL09:55 is
still forbidden inside the wait, not a delayed buy after10:25.

## MI Exit Reading (K-T7)

Own live row entered09:34:33.120513,180 at3.31. Before pause, actual09:34 bar
state long/trail2.220674. At09:39:39 the hold records held_qty180; the next
stored bar is09:39. The confirmation target is **09:35**, timestamp
1791207300000, and that bar is absent. Actual09:40:03.449 logs
`V2-CONFIRMATION-EXIT-UNANSWERABLE ... reason=target_bar_missed` for fill
24d3443e-3014-4f35-b34a-e6c040eabe8f. Own durable confirmation query returns
**zero** MI evaluation rows before the frozen10:35 population cutoff.

For THIS missing target, both today's and carry versions have no09:35 bar:
no computed negative may be invented, and no confirmation exit PASS is claimed.
The continuous09:39 bar is long/trail2.2207, not a SELL flip. At10:22, today's
actual seed is long/trail3.690977; continuous line is long/trail3.5717. MI's
oracle has no SELL after the09:34 BUY through the frozen window. This supports
no new MI ATR SELL signal in that window, not a general guarantee for holds.
Secondary MI recovery gaps are not yet safely classified for a live replay.

**General exit-effect scope remains UNMEASURED, not neutral by assertion:**
`_handle_bar` copies the strategy ATR state into CONF1 and calls
`evaluate_bar`; `_evaluate_completed_bar` still calls `_observe_atr_sell`
while gap hold blocks entries. A preserved short state, changed trail or
changed SELL transition can therefore alter exit evidence without any OMS
source edit. This must be shown and ruled on before a GAPKEEP build. The real
MI broker/ledger ownership mismatch remains OWNMIX1's separate issue.

## G10 Control Table

First live probe per named entry bar, compared with the same-bar oracle.
All thirteen states and trails agree to four decimals. Rounded historical
agreement is not a test of exact floating point parity or a fill counterfactual.

| Session / symbol | Bar ET | Actual / oracle state | Actual trail / oracle trail |
|---|---|---|---|
|09-28 CLRO|12:42|long/long|4.845062/4.8451|
|09-29 DXST|12:45|long/long|2.676015/2.6760|
|09-30 TGE|11:54|long/long|1.311998/1.3120|
|09-30 TGE|12:48|long/long|1.333992/1.3340|
|09-30 TGE|14:36|long/long|1.398575/1.3986|
|09-30 TGE|15:27|long/long|1.460924/1.4609|
|10-01 NXL|11:42|long/long|7.178548/7.1785|
|10-01 NXL|13:46|long/long|6.870396/6.8704|
|10-02 AIXI|11:05|short/short|1.734088/1.7341|
|10-02 AIXI|15:17|short/short|1.832881/1.8329|
|10-02 AMOD|11:22|short/short|3.500966/3.5010|
|10-02 AMOD|12:26|long/long|3.246596/3.2466|
|10-02 AMOD|15:43|long/long|3.282339/3.2823|

## Build And Install Disposition

Assessment branch only: `codex/gapkeep1-independent-assessment`. No source
change, fix PR, flag, deployment or install plan authorization. GAPHOLD focused
suite **8 PASS**, including the original reseed and BENF tests, untouched.
No new-source full-suite pair is claimed because there is no source build.

Before build: acknowledge the decision-memory/independent-feed proof limit,
silence-scoped mathematical change needed for exact oracle parity, and exit
evidence impact. Then test all K-T1..K-T10 on recorded inputs with event/receive
timing and coverage, preserve the original missing-data tests, test second
gaps/consumed flips, replay both exit readers, run the full paired suite, and
seek exact-head pin. A future v2-only install requires a reviewed single
evening plan and exact-SHA GO; none follows from this assessment.

# KEEPREST1 independent Step 0 - 2026-10-06

Result: **DISAGREE with the IMCC 09-23 08:17 acceptance classification.**
Implementation stopped under the operator's instruction to stop on disagreement.
This is not a disagreement with the requested frozen-price card for a real BUY
flip. No runtime code, settings, tests, production state, or shared handoff was
changed. No PR or ready claim is issued.

Branch: `codex/keeprest1-frozen-buy-after-flip`.
Base: `3ebde364d4634fdad45992e2ab1cbdf43ffeb221`.
Managed worktree: `/Users/velkris/.codex/worktrees/keeprest1-frozen-buy-after-flip/project-mai-tai`.
Commit marker installed with `scripts/install_commit_agent_marker_hook.sh codex`.
Production checkout read at 2026-10-06 14:59:15 UTC:
`7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`.

## Findings

| Claim | Assessment | Independent evidence |
| --- | --- | --- |
| 18 first-slot flip-no-fill take-downs from 09-22 through 10-06 | AGREE | 18 primary cancel/disarm records; Webull mirror cancellation records excluded from count. |
| OLOX 10-06 had a BUY flip followed by take-down of the 1.4425 software rest | AGREE | BUY arm logged 08:49:02.638 ET for the 08:48 bar; take-down 08:50:02.627 ET; next logged SELL disarm 09:31:02.692 ET. |
| OLOX first later crossing at 09:13 and executable fill | UNMEASURED | No bounded tick or quote extraction for that crossing completed before the stop. Stored bars 08:46-08:51 and the arm/cancel/SELL logs were read. |
| CLRO 09-28 had a BUY flip followed by take-down of the 5.5590 software rest | AGREE | Rest armed 07:20:02.974 ET; BUY arm 07:25:01.607 ET for the 07:24 bar; take-down 07:26:02.405 ET. |
| CLRO later crossing at 07:51 and executable fill | UNMEASURED | No tick/quote extraction for that crossing completed before the stop. |
| IMCC 09-23 08:17 was a no-fill take-down after a BUY flip | DISAGREE | ATR was SHORT, `flip=none`, when disarmed; actual BUY flip came at 08:25:01.262 ET for the 08:24 bar. |
| IMCC later price crossing at 08:24 | AGREE as a price observation | First stored Schwab trade at/above 8.0525 after the 08:17 take-down: 08:24:38.919 ET, price 8.09, size 150. This predates the confirmed BUY flip. |
| IMCC replay buys at 08:24 and takes a -8% exit with no duplicate | UNMEASURED | No implementation or replay undertaken after disagreement. A trade print is not proof of executable buy or exit. |
| 14 later crossings, nine within one minute, 10 +5% first / two -8% first / two neither, four never reached | UNMEASURED | Population enumeration completed; outcome and same-segment classification did not. Cannot treat all 18 reason labels as proof of a BUY flip. |

## Decisive IMCC evidence

Source: `/var/log/project-mai-tai/schwab-1m-v2.log-20260924.gz`.
Log timestamps below are UTC; subtract four hours for ET.

```text
2026-09-23 12:15:41,242 [V2-RESTING-EH-CROSS] IMCC px=8.0897 >= level=8.0525 -> marketable EH-LIMIT buy (cap=8.0928 band=0.50%)
2026-09-23 12:15:41,243 [V2-FANOUT-SLOT-BOUND] IMCC source=eh_resting slot=resting slot_id=c175e730-830e-591a-8e7e-34b80b0115b1 segment_id=1790165222399 bound=1
2026-09-23 12:15:44,608 [V2-FANOUT-OUTCOME] IMCC slot_id=c175e730-830e-591a-8e7e-34b80b0115b1 outcome=queued held=0 evidence_positive=0 could_not_tell=1
2026-09-23 12:17:02,449 [V2-ATR-PROBE] sym=IMCC ts_ms=1790165760000 close=8.000000 high=8.080000 low=7.800000 tr=0.280000 loss=2.120132 trail=8.032637 state=short age=13 touch=false flip=none vol=156813 fired_seg=true
2026-09-23 12:17:02,449 [V2-RESTING-EH-DISARM] IMCC slot=first reason=flip_no_fill_soft_rest resting_below_floor_bars=0 level=8.0525
2026-09-23 12:18:02,452 [V2-RESTING-EH-ARM] IMCC slot=first soft-rest at level=8.0326 (EH; broker stop dead, watching quotes)
2026-09-23 12:25:01,262 [V2-ATR-PROBE] sym=IMCC ts_ms=1790166240000 close=8.210000 high=8.350000 low=7.400000 tr=0.840000 loss=1.974400 trail=6.235600 state=long age=0 touch=false flip=BUY vol=255138 fired_seg=true
2026-09-23 12:25:01,262 [V2-CW-ARM] IMCC armed bar_ts=1790166240000 trig=8.3500 flip_level=8.0326
2026-09-23 12:26:02,372 [V2-RESTING-EH-DISARM] IMCC slot=first reason=flip_no_fill_soft_rest resting_below_floor_bars=0 level=8.0326
2026-09-23 12:38:02,217 [V2-CW-DISARM] IMCC reason=flip
```

The read-only database query found zero `schwab_1m_v2` broker order rows for
IMCC between 12:14 and 12:27 UTC. That does not establish the reason the earlier
software draft did not reach the broker; intent/refusal detail remains unmeasured.
It does establish that a later executable broker fill cannot be claimed from
these reads.

## Enumerated Population

Times are take-down timestamps in ET. A historical `level` is reported as logged;
no current 0.5% offset is retroactively applied to it.

| Date | Symbol | Take-down ET | Logged trigger or legacy level | Session |
| --- | --- | --- | --- | --- |
| 2026-09-22 | TOPS | 09:30:03.100 | level 1.3490 | software rest at boundary |
| 2026-09-23 | IMCC | 08:17:02.449 | level 8.0525 | software |
| 2026-09-23 | IMCC | 08:26:02.372 | level 8.0326 | software |
| 2026-09-23 | IPDN | 08:40:03.217 | level 6.8167 | software |
| 2026-09-23 | WHLR | 08:47:03.088 | level 5.0802 | software |
| 2026-09-23 | IPDN | 11:51:02.633 | level 5.3820 | broker |
| 2026-09-24 | WETO | 09:24:00.624 | 2.0778 | software |
| 2026-09-24 | GLND | 12:07:02.479 | 4.4392 | broker |
| 2026-09-24 | NCPL | 13:44:01.410 | 1.4559 | broker |
| 2026-09-24 | GLND | 14:38:02.507 | 4.5955 | broker |
| 2026-09-24 | PMAX | 15:49:02.934 | 1.6753 | broker |
| 2026-09-28 | CLRO | 07:26:02.405 | 5.5590 | software |
| 2026-10-02 | AIXI | 12:32:02.857 | 1.6897 | broker |
| 2026-10-02 | AIXI | 13:54:02.921 | 1.7866 | broker |
| 2026-10-05 | SAIQ | 08:12:02.425 | 6.7928 | software |
| 2026-10-05 | VEEA | 08:34:02.972 | 5.2613 | software |
| 2026-10-05 | MI | 09:16:02.367 | 2.7288 | software |
| 2026-10-06 | OLOX | 08:50:02.627 | 1.4425 | software |

## Read Bounds and Limitations

- SSH alias `mai-tai-vps`, batch mode, initial connection timeout 10 seconds.
- Selected rotated logs dated 20260923-20261006 and the current log only.
- Decompressed scan cap 45,000,000 characters per selected log, result cap 150
  matching reason records. No full log downloads or Redis reads.
- Named-case detail reads restricted by symbol and time; one detail extraction
  hit its output cap and was narrowed before any result was used.
- PostgreSQL reads used `BEGIN READ ONLY`, local statement timeout five seconds,
  bounded symbol/time predicates, explicit `LIMIT`, and `ROLLBACK`.
- Schema read limited to relevant public tables. No environment contents or
  credentials were printed; no database, service, broker, or flag writes.
- Code inspected at the pinned base: `_cw_v2_resting_track` shares the
  `resting_flip_ms` grace latch with the software-cross submission path. Thus
  `flip_no_fill_soft_rest` alone cannot distinguish confirmed BUY-flip expiry
  from an earlier software draft's fill-settle expiry.

## Parent Receipt

Lane A only. Step 0 base head is
`3ebde364d4634fdad45992e2ab1cbdf43ffeb221`; report commit can be identified with
`git rev-parse HEAD` on this branch.
Evidence report ETA supplied: **2026-10-06 12:00 ET**.
Remaining blocker count: **1**, reconcile the IMCC acceptance classification and
population definition before implementation. Build ETA is held pending that
reconciliation; this is not a ready head.

The requested deploy default is ON with a rollback switch. Code default,
expected composed flags, dual-session implementation, RPG freeze, mutations,
full unit failed-name pair, and both Validate runs remain unimplemented and
unverified because the required Step 0 stop precedes that work.

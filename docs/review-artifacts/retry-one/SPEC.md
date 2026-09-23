# SPEC — RETRY-ONE: any close of the first try hands the flip back; at most one retry per name per day (operator 2026-09-23)

**Written by `claude-1` 2026-09-23 ~16:15 ET. Operator: "when you have a hard stop it should reset… every time it makes
sense… go ahead and write up to codex, let's build now." For `codex-2` to build now (flag default OFF); deploy on its own
day; codex-2's own causal backtest of the reset-on-any-close version is part of the build, not a gate before it.**

## Two named cases, one on each side

**The miss — VSA 2026-09-23 15:50 ET.** Rest at the line 3.859. 15:29: the Webull mirror filled 1 share at 3.87, Schwab did
not fill. 15:30: the software hard stop closed the Webull leg at 3.56 (−7.7%). The pre-flip close evaluator found no
confirmation-exit close, so it marked the flip **`consumed`** (`[V2-FLIP-OWNER-PREFLIP-CLOSE-EVALUATED] … consumed=6
entry_allowed=0 confirmation_closed=0`, 15:30:47). 15:50: the real flip crossed the same line (chart and bot agree, the
bot armed at 15:51), but the only path left was the reclaim slot, disabled since August ⇒
`[V2-FLIP-OWNER-ADMISSION] … allowed=0 reason=non_first_producer_disabled` at 15:53 and 15:54. VSA went 3.86 → 4.43 in
three minutes. Zero shares on either broker; the day's VSA result is the −7.7% stub only.

**The loop — DCOY 2026-09-22.** Three tries on one level (10:53 −3.1%, 11:03 −1.8%, 11:52 −8.0%); RAIN the same day, five
tries. The confirmation-exit reset (09-17) hands the flip back every time, with no count.

## What the code does today (`strategy_core/schwab_1m_v2.py`)

`_evaluate…preflip_close` (~:1575): when the first-rest position closes before the flip completes, it asks
`_flip_owner_confirmation_closed(...)` — did EVERY sibling row close by a **confirmation exit**? Yes ⇒
`_retire_flip_owner_opportunity(reason="confirmation_exit_closed_all_siblings_before_flip")` = **released**, the next
cross may rest again. No ⇒ `flip_owner_phase = "consumed"` = **locked** until a new ARM. A hard stop, a floor, a flip
exit or the broker's own bracket leg all land in "no".

## The rule

1. **Any close of the first try resets the flip.** In the pre-flip close evaluator, `confirmed_close` becomes
   `closed_by_any_exit`: every sibling row of the opportunity is CLOSED (qty 0) — by confirmation exit, `CW_HARD_STOP`,
   `CW_FLOOR`, `CW_FLIP`, or a broker bracket leg (`[OMS-V2-OCO-RESOLVED-FLAT]`). The proof source is the same durable
   close feed the confirmation reset already reads, widened to every exit reason; a row still open ⇒ no reset (unchanged).
   `_retire_flip_owner_opportunity(reason=f"first_try_closed_{exit_reason}")`.
2. **At most one retry per name per trade date.** A per-symbol, per-day counter `first_try_closes` incremented on each
   reset. The reset fires only while `first_try_closes < 1 + retry_one_max_retries` (setting, spec: **1**). The second
   close of the day does NOT hand the flip back: `flip_owner_phase = "consumed"`, reason `retry_budget_exhausted`, and the
   name stays out until the 04:00 ET roll. Log `[V2-FLIP-OWNER-RETRY] SYM closes_today= retries_left= action=released|held`.
3. **A retry needs a fresh cross.** The reset only re-enables the resting order; it never emits on the cross that just
   stopped us. (Unchanged: a rest exists only while the line is above price — the KXIN 09-17 gap where the real flip closes
   within one bar of the reset is NOT solved here and stays a known limit.)
4. Settings: `strategy_schwab_1m_v2_retry_one_enabled: bool = False` (byte-identical), `..._retry_one_max_retries: int = 1`.

## What it would have done
- VSA 09-23: 15:30 stop ⇒ reset (closes_today=1, retries_left=1) ⇒ 15:50 cross rests and fills ~3.86 ⇒ target 4.05 in
  3 min ≈ +5% both brokers (instead of −7.7%).
- DCOY 09-22: 10:55 conf exit ⇒ reset (1 left) ⇒ 11:03 fill, 11:05 conf exit ⇒ **held** ⇒ no 11:52 entry (−8.0 avoided).
- RAIN 09-22: tries 3, 4, 5 not taken.
- Month numbers (fill-based, 126 trips, 09-08→09-22): trip#2 break-even (+21 sum), trip#3+ −83 sum; "one retry" removed
  net −55.6 forgoing 19 winners. **These were counted with the confirmation-only reset in force; the reset-on-any-close
  version has NOT been counted — that is codex-2's causal backtest below.**

## Tests codex-2 writes (behavioural, on the real strategy path + the OMS close feed)
1. Control: enabled=False ⇒ byte-identical: a hard-stop close still reads `consumed`, a confirmation close still releases.
2. VSA shape: Webull-only fill, CW_HARD_STOP close 60 s later ⇒ `released`, next cross rests on BOTH legs.
3. Broker-bracket close (`OCO-RESOLVED-FLAT`) of the first try ⇒ released.
4. One sibling still open ⇒ NOT released (unchanged safety).
5. Budget: second close of the day ⇒ `held`, `retry_budget_exhausted`; the 04:00 roll clears it; `max_retries=2` ⇒ third.
6. DCOY replay from the stored closes ⇒ exactly one retry, third try refused.
7. Mutations: reset on a still-open sibling ⇒ test 4 RED; counter not incremented ⇒ test 5 RED; reset emitting on the
   same cross ⇒ a "no emit without a fresh cross" test RED.

## codex-2's causal backtest (part of the build; reported with the PR)
Own code, `oms_managed_positions` closes + fills + bars, 08-24→09-23, both brokers: apply reset-on-any-close with
max_retries ∈ {0, 1, 2}, causally (a retry counts only if a fresh cross occurred after the close), report trades / winners /
losers / sum per setting, name the forgone winners, and the VSA + DCOY replays. If max_retries=1 is not better than the
confirmation-only reset on that window, say so in the PR; the operator decides.

## Deploy / grade
Flag on its own day (queue: offset 09-23 → C → GAPHOLD → RETRY-ONE, unless the operator reorders). Grade over 5 sessions:
`[V2-FLIP-OWNER-RETRY]` counts released vs held, and the P&L of every retry taken, by name.

# P7 — what a study grouped on `cw_arm_bar_ts` can actually see

**Status:** audit. Written 2026-08-19. No code change.

> ## ⛔⭐⭐ THE RULE
> **First-vs-reclaim keys on `cw_entry_n`, never on `cw_arm_bar_ts`.**
> `cw_entry_n` is present on **97%** of open fills. `cw_arm_bar_ts` is present on **53%**, and the
> missing half is **structured by leg** — so grouping on it does not just shrink a study, it
> **re-weights** it.

---

## Measured — `live:schwab_1m_v2` + `live:orb`, 2026-08-01 → 08-19, 421 open fills

| leg | fills | with `cw_arm_bar_ts` | |
|---|---|---|---|
| schwab primary — **resting** | 127 | **26** | **20%** |
| schwab primary — reactive | 49 | 49 | **100%** |
| orb `reactive` | 90 | 90 | **100%** |
| orb `rth_resting` | 107 | 51 | 48% |
| orb `eh_resting` | 26 | **0** | **0%** |
| **all** | **421** | **223** | **53%** |

Not historical drift — it is ~50% **every day** across the window.

⇒ A study grouped on the segment id **over-represents reactive entries** (100% present) and
**excludes extended-hours resting entries entirely** (0% present). Since reclaim behaviour is
exactly what such studies compare, the bias lands on the variable under test.

## ⛔ This is BY CONSTRUCTION and already documented — it is not a bug to fix

`docs/v2-fresh-flip-since-confirmation-design.md` §7 states it plainly: *"`cw_arm_bar_ts` is 0 on
resting orders **by construction**; `cw_entry_n` is stamped but never incremented on that path."*
The resting order is built before the arm stamp is available to it, so the payload carries 0.

⛔ **The STATE field is fine — only the PAYLOAD carries 0.** Verified against every retained log:
**0 of 1621 `[V2-CW-ARM]` lines carry `bar_ts=0`.** The in-memory `state.cw_arm_bar_ts` is always
stamped at arm.

## ⛔ A LEAD I FOLLOWED AND REFUTED — recorded so nobody re-derives it

`_cap_reconstructed_segment` (and the replay's equivalent) gate on `0 < st.cw_arm_bar_ts <= watch_start`.
Knowing that resting orders carry 0, the obvious hypothesis is: **the seed-cap can never fire on a
resting segment, which is the live default — and that is why it "never ran at all" for CAST.**

**It is false.** Those guards read the STATE field, not the payload, and the state field is never 0
(0 of 1621 arms). The cap has fired **35 times against 320 db-seed hydrations** — a working guard on
a narrow population, not a dead one. The CAST miss needs a different explanation.

⇒ Payload and state share a name and diverge in value. Any future claim about `cw_arm_bar_ts` must
say **which of the two it means**.

## What this does NOT quantify

`docs/v2-entry-count-and-exit-poll-design.md` says the resting-path entry **under-count** is
unquantifiable from recorded data and instructs: *"do not put a number on it."* That still stands
and nothing here changes it. **Segment-id COVERAGE and entry UNDER-COUNT are different quantities:**
coverage is countable (the field is either present or not); the under-count is not, because the
missing increments leave no trace. This document measures the first only.

## Consumers — who inherits the 53%

| consumer | affected? | why |
|---|---|---|
| `ops/health/trade_recorder.py` | **YES** | records `cw_arm_bar_ts` per trade, so every study built on the recorder inherits the gap and its leg-structure |
| any ad-hoc first-vs-reclaim grouping | **YES** | this is the case the rule at the top exists for |
| `services/schwab_1m_v2_bot.py::_cap_reconstructed_segment` | no | reads the STATE field, always stamped |
| `backtest/replay.py` watch-start cap | no | same — STATE field |

## ⛔ A §82 cross-connection found while auditing this

`docs/v2-fresh-flip-since-confirmation-design.md` §7 also records: *"Phantom close
(`SPURIOUS-no-shares-ever-held`) gates on the UNION qty, never `held_qty`; it re-arms
`fanout_webull_claimed`, **permitting a second Webull leg per flip**."*

That is a **THIRD** route to the §82 duplicates, alongside the two found from the tape (the reactive
path ignoring the latch — fixed in #739 — and the claim expiry re-opening on `position_qty == 0`).
It was already written down. It belongs on §82's board entry so the item is not closed on two of
three causes.

## Pointers

`docs/v2-fresh-flip-since-confirmation-design.md` · `docs/v2-entry-count-and-exit-poll-design.md` ·
#570 (segment identity) · #739 (§82 latch) · [[project_mai_tai_v2_entry_segment_identity]]

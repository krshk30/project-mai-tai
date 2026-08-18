# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> **Target: ~150 lines.** To onboard an agent: *"Read `docs/session-handoff.md`."*

> **⛔⭐ OUTPUT CONSTRAINT (every study): report per-trade %, MEDIAN-FIRST, with a drop-one.
> NEVER a bare dollar total.**

---

# 🚨 TODAY'S FIRST ACTION — 08:11–08:26 ET, DO THIS BEFORE ANYTHING ELSE

**#710 shipped the CORE→ALL_DAY session fix at 16:06 ET 08-17. It is UNEXERCISED.** The only thing
that can prove it is a **pre-market Webull fan-out fill**, and the window is ~08:11–08:26 ET
(pre-market `live:orb` fills run 8/3/9/4/5/1 per session). **Miss it and you wait a day.**

**THREE-PART ACCEPTANCE — part 1 alone is NOT proof.** The failure that looks like success: the pair
is accepted (200), rests, and its legs are inactive until 09:30 — a clean `ATTACHED` line protecting
nothing.

```bash
# 1. did it attach, and with which session string?
sudo grep -E "WEBULL-PROTECT-(ATTACHED|RETRY|FAILED)|WEBULL-EXIT-PAIR-REFUSED" \
  /var/log/project-mai-tai/oms.log | cut -c1-260      # lines carry session= only AFTER #710
```
2. **READ THE SINK.** Take the base coid from `[WEBULL-PROTECT-ATTACHED]`, derive `<base>T` /
   `<base>S` via `_combo_leg_coid`, and fetch each leg with Webull `OrderDetailRequest`
   `.set_client_order_id(...)` — are they **WORKING before 09:30**? Our log says what we SENT; only
   Webull says what is ARMED.
   ⛔⭐ The legs are **not discoverable** (no symbol-keyed tree to walk) but they **ARE addressable
   and readable by deterministic coid**. The old "unqueryable" wording nearly caused this check to
   be skipped as impossible.
3. **On the exit: WHO did it** — the native pair or the software ladder? If the ladder exits every
   pre-market position again, the attach is decoration regardless of parts 1–2.
4. **The operator's own Webull screen ~08:30.** His screen outranks our logs (standing rule).

⛔ **A failure is as informative as a pass** — #707's `[WEBULL-EXIT-PAIR-REFUSED]` captures the full
payload + broker response either way. Log all three parts regardless of outcome.
⛔ **n=0 fills is a NON-RESULT, not a pass.** State the fill count with the verdict.

## 📋 THEN, IN THIS ORDER (operator-set 08-17 night)
**acceptance read → ~~§49~~ ✅ *(killed 08-18, see below)* → R4 wiring → R1 → §31 → §32 → §1 → §2 → §3.**

---

## ⚡ FIRST SCREEN

**PRE-OPEN PASS RUN 2026-08-18 06:40 ET — GREEN.** **7/7 services running** (enumerated, not queried
by name). **Nothing restarted overnight** — OMS still up from 17:50 ET Mon, carrying #714. Account
**flat**: 0 open managed rows, broker book empty. Market data **flowing** (latest bar 06:40 ET, 240
rows/30m); `market-data.log` silent since 21:53 ET is a **quiet logger, not a freeze** — confirmed
against the bar table. v2 watchlist live and rotating (`count=3 sample=PFSA,SGLY,XOS` 06:37 ET).
Fan-out **armed**: `DUAL_BROKER_FANOUT_ENABLED` · `WEBULL_MIRROR_EH_ENABLED` · `NATIVE_BRACKET_ENABLED`
all true, fan-out qty **1**. Infra: redis PONG **1.11G/2.00G ⚠️ `allkeys-lru`** · postgres 19 conns ·
disk 23% · load 1.24.

**Box files = `d9d84de`; `src/` diff vs `origin/main` = 0 files** (main has since advanced on
docs-only commits). ⛔ The **running OMS process carries `70ca930`'s code** — started **17:50 ET Mon**,
**no restart since**. The only `src/` delta is #715's `backtest/broker_refusal.py`, which the OMS
never imports ⇒ **functionally identical for the OMS, but do not call the process "d9d84de".**
Verified BY CONTENT, not by hash: `BROKER-SYNC-UNREADABLE` ×1, `SchwabPositionsUnavailable` ×5,
`_EXIT_PAIR_SESSION_EXTENDED = "ALL_DAY"` ×1, `WEBULL-EXIT-PAIR-REFUSED` ×1.

### 🔬 INSTRUMENT CHECK on today's acceptance read — the watch is NOT blind
✅ The grep **fires on a known-bad tape**: 40 matches in `oms.log-20260815.gz`, 11 on 08-17 carrying
the real refusal text. ✅ **Rotation is safe** — today's window (11:00–13:30 UTC) sits entirely in the
current `oms.log`, opened 00:00 UTC. One file; no `.log*` glob needed.
⛔ **`session=` is UNEXERCISED, not failing.** Zero lines carry it, but the **denominator is zero**:
all 11 attach lines are from 08:26 ET Mon, four hours *before* #710 deployed at 16:06 ET, and there
have been **0 since**. Absence proves nothing here.

### Shipped + deployed today — FIVE PRs, all verified by content
| PR | what | outage | exercised? |
|---|---|---|---|
| #706 | 3 guards on the Webull attach + `RoutingBrokerAdapter.fetch_quotes` | 3s | ⛔ **0/0/0 across the WHOLE retained window** — `ABANDONED`/`COALESCED`/`UNPLACEABLE`, and `ATTACHED` is **0** too |
| #707 | `[WEBULL-EXIT-PAIR-REFUSED]` payload logging · retry horizon 4.0s→~29s | 10s | ✅ **validated — it delivered the root cause** |
| #709 | broker account on every `[V2-OCO-EMIT]` line | 15s | pending next bracket |
| #710 | **pre-market pair tagged `ALL_DAY`, not `CORE`** | 3s | ⛔ **UNEXERCISED — top of this file** |
| #714 | **L1+L2+L3 — a failed Schwab positions read never reads as flat** | 4.4s | ✅ **PROVEN by forced injection** |

**🕐 OPEN PRs: none.** #711/#712/#713/#715/#716/#717/#718 all merged; every one is docs- or
test-only and **none is deployed or needs a restart**.

## ⭐⭐ THE ROOT CAUSE — found 08-17, closes a week of wrong answers
Webull validates a **`CORE`-tagged order against the CORE reference — the PRIOR CLOSE** — not the
live extended-hours tape. Pre-market on a gapper that is fatal:
```
IVF 08-17 08:26 ET — bought 2.5300, stop 2.40, IVF prior close 0.9716
  -> 5x 417 "The stop price of the stop-loss order should be lower than the current market price."
```
Our stop **was** below our entry. It was not below the prior close. Per-fill cross-tab over 6
sessions: **100% of refusals are pre-market; every RTH fill got its bracket.**

⛔ **Broker-verified enum on the v3 COMBO endpoint:** `CORE 200 · ALL_DAY 200 · ALL 417 · NIGHT 417 ·
Y 417`. **`ALL` is valid for a SINGLE-LEG order and REFUSED on the combo.** Do NOT "correct" it.
⛔ **`preview_order` does NOT validate position backing** (it 200s while flat) ⇒ Probe W4 only ever
proved the shape PARSES, never that it PLACES. Two comments still say "BROKER-PROVEN" (item 8).

## 🔴 OPEN THREADS — detail in [`handoff-open-items.md`](handoff-open-items.md)

### 🔴 TIER A — can cost money now
1. **⭐⭐ THE ACCEPTANCE READ** — top of this file. Nothing else runs first.
2. **THE RETRY BOUND IS STILL UNREACHABLE** — `_v2_exit_close_failures` resets on every positively-
   HELD read. Untouched by everything shipped today. Needs a restart ⇒ deploy window.
3. **PRE-MARKET IS 0% BRACKETED** — 14d: PRE 0/34 orb, 0/13 schwab. **#710 is the first real attempt
   at this.** Schwab still refuses stop legs in EH; #647 Gate 2 (built, dark) is its only answer.
4. **§54 — NOTHING PAGES ON SUSTAINED BROKER UNREADABILITY** *(new tonight)*. #714 correctly leaves
   the ledger **stale-but-intact** while reads fail, but during that window we stop learning what
   changed at the broker — a native stop could fill unseen. `[BROKER-SYNC-UNREADABLE]` **only logs.**
   **Trip on N consecutive failures AND holding something.** Sizing already measured: longest
   unreadable run was **273 reads ≈ 68 min** (Sat 08-15 eve) vs **6 reads ≈ 1 min** on the last
   trading day ⇒ a clean gap for the threshold. **Natural pair with Ship 1's exit-blocked pager —
   same trip logic, different sensor.** Needs a restart.

### 🟡 TIER B
5. **~35 of 76 SCHWAB ENTRY REJECTS ARE OURS** *(§32, item 18)* — *"The stop price must be above the
   current ask for buy stop orders"*, 7 symbols, 4 of 6 days. ⛔ Same family as the Webull
   `STOP_LOSS_PRICE_LT_MARKETPRICE` case — check for a shared cause. **Test before building:** our
   submitted stop vs the quote at submit vs the next minute of tape. ⛔ **Do NOT widen the trigger to
   make rejects stop — that is strategy, not execution.**
   The other ~41 of 76 are Schwab refusing the security outright (not ours, not fixable).
6. **`virtual_positions` CLEAR LAGS THE EXIT FILL** *(item 12 addendum)* — 4s / 5m26s / 20m03s
   measured. A restart inside that window fires a FALSE `[OMS-BOOT-PROTECTION-ALERT] NAKED`.
   Boot-protection should confirm against `oms_managed_positions` (the #704 pattern). Low severity;
   **a NAKED alert that cries wolf gets ignored the day it is real.**
7. **THE BROKER-TRUTH GATE IS SHARED-BOOK SCOPED** *(item 19)* — `oms/service.py:1145-1171` reads
   `account_positions`, so on a symbol the operator holds manually it cannot tell our shares from
   his. **Latent** — no v2 emitter reaches it. Same shape as the restart pre-flight (item 20).
8. **§1 — TWO "BROKER-PROVEN" COMMENTS REST ON A PREVIEW** and must be corrected.
9. **§31 — CORRECT THE RECORD** on the overnight scope: not "341 positions, latest close 19:00 ET"
   but **41 pre-market `live:orb` positions, 0 past 19:30, latest close-of-day 09:31 ET, longest
   hold 47 min.**
10. **2 UNREPORTED #644 COMPOSITION BREACHES (08-14)** — ONFO + VWAV `resting=2` vs cap ≤1.
11. `broker_order_events` conflates our aborts with broker refusals · fan-out looser cap · float
    ceiling · ~70bps quantisation · Schwab-only orphan watch · reconciler blind to shorts · **§3
    `source` column**.

### 📋 TIER C
12. Churn (median 5 entries/symbol-day) · 16:00 bracket death · boot-hold gate · Redis eviction ·
    per-lot attribution · flaky `test_scanner_cycle_history_retention_and_dedup`.

## ✅ CLOSED TODAY
- **Item 1** — 875/875 entry orders present in Schwab's own book, zero absent; median time-at-rest
  61–62s.
- **Item 17 (the ledger-erasure defect)** — #714 shipped, deployed 17:50 ET, and **proven live at
  19:47 ET by forced fault injection** (real adapter → real sync loop, DB stubbed).
- **§53.2** — the weekend failure burst is **normal 15s poll cadence with every call failing**, NOT a
  retry storm. No new item.

⛔ **QUOTE THE 2-of-2 CONVERSION, NEVER "2/324".** Retained coverage is **Mon 08-10 → Mon 08-17**
(6 sessions), and **274 of the 324 failures fell on Sat 08-15, a non-trading day** — session-day
failures are **50**. Exposure scales with **HOLD TIME**, not failure frequency.
⛔ **§53.1 is UNANSWERABLE, n=1** — the window holds exactly one weekend and `journalctl -u
project-mai-tai-oms` has **no entries at all** (file sink). Current wording stays **"one weekend
showed 274 failures; cause not established."** Do **not** restate it as "Schwab throws outage
bursts" until a second weekend rotates in.

## ✅ §49 — v2 SESSION ROLL: **VERIFIED 08-18. THE PREMISE WAS WRONG — NOTHING TO BUILD.**
⛔ Monday's conclusion (*"v2's roll produced no matching log line… it cannot be verified because it
does not log"*) was **FALSE**. I grepped v2's log for the **strategy-engine's** phrasing
(`session-roll fired`); **v2 emits `[V2-SESSION-ROLL]`.** A whole board section rested on a wrong
grep pattern.

The line exists and has fired at **04:00 ET on every retained day**:
`08-13 rolled=18 · 08-14 11 · 08-15 30 · 08-16 30 · 08-17 30 · 08-18 44`
```
2026-08-18 08:00:03  [V2-SESSION-ROLL] boundary_crossed=True rolled=44 symbols=AACG,AEHL,AIXI,…
2026-08-18 08:09:37  [V2-SESSION-ROLL] boundary_crossed=False rolled=1 symbols=WFF (anchor=… watchlist=1 armed=1; protected/skipped untouched)
```
⭐ It **already logs the DECISION**, not just the action — the second line is a roll that evaluated and
declined. That was the whole ask. It also logs on restarts (`08-13 16:44 / 21:36 rolled=0`).

⇒ **No line to write. No 08-19 verification to wait for. No v2 restart to bundle.** v2 stays untouched
since 08-13, and a deploy comes off the queue.

## 📌 RULES CARRIED FROM 08-17 NIGHT
1. **⛔ A TIMING WRAPPER MEASURES THE WRAPPER.** I reported a 34s OMS shutdown from wall-clock around
   `systemctl stop`; systemd's own record showed **4.4s**. **Read systemd's record.**
2. **⛔ THE SYNC-ABORT EXPOSURE PRE-DATED #714 BY THREE WEEKS.** Webull has raised
   `WebullPositionsUnavailable` since **07-24** while `sync_broker_positions` called
   `list_account_positions` **bare** — a Webull 429 could already abort **Schwab's** sync. #714 made
   the loop per-account; #716 pins it.
3. **⛔ A ROLL / GUARD THAT DOES NOT LOG CANNOT BE VERIFIED.** See §49.
4. **⭐⭐ "UNEXERCISED" DOES NOT MEAN "WAIT".** If the untested path is reachable by **injecting a
   fault into the real object**, inject it — same day. Only market/broker state we cannot create
   justifies waiting. Stub the *persistence*, never the object under test.
5. **⛔⭐⭐ A QUERY AGAINST A WRONG NAME RETURNS A CONFIDENT WRONG ANSWER, NOT AN ERROR.**
   **FOUR instances in 24h**, each of which produced a false conclusion:
   `systemctl is-active project-mai-tai-oms-risk` → `inactive` (no such unit) ·
   `journalctl -u project-mai-tai-oms` → *no entries* (file sink) ·
   grepping v2's log for the **strategy-engine's** roll phrasing → **"it does not log"**, which
   became §49 · grepping `WEBULL-PROTECT-SKIP/NOHOLD/SERIALIZED` → 0/0/0 for markers that
   **do not exist** (the real ones are `ABANDONED`/`COALESCED`/`UNPLACEABLE`).
   ⇒ **ENUMERATE, THEN FILTER.** A name you typed is an **unverified input to the query**. If the
   tool cannot distinguish *absent* from *dead/empty/zero*, verify the name first — `list-units`,
   `ls`, or grep the marker **out of the source**.
   ⛔ And never chain a destructive command behind a suppressed-error one — a
   `checkout … 2>/dev/null; reset --hard` wiped two commits off the branch it was standing on.

## 🔑 SCHWAB TOKEN
`refresh_token_expires_at = 2026-08-19T09:21:35Z` ⇒ **Wed 08-19 05:21 ET**, BEFORE the 07:00 EH open.
**Re-auth Tue evening or Wed after the close.** ⛔ Never quote from memory — read the store.

## ⚠️ SCHWAB API WAS UNSTABLE 08-17
**46 positions-read failures (ET) + 7 entry aborts** — timeouts, `Unable to resolve host
traderapi-accounts.schwab.com`, "Service is currently unavailable". **One root, two harms** — dropped
entries AND holdings-ledger corruption. A fix addressing only one will look like it worked.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] · [[project_mai_tai_webull_core_session_root_cause]] ·
[[project_mai_tai_reprotect_chain_uncovered_window]] · [[project_mai_tai_virtual_positions_false_zero]] ·
[[project_mai_tai_probe_w_webull_stoplimit_master]] · [[feedback_unexercised_is_not_a_result]] ·
[[feedback_the_tools_status_is_not_the_things_status]] · [[feedback_a_failing_control_voids_the_probe]]

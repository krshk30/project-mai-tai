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

# 🚨 TOMORROW'S FIRST ACTION — 08:11–08:26 ET, DO THIS BEFORE ANYTHING ELSE

**#710 shipped the CORE→ALL_DAY session fix at 16:06 ET 08-17. It is UNEXERCISED.** The only thing
that can prove it is a **pre-market Webull fan-out fill**, and the window is ~08:11–08:26 ET
(pre-market `live:orb` fills run 8/3/9/4/5/1 per session). **Miss it and you wait a day.**

**THREE-PART ACCEPTANCE — part 1 alone is NOT proof.** The failure that looks like success: the pair
is accepted (200), rests, and its legs are inactive until 09:30 — a clean `ATTACHED` line protecting
nothing.

```bash
# 1. did it attach, and with which session string?
sudo grep -E "WEBULL-PROTECT-(ATTACHED|RETRY|FAILED)|WEBULL-EXIT-PAIR-REFUSED" \
  /var/log/project-mai-tai/oms.log | cut -c1-260      # every line now carries session=
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

---

## ⚡ FIRST SCREEN

**As of 2026-08-17 EOD.** **Deployed HEAD `a2616f6`** — verified BY CONTENT on the box and asserted
as an ancestor; `src/` diff vs `origin/main` = 0 files. Tree clean. **Account fully FLAT** (0 managed
rows, 0 working orders, shared book empty — the operator's IVF 5000 closed).

### Shipped + deployed today — four PRs, all verified by content
| PR | what | outage | exercised? |
|---|---|---|---|
| #706 | 3 guards on the Webull attach + `RoutingBrokerAdapter.fetch_quotes` | 3s | ⛔ **0/0/0 — none has run** |
| #707 | `[WEBULL-EXIT-PAIR-REFUSED]` payload logging · retry horizon 4.0s→~29s | 10s | ✅ **validated, and it delivered the root cause** |
| #709 | broker account on every `[V2-OCO-EMIT]` line | 15s | pending next bracket |
| #710 | **pre-market pair tagged `ALL_DAY`, not `CORE`** | 3s | ⛔ **UNEXERCISED — see the top of this file** |

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
Y 417`. **`ALL` is valid for a SINGLE-LEG order and REFUSED on the combo** — the combo restricts the
documented enum. **Do NOT "correct" ALL_DAY to ALL.**
⛔ **`preview_order` does NOT validate position backing** (it 200s while flat) ⇒ Probe W4 only ever
proved the shape PARSES, never that it PLACES. Two comments still say "BROKER-PROVEN" (item 8).

## 🕐 OPEN PRs — **NONE.** #706 #707 #709 #710 all merged and deployed.

## 🔴 OPEN THREADS — detail in [`handoff-open-items.md`](handoff-open-items.md)

### 🔴 TIER A — can cost money now
1. **⭐⭐ THE ACCEPTANCE READ** — top of this file. Nothing else runs first.
2. **⛔⭐⭐ ONE FAILED SCHWAB POSITIONS READ ERASES A HELD POSITION'S LEDGER ROW — PROVEN HARM**
   *(item 17, new)*. `list_account_positions` returns `[]` on any failure (5 paths incl. a bare
   `except`), `sync_account_positions` zeroes every absent symbol, `[VIRTUAL-CLEAR]` erases the row
   **one-way**. Webull HAS a never-synthesize-flat guard; **Schwab has none.**
   **324 failures / 109 hold-windows / 2 landed during a hold / 2 of 2 ERASED**, to the second
   (CRWU 08-12 19:34:18, VWAV 08-14 19:31:49). Both were **isolated single failures** — no burst
   needed. ⇒ Report the **2-of-2 conversion**, never the 2/324 trigger rate.
   **BUILD 3 LAYERS:** port Webull's guard to Schwab · gate the sync against an empty snapshot ·
   **make the erasure re-derivable** (nothing repairs these rows today — that is what turns a
   transient hiccup into permanent corruption).
3. **THE RETRY BOUND IS STILL UNREACHABLE** — `_v2_exit_close_failures` resets on every positively-
   HELD read. Untouched by everything shipped today.
4. **PRE-MARKET IS 0% BRACKETED** — 14d: PRE 0/34 orb, 0/13 schwab. **#710 is the first real attempt
   at this.** Schwab still refuses stop legs in EH; #647 Gate 2 (built, dark) is its only answer.

### 🟡 TIER B
5. **~35 of 76 SCHWAB ENTRY REJECTS ARE OURS** *(item 18, new)* — *"The stop price must be above the
   current ask for buy stop orders"*, 7 symbols, 4 of 6 days. ⛔ Same family as the Webull
   `STOP_LOSS_PRICE_LT_MARKETPRICE` case — check for a shared cause. **Test before building:** our
   submitted stop vs the quote at submit vs the next minute of tape. ⛔ **Do NOT widen the trigger to
   make rejects stop — that is strategy, not execution.**
   The other ~41 of 76 are Schwab refusing the security outright (not ours, not fixable).
6. **`virtual_positions` CLEAR LAGS THE EXIT FILL** *(item 12 addendum)* — 4s / 5m26s / 20m03s
   measured. A restart inside that window fires a FALSE `[OMS-BOOT-PROTECTION-ALERT] NAKED`.
   Boot-protection should confirm against `oms_managed_positions` (the #704 pattern). Low severity;
   the reason stands on its own — **a NAKED alert that cries wolf gets ignored the day it is real.**
7. **THE BROKER-TRUTH GATE IS SHARED-BOOK SCOPED** *(item 19, new)* — `oms/service.py:1145-1171`
   reads `account_positions`, so on a symbol the operator holds manually it cannot tell our shares
   from his. **Latent** — no v2 emitter reaches it. Same shape as the restart pre-flight (item 20).
8. **2 UNREPORTED #644 COMPOSITION BREACHES (08-14)** — ONFO + VWAV `resting=2` vs cap ≤1.
9. `broker_order_events` conflates our aborts with broker refusals · fan-out looser cap · float
   ceiling · ~70bps quantisation · Schwab-only orphan watch · reconciler blind to shorts.

### 📋 TIER C
10. Churn (median 5 entries/symbol-day) · 16:00 bracket death · boot-hold gate · Redis eviction ·
    per-lot attribution · flaky `test_scanner_cycle_history_retention_and_dedup`.

## ✅ CLOSED TODAY
**Item 1 (submitted-but-never-rested) — CLOSED.** Against Schwab's OWN book, per-day pulls:
**875/875 of our entry orders are present — ZERO absent.** Median time-at-rest **61–62s every day**
(a fixed reprice cadence, not market behaviour). 08-11 is unremarkable on all three measures. The
E1→E2 drop was an artifact of E2 coming from our own `ExecutionReport` normalisation.

## 🔑 SCHWAB TOKEN
`refresh_token_expires_at = 2026-08-19T09:21:35Z` ⇒ **Wed 08-19 05:21 ET**, BEFORE the 07:00 EH open.
**Re-auth Tue evening or Wed after the close.** ⛔ Never quote from memory — read the store.

## ⚠️ SCHWAB API WAS UNSTABLE 08-17
~45 positions-read failures + 7 entry aborts (timeouts, `Unable to resolve host
traderapi-accounts.schwab.com`, "Service is currently unavailable"). **One root, two harms** —
dropped entries AND holdings-ledger corruption. A fix addressing only one will look like it worked.

## 🧠 MEMORY POINTERS
[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project_mai_tai_restart_bar_gap_checklist]] · [[project_mai_tai_webull_core_session_root_cause]] ·
[[project_mai_tai_reprotect_chain_uncovered_window]] · [[project_mai_tai_probe_w_webull_stoplimit_master]] ·
[[feedback_which_population_does_this_change_reach]] · [[feedback_a_failing_control_voids_the_probe]] ·
[[feedback_unexercised_is_not_a_result]] · [[feedback_fixture_must_match_production_config]]

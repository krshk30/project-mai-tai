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

**As of 2026-08-17 EOD.** Tree clean. **Account fully FLAT** (0 managed rows, 0 working orders, shared
book empty — the operator's IVF 5000 closed).

**Box files = `d9d84de`; `src/` diff vs `origin/main` = 0 files.** ⛔ But the **running OMS process
still carries `70ca930`'s code** — it started **17:50 ET** (#714) and has **not restarted since**.
The only `src/` delta between them is #715's new `backtest/broker_refusal.py`, which the OMS never
imports ⇒ **functionally identical for the OMS, but do not call the process "d9d84de".**
Verified BY CONTENT, not by hash: `BROKER-SYNC-UNREADABLE` ×1, `SchwabPositionsUnavailable` ×5.

### Shipped + deployed today — FIVE PRs, all verified by content
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

| **#714** | **L1+L2+L3 — a failed Schwab positions read never reads as a flat account** | 4.4s | ✅ **PROVEN BY FORCED INJECTION** |

**Deployed HEAD is now `70ca930`** (#714, 17:50 ET). ⛔ §43 diff assertion held: **`webull.py` is NOT
in the changed set**, so #710's `ALL_DAY` is untouched and tomorrow's attribution is preserved.

### ✅ §50 — #714 RESOLVED 19:47 ET WITHOUT WAITING (option **a**, forced injection)
On-box harness: the **real `SchwabBrokerAdapter`** pointed at an unreachable endpoint, driving the
**real `sync_broker_positions`**, with only the DB store stubbed. Four assertions all passed —
adapter **raised** the typed exception (not `[]`); the failed account **never reached**
`sync_account_positions`; the **healthy account still synced**; the one-way clear **and** the L3
restore were both scoped to `fetched`. `[BROKER-SYNC-UNREADABLE]` fired with the full reason.
⛔ No live account traded, no DB row written, fleet flat, outside market hours. Harness deleted.
⭐ **Method note:** "UNEXERCISED is not a result" does **not** oblige you to wait for a rare live
trigger. Inject the fault against the real object. Waiting was the worse option here — see below.

### ⛔⭐ DENOMINATOR CORRECTION — "2/324" IS CONTAMINATED
Retained-log coverage is **Mon 08-10 → Mon 08-17** (6 sessions), **not** 08-11→08-17 as first stated.
⛔ **Re-bucketed to ET** — my first pass bucketed by UTC day, and **logs rotate 00:00 UTC = 20:00 ET**,
so a Saturday-evening burst was filed under Sunday:

| ET day | failures | | ET day | failures |
|---|---|---|---|---|
| 08-10 Mon | 0 | | **08-15 Sat** | **274** ⛔ *market closed* |
| 08-11 Tue | 2 | | 08-16 Sun | 0 |
| 08-12 Wed | 1 | | 08-17 Mon | 46 |
| 08-13 Thu | 0 | | 08-14 Fri | 1 |

**274 of 324 (85%) fell on a non-trading day.** Session-day failures are **50**, not 324. The
**2-of-2 conversion is unchanged** and remains the number to quote.

### §53 — THE WEEKEND SPIKE: **NOT** PROMOTED TO A FINDING
⛔ **Downgraded wording: one weekend showed 274 failures; the cause is NOT established.**
The "Schwab throws large outage bursts" claim is withdrawn pending evidence. The forced-injection
decision stands on its own — only that rationale was in question.

**53.1 — prior weekends: UNANSWERABLE, n=1.** The retained window runs Mon→Mon and contains
**exactly one weekend**. `journalctl -u project-mai-tai-oms` returns **no entries at all** (the service
logs to a file sink), so there is no deeper history. ⇒ Cannot distinguish scheduled weekend
maintenance from an outage. **Re-check after the next weekend rotates in.**

**53.2 — cadence, NOT a retry storm** ✅ *(closed, no new item)*. Inter-failure gaps during the burst:
**254 of 272 were exactly 15s** (whole range 14–16s), and the adapter's positions read has **no retry
or backoff** (its only retry is a single token-refresh). ⇒ normal 15s poll, **every call failing**.
Schwab's own words, 278×: `Application encountered unexpected error that prevented fulfilling this
request` — a server-side 5xx, not our timeout. Other classes: 30 read timeouts, 8 DNS
`NoResolvedHost`, 3 `Service is currently unavailable`, 3 TLS handshake, 2 upstream reset.

### 🔴 §54 — THE FIX CREATES ITS OWN SILENT WINDOW *(new item, NOT tonight)*
#714 correctly leaves the ledger **stale-but-intact** while reads fail — but during that window we
stop learning what changed at the broker, and a native stop could fill unseen.
`[BROKER-SYNC-UNREADABLE]` **only logs**; nothing pages on *sustained* unreadability. 274 lines nobody
reads is a component reporting healthy while its function is dead — the shape this board exists for.
**Sizing is already measured** — the trip threshold has a clean gap to sit in:

| | longest consecutive unreadable run |
|---|---|
| Sat 08-15 evening | **273 reads ≈ 68 min** |
| last trading day (08-17) | **6 reads ≈ 1 min** |

⇒ Trip on *N consecutive failures* **and** *holding something*, well above 6 and well below 273.
**Natural pair with Ship 1 (the exit-blocked pager): same trip logic, different sensor.**

## 🕐 OPEN PRs
**#715** R4 refusal model (backtest module only, no wiring) · **#716** §46.1 raise-propagation tests.
Both docs/test-only — **neither needs a restart**.

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
**Item 1 — CLOSED.** 875/875 entry orders present in Schwab's own book, zero absent; median
time-at-rest 61–62s. Full detail in [`handoff-log.md`](handoff-log.md).

## §49 — v2 SESSION ROLL: GIVE IT A LINE, THEN VERIFY IT
**STATUS: UNVERIFIED — not confirmed, and not a pass.** ⛔ Leave this visible until it changes;
"unverified" is a state that resolves only if someone comes back to it.

During §46.3 strategy-engine's roll was **confirmed firing in-process**:
```
2026-08-17 08:00:00 UTC  bot day-roll fired | strategy=polygon_30s  2026-08-16 -> 2026-08-17
2026-08-17 08:00:02 UTC  scanner session-roll fired | previous_session=2026-08-16T08:00:00+00:00
```
There is **no systemd timer and no crontab entry** for it — the roll is driven by the running
strategy-engine loop, so a restart disarms nothing; it only needs the process alive at 04:00 ET.

⛔ **v2's own roll produced no matching log line.** v2 was not restarted, so nothing changed for it —
but nothing verified it either. **It cannot be verified because it does not log**, and a roll that
fires silently is indistinguishable from one that never fired. Same failure this board keeps finding:
**the code only speaks when it acts.**

**Order of work — the line FIRST, then the verification:**
1. **Add the line.** Emit on the roll itself, carrying previous/new session boundaries and the
   account, matching the shape `strategy_engine_app` already uses. ⭐ **Log the DECISION, not just
   the action** — if the roll evaluates and declines to fire, that gets a line too, or the same blind
   spot reopens one level down.
2. **Then verify against the 08-19 04:00 ET boundary — NOT 08-18's**, because the line will not exist
   for tomorrow's roll. State that explicitly; do not report tomorrow's absence as a pass.
3. **Deploy timing:** v2 has been untouched since 08-13 and restarting it is not free. This is
   log-only and **does not justify a restart of its own** — bundle it with the next v2 deploy that
   has a real reason, or hold it. By-content and files-before-process checks apply as usual.

⛔ Do NOT close this as "verified" because the roll appears to work. Until the line exists and has
been read after a real 04:00 boundary, the honest status stays **UNVERIFIED**.

## 📌 THREE RULES CARRIED FROM 08-17 NIGHT
1. **⛔ A TIMING WRAPPER MEASURES THE WRAPPER, NOT THE SERVICE.** I reported a 34s OMS shutdown from
   wall-clock around `systemctl stop`; systemd's own record showed **4.4s** (`Stopping 21:49:59.362`
   → `Deactivated 21:50:03.744`, `TimeoutStopUSec=30s` never approached). **Read systemd's record.**
   Cheap rule; it kept a wrong entry off the board.
2. **⛔ THE SYNC-ABORT EXPOSURE PRE-DATED #714 BY THREE WEEKS.** The Webull adapter has raised
   `WebullPositionsUnavailable` since **2026-07-24**, and `sync_broker_positions` called
   `list_account_positions` **bare** — so a Webull 429 with no cached snapshot could already abort
   **Schwab's** sync too. #714 made the loop per-account; #716 pins it with a behavioural test. The
   exposure existed for three weeks and nobody knew — same column as everything else here.
3. **⛔ A ROLL / GUARD THAT DOES NOT LOG CANNOT BE VERIFIED.** See §49. Give it a line before trying
   to confirm it, and log the decision as well as the action.

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

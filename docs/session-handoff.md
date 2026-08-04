# Session Handoff — CURRENT STATE (read this first)

> ## ⛔ HOW TO MAINTAIN THIS FILE — two verbs, never merge them
> 1. **OVERWRITE this file.** It answers one question: *what is true RIGHT NOW.* If a line here is
>    no longer true, **delete or rewrite it** — never append.
> 2. **APPEND to [`handoff-log.md`](handoff-log.md).** That is where *what changed today* goes.
>
> ⭐ **Why it is split (2026-07-29).** These two jobs used to share one file. Appending is easy and
> got done every day; overwriting requires noticing that something written two weeks ago is no
> longer true, and nothing forced it. Result: the narrative was always current while LIVE OPS STATE
> sat **twelve days stale**, and the file's own "keep under 400 lines" rule was structurally
> impossible. Keep them apart and staleness needs someone to actively write something false.
>
> **Target: ~150 lines.** If it grows, detail belongs in the log or a linked doc, not here.
> To onboard an agent: *"Read `docs/session-handoff.md`."* — this file alone should be enough to
> know what is live, what is open, and what to watch.

> **⛔⭐ OUTPUT CONSTRAINT (every study, no exceptions): report per-trade %, MEDIAN-FIRST, with a
> drop-one. NEVER a bare dollar total.** One VEEE at $25-29 notional outweighs sixteen $1-7 names
> and flips conclusions (violated twice 07-15 -> two wrong answers). This is the single line that
> saves the most rework. [[project_mai_tai_percentages_not_dollars]]

---

## 🟢 FLEET — what is live right now

**As of 2026-08-03 EOD.** v2 flat; only the operator's manual CYN 5000 remains.
✅ **DEPLOYED 2026-08-03 23:00 UTC — HEAD `b117d89`.** Both v2 fixes are LIVE (attended, post-close).
All five services active, v2 errors since restart **0**, ledger flat, **0 open managed rows**.

⭐ **Today was an ENTRY-PATH day.** Four live breaches of the entry cap on real money, three
phantom managed rows, and a P0a test that never became possible. Detail: [`handoff-log.md`](handoff-log.md) 08-03.

| service | state |
|---|---|
| `project-mai-tai-schwab-1m-v2` | active — real money (+ Webull fan-out leg) |
| `project-mai-tai-oms` / `strategy` / `control` | active (OMS up since 07-31, NRestarts=0) |
| `project-mai-tai-trade-coach` / `-orb` | inactive + disabled |

**Live flags unchanged from 07-31.** P0a (`OMS_HOLD_MARKETABLE_MANAGED_EXIT`) is still
**deployed-not-validated** and runs on the CODE DEFAULT `True` — it is **absent from the env file
and from `/proc/<oms-pid>/environ`**, so the kill switch is an **APPEND**, not a flip:
`MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT=false` + `stop strategy → restart oms → start strategy`.

**Schwab token:** re-authed 08-03 06:40 ET after a weekend death. Next expiry **Mon 2026-08-10
06:40 ET — 19 min before the EH window opens.** A Wednesday-evening re-auth reminder is now live
(`/home/trader/schwab_reauth_wednesday_cron.sh`, trader crontab `2 22,23 * * *`, guarded to Wed
18:00 ET) — re-authing midweek parks every future expiry off the market open.

---

## 🔴 ATTENDED DEPLOY QUEUE — nothing ships without the operator present

| # | what | state |
|---|---|---|
| **#639** | bar-gap DEAD BAND (`>` → `>=`) | ✅ **MERGED** `b96a0eb` |
| **#645** | pager scoping + halt downgrade | open — replaces **#641**, which GitHub auto-closed when `--delete-branch` on the #639 merge removed the base it was stacked on. Same commit cherry-picked onto main. ⛔ **lesson: never `--delete-branch` a PR that has a stacked child** |

✅ **VPS SYNCED to `71c6c2c`** — all ops fixes live on the box, modes 755, `bash -n` clean, and
`fleet_health_check.py` run live returns **GREEN on all 4 checks**.

⛔⭐ **A REPO-SIDE BUG SURFACED DOING IT.** The pull ABORTED because two cron wrappers had local
modifications on the box — and the diff was **mode-only, `100644 → 100755`**. They are committed
**non-executable in git**, so someone had `chmod +x`'d them on the box to make cron actually run
them. That is the Windows-commit trap in its permanent form: **any fresh checkout or new box gets
crons that silently never run.** Resolved for now by resetting the mode, pulling, then `chmod 755`
back — but the REPO still has them 644. **Fix with `git update-index --chmod=+x` (what #568 did for
the OCO wrapper) and audit every `ops/health/*.sh` for the same.**
| **#642** | design note: entry-count + exit-poll, one workstream | open |
| **#644** | ✅ **DEPLOYED** — entry composition cap + exit poll from open rows | live at `b117d89` |

⛔ **NEITHER FIX IS FLAG-GATED.** Rollback = `gh pr revert 644` → VPS `git pull --ff-only` →
restart v2, then `stop strategy → restart oms → start strategy`. (P0a's own kill switch is separate
and still an **APPEND** of `MAI_TAI_OMS_HOLD_MARKETABLE_MANAGED_EXIT=false`.)

⚠️⚠️ **TWO THINGS THE DEPLOY DID *NOT* PROVE — do not log these as validated:**

1. **Fix 2's re-enrol path is UNEXERCISED.** The three phantoms cleared via the OMS restart's
   `[OMS-V2-MANAGED-REHYDRATE]` — the pre-existing "only a restart clears them" behaviour — **not**
   via the new poll-from-rows-and-re-enrol mechanism. `[OMS-V2-POLL-REENROLL]` has never fired.
   **⭐ Track presence AND frequency:** one fire proves the mechanism; REPEATED fires mean the
   underlying leak is live and self-healing is masking it — that is the trigger for a separate
   root-cause pass. ⛔ "Phantoms cleared on deploy" is NOT "Fix 2 validated".
2. **The entry cap is unvalidated until a live cross.** First cross next session must read a legal
   composition — **two entries, not three**.

✅ What the deploy DID prove: two exits backfilled from broker execution records
(`[OMS-OCO-EXIT-POLL]` → `[OMS-OCO-EXIT-FILL]` → `[OMS-V2-OCO-RESOLVED-FLAT]`), all three rows
closed, 6 rejects (`oversold` / `NO_POSITION`) confirming flat the hard way with #608's bound
holding well under 8, and **nothing executed**.

---

## ⭐⭐ THE ENTRY BUG — FIXED IN CODE, NOT YET LIVE (35b46e1)

**Three entries on one ATR cross, four times on 2026-08-03** (HYFM ×3, FUSE ×1), all real money.
The operator spotted it on a chart showing one unbroken ATR trail; the bot's own log confirmed
exactly ONE arm and ONE disarm per run.

**Two defects, both required:**
1. the ARM ran `cw_entries_this_flip = 0`, **wiping the entry that caused the cross** — the resting
   buy fills INTRABAR, the arm confirms at the BAR CLOSE 21s–706s later
2. the resting FILL **never consumed a slot at all** — the only increment was on the reactive path

⭐ **The cap is COMPOSITION, not a count** (operator 2026-08-03): **≤1 resting AND ≤1 reclaim**.
A scalar cap-at-2 permits `resting+resting` and `reclaim+reclaim`, and two reclaims is "very bad".
⛔ **Degenerate case:** if the resting never fills its slot is **FORFEIT** — reactive may not
substitute into it. ⛔ An exit does **not** refill a slot.

⚠️⚠️ **BLAST RADIUS — LIVE NOW, EXPECT A LIGHTER TAPE.** Reactive can no longer be a first entry
and must break the **segment high**, not the flip+2 trigger. On 08-03 the Schwab leg filled
**11 resting and 10 reactive**, so a material share of the reactive ones would not have fired.
⛔ **"Byte-identical on a normal cross" is FALSE and was dropped with the scalar spec** — the
composition rule makes it impossible. Proof: a quote at 12.9 against a 15.8 segment high used to
enter and now returns None (that is the SOBR chase closing). Intended under the rule, **not a
regression** — but judge whether those reactive entries were earning.
⭐ **Side effect:** this closes the SOBR stale-trigger chase (`test_stale_trigger_behaviour_is_restored`
existed to document it as live-and-accepted; it is now inverted, not deleted).

---

## 🔴 THREE PHANTOM MANAGED ROWS — UNTOUCHED, awaiting the operator's go

`live:orb` FUSE · `live:orb` HYFM · `live:schwab_1m_v2` HYFM. Broker-flat confirmed, **no money at
risk**, but they block fan-out re-entry (`fanout_webull_collision_managed` fired 3× on 08-03).

**All three produced ZERO miss lines** — that is the confirmation they are the **never-enrolled**
shape, not the cancelled-buy shape. The exit poll iterates an **in-memory set**
(`_managed_v2_symbols`), not the table it services, so an open row whose key is missing is never
polled, never logged, never closed.

⛔ **Ruled out with evidence:** collision-skip · all five discard sites · rehydrate · `_v2_accounts()`
· the store lookup · a loop-abort. **How the keys left the set is still unpinned — and that is
deliberately not a blocker.** Driving the poll from the open rows closes the class regardless.

**After-close plan (operator's go required):** backfill the unrecorded round trips from Schwab
history, then clear the three rows.

---

## 👀 WATCH NEXT SESSION

1. **DEPLOY FIX 1 + FIX 2 ATTENDED — both are BUILT (#644).** Gates:
   entry-counter → a live cross reads a legal composition, never 3 · exit-poll → **evict an open
   row's key from the set and prove the poll re-enrolls and closes it.** ⛔ Do not ship a fix that
   only works when the set is already correct.
2. **RE-ARM THE WATCH.** `/home/trader/p0a_watch.sh` exits at 16:05 by design. Relaunch:
   `setsid /home/trader/p0a_watch.sh </dev/null >>/home/trader/p0a_watch.nohup 2>&1 &`
   ⛔ Two corrections it still needs: bound windows by **DISARM** as well as ARM (it swept in a
   post-disarm entry), and attribute by **FILL time** not `submitted_at` (a resting order is placed
   minutes before it fills). Retire/rename `oco_old_miss_lines` — it still counts raw AGE, which is
   exactly the discriminator we proved wrong.
3. **P0a IS STILL UNVALIDATED.** It needs a MARKETABLE software-ladder exit that rests through a
   refresh tick and fills. 08-03 produced one EH exit that filled in **41ms** — correctly scored
   `fastfill_inconclusive`, neither pass nor fail. Closure comes via **item 11's stand-down path**
   (they converge); a deliberate attended pre-market qty-1 test is the fallback.
4. **The 2 Webull position-sync failures** want a "can a failed sync leave stale holdings state?"
   check. They do NOT explain the phantoms (timing does not line up) but are a plausible other
   divergence source. The other 38 of 42 OMS errors are chronic Webull API flakiness — in view,
   not chased.

## 🔴 OPEN THREADS (detail: [`handoff-open-items.md`](handoff-open-items.md))

1. **Backtest-vs-live parity on a STABLE-CODE day** — still unproven end-to-end. 07-30 unusable.
2. **polygon/strategy-engine freeze — HALF CLOSED.** The trade-coach half is resolved (see log).
   The JSON-snapshot-encode freeze is not; #366 remains undeployed.
3. **Schwab API-open rejects ~3/day and nothing evicts** — ~20 lost entries last week, a different
   symbol each day. `#326`'s eviction does not fire on this reject reason.
4. **An unnamed suppression stops a rejected-symbol retry** — risk PASSES, no broker order is
   created, the intent is marked rejected, nothing is logged. Currently protective; mechanism
   unidentified. Same shape as #580/#608, which both cost money.
5. **A Schwab rejection vetoes the Webull leg too** — the fan-out itself works (proved on
   APLX/SNDG); but a name Schwab refuses via API is traded on NEITHER broker.
6. **Order churn 284 orders -> 23 round trips (12:1)** — resting-entry reprice churn. Invisible to
   the recorder, fully visible on the live tape. Understand before the coach redesign.
7. **Reconciler severity is INVERTED — an UNOWNED position pages CRITICAL.** A hand-bought AZIO
   (972 sh, `live:orb`, zero orders/fills/bars of ours) paged RED. `virtual_quantity == 0` is the
   *definition* of "not ours", and it forces `critical`; a real drift on a position we own is only a
   *warning*. The payload already computes `strategy_codes: []` and discards it. ⛔ PROTECTED_SYMBOLS
   gates the OMS, a *different* list gates the reconciler. ✅ OMS never touched AZIO.
8. **Redis evicts the HEARTBEAT stream ⇒ false "oms-risk fleet down" RED page.** `maxmemory 512 MB`
   + **`allkeys-lru`** + `snapshot-batches` at **180 MB in 26 entries** ⇒ the 47 KB heartbeat key gets
   dropped and the watchdog reports a zombie. OMS was healthy throughout (0 log gaps >60s).
   ⛔ **Do NOT fix by cutting `snapshot_batch_stream_maxlen`** — 180 is load-bearing (the scanner
   warmup prefill needs **120** batches); cutting it blinds squeeze detection ~10 min per restart.
9. **⭐⭐ SELECTION — we buy stocks whose move is already SPENT (scanner AND bot).** AXTU 07-31 was
   **+54.5%** before we touched it; range then compressed 8.4%→5.1% while median bar volume fell
   4,388→1,200, and we bought it **three times** during that decay. Operator wants names that still
   OSCILLATE ("down, up 20%, down"), not exhausted ones. ⛔ The vol floor can't fix it: it samples
   **one** bar (AXTU armed off a 10,467 bar, filled into 2,999; median 2,217, only 17% of bars clear
   10,000). ⛔ **DISCUSS BEFORE BUILDING.**
10. **IRE: a Schwab REPLACE spawned an order we never recorded** — our books said 2, broker said 4.
   We never issue a replace. Parked by operator decision: catch it live next time (#626 now
   surfaces the drift in ~8 min instead of hours).
11. **⭐🔴 PRE-MARKET OCO HOLE — no native bracket outside RTH, so the software ladder owns the
   exit.** `[V2-OCO-EMIT] SKIPPED (outside RTH)` is exactly how KUST 07-31 lost **−5.17%** on a
   signal that was right while the Webull leg made **+1.76%**. **DEFERRED BEHIND P0a VALIDATION,
   NOT DROPPED** — validate the hold on the software-ladder path *before* changing that path,
   otherwise the fix and its own regression land together and neither is measurable. Sequencing:
   P0a validated → (#366 if the quiet window is only deploy-sized) → **this is the next build.**
   ⛔ Its design constraint is not optional — see *THE STAND-DOWN-CLEAR CONSTRAINT* below.

✅ **CLOSED today: VPS retention** (DB 12->10 GB, logs 1.7 GB->426 MB, logrotate installed).

---

## ⛔⭐ THE STAND-DOWN-CLEAR CONSTRAINT (binds open thread 11)

**Emitting a bracket is NOT sufficient. "OCO ⇒ churn-immune" is false.**

A native OCO bracket makes the BROKER own the exit, so timer-driven cancel/replace structurally
cannot happen — *while the bracket is live*. But when it resolves or stands down,
`[OMS-OCO-STAND-DOWN-CLEARED] ... OCO gone; ladder deferred` hands the exit **back to the bare
timer ladder** — which is KUST, now on a bracketed entry.

**The evidence it is real, not theoretical.** Cancelled/rejected sells within 60 min of an
**OCO-bracketed** entry: **NVVE 07-23 = 11**, KUST 07-22 = 6, FIEE 07-27 = 6, several at 3.
*(Caveat: symbol-level count in a time window; some sells may belong to another position that day.)*

⇒ **The requirement.** On stand-down-clear the exit must either **re-arm a bracket** or **inherit
the P0a marketable-hold** (`_managed_exit_refresh_exempt`, `oms/service.py:3770`). It must **never**
fall back to the bare refresh cadence. Any pre-market-OCO design that does not state which of those
two it does on stand-down-clear is incomplete.

⚠️ P0a alone does not close this: the hold engages only while `limit <= bid`. A bracket that stands
down while the exit is **not** marketable still lands on the plain ladder.

---

## 🔔 ALERTING — what will now reach the phone (all ntfy, all NEW today)

| watch | fires on |
|---|---|
| `bar_gap_watch_cron.sh` | a hole in the v2 bar series — **and auto-repairs the DB**, then reports what it filled |
| `reconcile_alert_cron.sh` | any reconciler **critical** finding, fingerprint-deduped (7/day, not 1149) |
| existing | OMS liveness, pre-open readiness, token expiry, OCO capture, orphan orders |

⛔ All are ROOT crontab, guarded in ET **inside the script** (`CRON_TZ` is ignored on this box).
⛔ **A script committed from Windows lands mode 664 AND carries CRLF** — both make it silently never
run. Verify `stat -c %a` and `bash -n` **on the box**. Hit both today.

---

## 📜 HISTORY

- **What happened, day by day:** [`handoff-log.md`](handoff-log.md) (append-only)

| archive | covers |
|---|---|
| [`handoff-archive/2026-07.md`](handoff-archive/2026-07.md) | 07-16..07-25 — OCO build, wrong-bars root cause, resting flip-entry, EH trading, fan-out |
| [`handoff-archive/2026-06.md`](handoff-archive/2026-06.md) | go-live, #326, OMS exits, ATR qualifier, 04:00 race |
| [`handoff-archive/2026-05.md`](handoff-archive/2026-05.md) | v2 build-out — bar-build, ATR-flip design, exit-engine |
| [`handoff-archive/2026-04.md`](handoff-archive/2026-04.md) | token-SPOF saga, early v2 scaffolding |
| [`handoff-archive/schwab-1m-v2.md`](handoff-archive/schwab-1m-v2.md) | the v2 bot's own design/status history |

## 🔗 KEY REFERENCE DOCS (design-first / canonical)

- **Entry rules:** [`schwab-1m-v2-entry-criteria.md`](schwab-1m-v2-entry-criteria.md) ·
  [`v2-fresh-flip-since-confirmation-design.md`](v2-fresh-flip-since-confirmation-design.md) **(NEW 07-30)** ·
  [`schwab-1m-v2-atr-flip-entry-design.md`](schwab-1m-v2-atr-flip-entry-design.md)
- **ATR qualifier + warmup:** [`v2-atr-fresh-flip-qualifier-design.md`](v2-atr-fresh-flip-qualifier-design.md)
  ⛔ *its gate is implemented in DEAD CODE and the flag is off — enabling it gates nothing*
- **Resilience / ops:** [`schwab-1m-v2-loop-resilience-design.md`](schwab-1m-v2-loop-resilience-design.md) ·
  [`vps-deployment.md`](vps-deployment.md)

## 🧠 MEMORY POINTERS (auto-load each session)

[[project-mai-tai-context]] · [[project-mai-tai-fleet-roster]] · [[project-mai-tai-architecture]] ·
[[project-mai-tai-restart-bar-gap-checklist]] **(READ BEFORE ANY RESTART)** ·
[[project-mai-tai-orb-window-defers-entries-to-1000]] · [[project-mai-tai-trade-coach-dead-retry-storm]] ·
[[project-mai-tai-oms-scoping-invariant]] · [[feedback-be-crisp-no-essays]] ·
[[feedback-session-doc-and-memory-discipline]]

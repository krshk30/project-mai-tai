# Deploy sheet — Thu 2026-08-20 evening window

**Written 2026-08-20 morning, before the window opens.** Acceptance is fixed HERE, in advance, so
nothing gets graded against a number chosen after the result is visible.

> ⛔⭐⭐ **`src diff = 0` IS NOT EVIDENCE.** Since 2026-08-19 the box carries code it is not running.
> Every step below states the **file-write time and the process-start time side by side**, per
> service. The diff is not the check; the table is.

---

## 1. Order — OMS first, then v2. Not negotiable.

| # | service | carries | why this order |
|---|---|---|---|
| 1 | **oms** | **#735** + **#736** + **#737** | #735 is what lets the Webull mirror leg through at all. If v2 restarts first with the flag on, it emits legs into an OMS that still stamps a Schwab bracket onto them and aborts them client-side — the exact 720-order defect, and the acceptance would fail for the wrong reason. |
| 2 | **schwab-1m-v2** | **#743** + **the flag** | Restart only after the OMS is confirmed running the new code. |

### ⛔⭐⭐ THE FLAG. Miss it and #735 ships and does NOTHING.

```
MAI_TAI_STRATEGY_SCHWAB_1M_V2_WEBULL_RESTING_MIRROR_ENABLED=true
```

Confirmed **`false`** on the box this morning, read from `/etc/project-mai-tai/project-mai-tai.env`.
Set it with the v2 restart, not before. Then **read it back from the running process**, not from the
file — the file is what we intended, the process is what is true.

### ⛔ STANDING: `preflight_oms_restart.sh` runs BEFORE the OMS restart.

`/home/trader/ops_preflight/`, md5-identical to the repo copy. **It does not gate itself** — the
discipline is to run it. Bare Webull fills will exist; a restart without it can leave one uncovered.

---

## 2. Per-service evidence table — fill this in DURING the window

Take it twice: once before the pull, once after both restarts. A service whose process start is
EARLIER than its file-write time is running old code, whatever the diff says.

| service | file write (UTC) | process start (UTC) | running pulled code? |
|---|---|---|---|
| oms | | | |
| schwab-1m-v2 | | | |
| strategy | | | ⛔ expected NO — not in this window |
| market-data | | | ⛔ expected NO |
| control | | | ⛔ expected NO |
| reconciler | | | ⛔ expected NO |
| market-capture | | | ⛔ expected NO |

⛔ The four "expected NO" rows are stated deliberately. An unexpected restart of one of them is a
finding, and it can only be a finding if the expectation was written down first.

⛔ `preopen_readiness_cron.sh` is a real file, not a symlink — it re-diverges on every pull. Re-sync
it by hand after the pull and confirm the md5 matches the repo copy.

---

## 3. Acceptance — fixed in advance

Graded **Fri 08-21 am**, on a full session. ⛔ **A quiet Friday is a NON-RESULT, not a pass** — if
the population is too small to move any of these numbers, say so and re-grade Monday.

| # | signal | pre | expected | ⛔ stop condition |
|---|---|---|---|---|
| 1 | mirror **STOP_LIMIT rejects** | 720 since 08-14 | **→ 0** | any non-zero ⇒ #735 did not take |
| 2 | **orb entry fills/day**, with **Schwab's rate beside it** | 6–7 | **12–25** | — |
| 3 | `[WEBULL-BARE-FILL]` per session | — | **~9/day** | ⛔ **> 20 ⇒ STOP and report before Monday** |
| 4 | **duplicate legs per segment** | 19 of 119 | not worse | ⛔ **above 19-of-119 ⇒ STOP** |
| 5 | `[V2-DB-SEED-GAP-CENSUS]` denominator | `7 of 0` | **readable** — numerator ≤ denominator | a second `N of 0` ⇒ #743 did not take |
| 6 | seed-gap **fail-open occurrences** | 8 in 24h | **→ 0** | any `session-calendar lookup failed` ⇒ the timeout is still there |

### Signals that mean the OPPOSITE of the others

⛔ **#736's success is SILENCE.** Expect **zero** `[OCO-TARGET-BELOW-FILL]` lines. One appearing IS
the finding — do not read it as the feature working.

⭐ **#743's #6 is the load-bearing one, not #5.** The census denominator is cosmetic next to the
fail-open: a timeout takes the boundary check AND, through the aborted transaction, the internal-gap
checks on the same session. The internal-gap check is the one that matters — measured 08-18→08-20,
**37 of 43 truncations had no REST warmup behind them**, so for those symbols the refusal is the
only thing standing between stale bars and the strategy.

### ⛔ Every one of these states what it CANNOT see

- Reject counts are **contaminated** until Q1 (#746) is deployed — `broker_order_events` stores our
  own aborts as broker rejects. Signal 1 is "rejects that reached the mirror leg", not "Webull said
  no". Q1 is **built but NOT in this window**.
- Schwab-vs-Webull comparisons are **void structurally**, not since a date: when the Webull leg is
  present its order type is refused client-side, re-sent as a different type minutes later at a
  different price, and exits on its own OCO. Signal 2 puts the two rates **side by side**; it does
  not difference them.

---

## 4. NOT in this window

Built today, merged, deployed later. Listed so nobody grades tonight's restart against them.

| item | PR | needs |
|---|---|---|
| P21 — empty-tape trade drop | #744 | nothing live; changes what the replay reports |
| Q1 — abort vs broker reject | #746 | **a migration** (`20260820_0015`) — its own window |
| B19 / B20 — arm lifecycle | #747 | a v2 restart — **tomorrow's window** |
| B9 — §82 causes 2 and 3 | #745 | **design only, not built** |

⛔ B19/B20 need a restart and tonight is already full. Adding them would put an untested entry-side
state change into the same window as the acceptance for #735, and a failure could not be attributed
to either.

---

## 5. Rollback

| symptom | action |
|---|---|
| signal 1 non-zero, or bare fills > 20 | set the flag **false**, restart v2 only. The OMS code is inert without it. |
| signal 4 worse | flag **false** — the duplicate path is the fan-out leg |
| signal 6 non-zero | no rollback; #743 is strictly better than the timeout it replaces. Report and re-check the query plan on the box. |

⛔ The flag is the lever for 1–4. It is a v2-only restart, and it does not require touching the OMS.

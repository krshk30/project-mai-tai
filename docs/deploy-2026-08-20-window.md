# Deploy sheet — Thu 2026-08-20 evening window

**Written 2026-08-20 morning, before the window opens; §1, §1b, §1c revised the same day after the merges rescheduled the window.** Acceptance is fixed HERE, in advance, so
nothing gets graded against a number chosen after the result is visible.

> ⛔⭐⭐ **`src diff = 0` IS NOT EVIDENCE.** Since 2026-08-19 the box carries code it is not running.
> Every step below states the **file-write time and the process-start time side by side**, per
> service. The diff is not the check; the table is.

---

## 1. Order — OMS first, then v2. Not negotiable.

> ⛔⭐⭐ **MERGING IS SCHEDULING (§179).** Once merged, a change ships on the next deploy of whatever
> service it touches. B19/B20 were "tomorrow's window" and Q1 was "Friday's" — merging them put both
> on TONIGHT. The window contents below are the revised set, not this morning's.

| # | service | carries | why this order |
|---|---|---|---|
| 1 | **oms** | **#735** + **#736** + **#737** + **Q1 (#746)** + **migration `20260820_0015`** | #735 is what lets the Webull mirror leg through at all. If v2 restarts first with the flag on, it emits legs into an OMS that still stamps a Schwab bracket onto them and aborts them client-side — the exact 720-order defect, and the acceptance would fail for the wrong reason. |
| 2 | **schwab-1m-v2** | **#743** + **B19/B20 (#747)** + **the flag** | Restart only after the OMS is confirmed running the new code. |

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

## 1b. THE MIGRATION — three answers, read from the code, before 16:00

### ⛔⭐⭐ FIRST, THE THING NOBODY ASKED: Q1's CODE AND ITS MIGRATION ARE **NOT SEPARABLE**

`BrokerOrderEvent` now maps `event_source`, so SQLAlchemy emits it in every INSERT. Against the
pre-migration table that is **`UndefinedColumn` on every `broker_order_events` write** — verified,
not assumed, by pointing the shipped model at a table built to the OLD shape:

```
INSERT FAILED -> OperationalError
table broker_order_events has no column named event_source
```

⇒ **Q1 is "observability only" in its SIGNAL, not in its RISK.** If the OMS deploy runs without
migrations, the OMS ships code that throws on every order event it tries to record. This is the
mirror image of the rule we earned this week: there, code landed on disk unactivated; here, code
activates against a schema that isn't there.

### 1. Applied automatically, or by hand?
**Neither — an explicit opt-in, and it DEFAULTS TO OFF.** `deploy-service.yml` exposes
`run_migrations` with `default: false`; false ⇒ `MAI_TAI_RUN_MIGRATIONS=0` ⇒
`08_install_runtime.sh` prints *"Skipping alembic upgrade"*.
⇒ **The OMS deploy tonight MUST set `run_migrations: true`.** Left at the default, everything above
happens.

### 2. Is it reversible?
**Yes, mechanically.** `downgrade()` drops the index then the column; the upgrade is additive DDL
(`ADD COLUMN ... server_default 'unknown'` + `CREATE INDEX`) and Postgres DDL is transactional, so
the upgrade is atomic.
⛔ **But the ORDER of a rollback is not symmetric.** Downgrading while the OMS still runs Q1 code
re-creates the same break. The rollback is **revert the code and restart FIRST, then downgrade** —
never the reverse.

### 3. Before or after the restart?
**Before.** `deploy_service.sh` calls `08_install_runtime.sh` (which runs `alembic upgrade head`) at
line 149, and the `case` block that restarts units comes after it. Correct order for an additive
column: the schema exists before any process can write to it.

### ⛔ AND A HARD GUARD THAT DECIDES WHEN THE WINDOW CAN OPEN
```bash
if [[ "$RUN_MIGRATIONS" == "1" && "$IN_MARKET_WINDOW" == "1" ]]; then
  echo "refusing live service deploy with migrations enabled"; exit 1
fi
```
`IN_MARKET_WINDOW` = weekday **and** `07 <= ET hour < 16`. ⇒ the OMS deploy **cannot run before
16:00 ET** — it will abort, not warn. That is the right guard; it just means the window opens at
16:00 and not a minute earlier.

---

## 1c. ATTRIBUTION MAP — which signal belongs to which change

Four changes land tonight across four subsystems. Written BEFORE the window so no result gets
claimed by the wrong one.

| change | subsystem | its signal, and ONLY its signal |
|---|---|---|
| **#735 mirror + flag** | OMS fan-out / Webull | orb entry **fills/day**, mirror **STOP_LIMIT rejects → 0**, `[WEBULL-BARE-FILL]` count |
| **#743 seed calendar** | v2 db-seed | **readable census denominator**, **fail-open occurrences → 0** |
| **B19/B20 (#747)** | v2 arm lifecycle | `[V2-ENTRY-WINDOW-ARM-RELEASE]` at the 16:00 boundary, `[V2-CW-DISARM] reason=watchlist-removed` on removal |
| **Q1 (#746)** | OMS event recording | a **populated `event_source` column** — observability only, no behaviour change |

⛔ **#736 has no positive signal at all** — its success is the ABSENCE of
`[OCO-TARGET-BELOW-FILL]`. It is not in the table above because it cannot be graded by something
appearing. One line appearing IS the finding.

⛔ **B20 cannot affect tomorrow's entries.** It fires only after the entry window has closed, so no
entry decision on 08-21 can be attributed to it. B19 can affect them (a re-joining symbol arrives
disarmed rather than pre-armed) — if entry counts move, B19 is the candidate, not the mirror flag.

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

- Reject counts are **still contaminated for TONIGHT'S grading** even though Q1 deploys with this
  window. The column starts populating from the deploy forward; every pre-existing row is `unknown`,
  and signal 1's "720 since 08-14" baseline is entirely pre-column. ⇒ Signal 1 remains "rejects that
  reached the mirror leg", NOT "Webull said no". **Q1 is deployed tonight and PROVEN later** — a
  populated column is not the same as a clean split, and a count spanning the migration boundary
  must never be read as one.
- Schwab-vs-Webull comparisons are **void structurally**, not since a date: when the Webull leg is
  present its order type is refused client-side, re-sent as a different type minutes later at a
  different price, and exits on its own OCO. Signal 2 puts the two rates **side by side**; it does
  not difference them.

---

## 4. NOT in this window

Built today, merged, deployed later. Listed so nobody grades tonight's restart against them.

| item | PR | why not |
|---|---|---|
| P21 — empty-tape trade drop | #744 | nothing live; changes what the replay reports |
| B9 cause 3 — the phantom-close latch | #745 | **design approved, build not started** |
| the unified gap check downstream of both feeds | — | Q11 came back **6 of 43** ⇒ not urgent |
| P2 replay rebuild | — | needs redoing anyway: **P21 changed what the replay reports** |

⛔ **B19/B20 and Q1 ARE in tonight** — see §1. This morning's sheet said otherwise; merging changed
it. The signals stay separable because they land in four different subsystems (see the attribution
map above), and B20 cannot touch tomorrow's entries: it fires only AFTER the entry window closes.

---

## 5. Rollback

| symptom | action |
|---|---|
| signal 1 non-zero, or bare fills > 20 | set the flag **false**, restart v2 only. The OMS code is inert without it. |
| signal 4 worse | flag **false** — the duplicate path is the fan-out leg |
| signal 6 non-zero | no rollback; #743 is strictly better than the timeout it replaces. Report and re-check the query plan on the box. |
| **`broker_order_events` write errors after the OMS restart** | the migration did not run. ⛔ **Revert the OMS code and restart FIRST**, then decide on the migration — do NOT downgrade under running Q1 code. |
| arms not released at 16:00, or a symbol re-joining already armed | B19/B20 — v2-only restart on the previous commit. Independent of the flag. |

⛔ The flag is the lever for 1–4. It is a v2-only restart, and it does not require touching the OMS.

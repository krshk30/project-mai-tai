# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-25 21:30 ET**, read-only against production. Every number below
carries its as-of time. Awaiting independent review by `codex-2` before merge.

**Corrected by `claude-1`, 2026-08-26 07:55 ET**, after `codex-2`'s review of #794 returned BLOCKED
on five findings: the obsolete carry-note block was removed from `selftest.sh` (110/0 → **105/0**,
five false passes gone), the backfill "trend" claim was reduced to what is proven, the
"nothing was broken code" claim was scoped to the eight reporting failures, the split-delivery
claim in the commit body was corrected, and the gate section below now names the current hashes
instead of the superseded pin. Production was not touched.

---

# PRODUCTION — main and box are IN SYNC

| | |
|---|---|
| main / box | **`2bbe5ccc4419ed895be8a806d6e14616d33dbc58`** |
| open PRs | **0** |
| flat | ✅ both ledgers, `assert_fleet_flat` exit 0 |
| account_positions / virtual_positions / open orders | 0 / 0 / 0 |

**PIDs as of 21:25 ET (01:25 UTC 08-26):**

| service | pid | since (UTC) |
|---|---|---|
| oms | 1521794 | 2026-08-26 01:15:00 |
| strategy | 1521806 | 2026-08-26 01:15:00 |
| schwab-1m-v2 | 1522331 | 2026-08-26 01:18:51 |
| reconciler | 1514317 | 2026-08-25 23:51:35 |
| **control** | **44840** | **2026-07-14 12:18:04** ← never restarted |
| market-data | 1528374 | 2026-07-27 17:35:52 |

`NRestarts=0` on every unit. Fleet `/health` healthy.

---

# ⛔⭐⭐ EVERY MARKER SHIPPED TONIGHT READS ZERO — THAT IS THE CORRECT STATE

Counted on the box at **21:25 ET**, before any session has run against them:

```
[OMS-CANCEL-PAIR-REQUEST]       0     [V2-RECLAIM-SLOT-CHECKED]       0
[OMS-EXIT-RELEASE]              0     [V2-RECLAIM-UNION-ONLY-PASSED]  0
[OMS-CHILD-EXIT-ATTRIBUTION]    0     [BROKER-SYNC-OK]                0
```

**This is UNEXERCISED, not passed.** It is recorded deliberately so tomorrow's reading has a real
baseline instead of an assumed one. A zero tomorrow means nothing without its denominator.

---

# 🔴 GRADE AFTER THE CLOSE — NEVER MIDDAY

⛔ Today proved it: **signal 6 read 0 at 12:58 ET and 1 by 16:34 ET.** A must-be-zero signal cannot
be graded mid-window; mid-window is *not yet failed*, never *passed*.

| # | check | how to read it |
|---|---|---|
| 1 | **Signal 6** (#781) — `session-calendar lookup failed` must be **0** | state the denominator: `[V2-DB-SEED-GAP]` line count **and** the census `truncations=N of M` |
| 2 | **#780** reclaim markers | read `[V2-RECLAIM-SLOT-CHECKED]` (denominator) **before** `[V2-RECLAIM-UNION-ONLY-PASSED]` (result) |
| 3 | **#791 C1** cancel confirmation | a real `[OMS-CANCEL-PAIR-REQUEST] requested=N`, and **no `[OMS-EXIT-RELEASE]` without `release_confirmed=1`** |
| 4 | **#791 C2** child attribution | read `[OMS-CHILD-EXIT-ATTRIBUTION] evaluated=1` **before** `attributed` |
| 5 | **#790** signal 4 | tonight 10 total / 2 attributed / **8 unattributed** = UNEXERCISED. Does `attributed` RISE? |
| 6 | **#783** refresh-count watch | first run **06:15 UTC** |
| 7 | **#785** phantom-row counter | every 5 min, weekdays — data by morning |
| 8 | **#787** retention | ⛔ the 22:30 UTC run must print **"already matches the normalized source; no replacement needed"**. A *second successful install* means the drift check is broken. Success looks like a no-op. |
| 9 | **#788** health gate | proved itself twice tonight; watch on the next restart |
| 10 | **#759** recovery split | `[BROKER-SYNC-OK]` has **still never been emitted**; `rotate 30` now gives a 30-day window instead of 7 |

---

# PROMOTION GATE — CORRECTED, SUBMITTED, NOT YET PINNED

| file | sha256 | state |
|---|---|---|
| `promote.sh` | `421d49f868c89284a699dca898c4ceec74b6038e294d435e98ffd9fdea15993a` | submitted for review |
| `selftest.sh` | `793f9403b3f8cca40ab0b34c4b36a6f0338bd4dfe12c117d3d99f69f08b67baf` | **105 passed / 0 failed**, `MAI_TAI_REPO` set |

These supersede `promote.sh d52a8a72…` / `selftest.sh c662a72f…` (98/0), which were pinned before
the carry-note defect was found. Both hashes were re-read unchanged after the full run.

⛔ **`checksums.sh verify` is RED and must stay RED until `codex-2` re-pins it.** I authored these
files; an author re-pinning their own gate turns the gate into a rubber stamp. RED here is the gate
failing closed and is the correct reading, not a fault to silence.

⛔ `selftest.sh` ran **105/0, not 110/0.** The difference is not a regression: an obsolete five-case
carry-note block was removed on 2026-08-26. It rebuilt the production `printf` by hand instead of
executing it, so it passed even with the claim path deleted — five passes that proved nothing. 105
is the reconciled expectation.

⛔ `promote.sh` does **not** read the checksum file (0 references), so a RED verify does not
mechanically block `./promote.sh`. The block is a decision, not a mechanism.

---

# OPEN ITEMS — no owner yet

**(a) HTTP 417 false-success — FIXED but UNPROVEN.** #791 closes it; 0 evaluated tonight. Needs a
real software-close episode.

**(b) DAIC ledger gap.** Historical child/time/price remains **COULD_NOT_TELL by design** — #791 is
future-protection only. Evidence preserved at `/home/trader/daic-phantom-2026-08-25.txt`.

**(c) ⚠ v2 restart backfill burst is RECURRING, not one-time.** Tonight 9,707 lines in 33s across 4
symbols; bars ~7.5 days stale, correctly dropped, zero errors after. Prior days: 8297 / 3235 / 4887
/ 209 / 3905 / 867. **What is proven is RECURRENCE, and that tonight is the largest instance — not
a trend.** The sequence is volatile (it falls to 209 and back), and 7 points with no denominator
(restart count, symbol count, staleness depth) cannot carry a direction. ⛔ The 08-26 01:26Z journal
entry calls it "TRENDING UP"; that phrasing is superseded by this line. Matters because 9,707 lines can
mask a one-line signal — today's signal-6 failure *was* one line in this same log — and because it
sits in the seed path #781 just rewrote.

**(d) Control service — pid 44840 since 2026-07-14.** 1.7 GB RSS after six weeks, 10 newer
startup-required files. **Verified NOT a token risk.** Tonight's OMS deploy deliberately left it
alone. When restarted: after hours, straight after an observed refresh, then prove new PID ·
refresher enabled/healthy · token-store metadata intact · **a new `[SCHWAB-TOKEN-REFRESHED]` within
+35 min** (UNMEASURED before that; refreshes run ~every 29 min, 48–50/day).

**(e) 📌 Capacity — MEMORY-bound, deferred by the operator.** Basic 4 vCPU / 8 GB / 120 GB.
7.1G of 7.8G used, **zero swap**, disk only 25% used (**do not grow the volume — irreversible**).
⭐ Two free fixes before buying RAM: control's 1.7 GB after six weeks (a resize would restart it and
*silently reclaim* the memory, masking the cause), and `shared_buffers=160 MB` with an **80.69%**
cache hit ratio against a 15 GB DB — very likely the real cause of today's signal-6 timeout
(`COUNT(DISTINCT)` over a 1001 MB table, 1603 ms warm vs a 5 s `statement_timeout`).

**(f) Signal 4 blind spot** — 8 filled fan-out legs still carry no segment id.

**(g) Stale remote branches** — many refs ahead of main with no PR, one at +50. Cosmetic.

---

# ⛔ DISASTER RECOVERY — GitHub restores the machine, NOT the data

`ops/bootstrap/01…10` plus 11 systemd units rebuild a droplet from empty. **Not in git and with
no backup:** the **14 GB** database (18,652 fills · 79,169 broker_orders · **1,229,033 bars**),
`/etc/project-mai-tai/project-mai-tai.env` (12 KB of credentials), the Schwab token store, and 46 MB
of logs. **Zero `pg_dump` backups exist**, and there is no backup/restore tooling anywhere in the
repo. Operator is enabling droplet backups. ⚠ Take a manual snapshot *before* any resize — periodic
backups give no restore point until the first one runs.

---

# WORKING RULES THAT COST US TIME TODAY

- ⛔ **A number that does not reconcile is the tell.** A test total that did not move after adding
  seven tests meant they were never executing.
- ⛔ **An empty result for an identifier you guessed is not an absence** — it is an unasked question.
  Two false negatives today came from an invented branch name and an invented unit name.
- ⛔ **Verify the artifact, not a copy of it.** A `/tmp` harness passed 7/7 while the suite ran none.
- ⛔ **`--squash` destroys ancestry.** Verify content on main and the resulting commit, never the PR
  record or an error alone. A 502 can follow a completed merge.
- ⛔ **Disjoint files are not independent** — check for interaction explicitly.

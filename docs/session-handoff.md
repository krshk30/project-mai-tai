# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Written by `claude-1`, 2026-08-28 17:14 ET; corrected 18:55 and 19:2x ET.**
⚠ **This file has now been wrong three times** — a conflated count, an asserted unknown, and two
stale lines. ⭐ **A state document decays faster than the code it describes.** Prefer regenerating
it at close-out over patching it. ⚠ **This is a MID-SESSION INSURANCE SNAPSHOT, not a
close-out.** No freeze, no manifest, no promote. The day is still running.

---

# PRODUCTION — main is AHEAD of box by one test-only PR

| | |
|---|---|
| main | **`a44c894a5b4ad531c5d008e9f309397bf1d62506`** |
| box | **`69d622783e56030b8075169cc2100c8f721a0dbd`** |
| the gap | ⭐ **#838 only — `tests/integration/` + `validate.yml`. Nothing to deploy.** ⛔ Do not read this as a pending release |
| checkout | clean |
| alembic head | **`20260828_0016`** — first migration in weeks; `ix_dashboard_snapshots_type_created_id_desc` confirmed present |
| open PRs | **5 — #827, #828 (both re-submitted, under review), #837 (this doc), #839, #840.** Everything else merged |

**PIDs as of 17:14 ET. `NRestarts=0` on every unit.**

| service | pid | moved today? |
|---|---|---|
| oms | 1982227 | ✅ twice — #825/#829/#830/#833 then #832 |
| strategy | 1982263 | ✅ twice (same windows) |
| market-data | 1974639 | ✅ once (#833) |
| control | 1974293 | ✅ once (#833) — token refresher restarted with it |
| **schwab-1m-v2** | **1884057** | ⛔ **DELIBERATELY UNTOUCHED since 06:58 ET.** #827 is the only PR that would move it, and it is blocked |
| reconciler | 1811867 | — |

⭐ **Token refresh after the control restart: +53 s, `refresh_token_expires_at` unchanged at
`2026-08-31T20:02:05Z`.** ⛔ That date still needs a HUMAN — re-auth Monday 08-31.

---

# ⭐⭐ WHAT SHIPPED TODAY — 11 MERGED AND DEPLOYED (as of 18:55 ET), none of it proven

| PR | what | exercised? |
|---|---|---|
| #824 | fan-out claim: durable evidence beats a transient zero, 24.119 s bound | ⛔ **0/0/0 over 0** |
| #825 | N3 producer-side: defer a virtual-position clear younger than 24.119 s | ⛔ UNEXERCISED |
| #829 | bound repeated cancels against a dead target (intent path) | ⛔ would have fired **zero** times in all retained history |
| #830 | unknown `updated_at` defers instead of clearing | ⛔ **DESIGNED, DEFENSIVE, UNREACHABLE** — the column is NOT NULL |
| #832 | same bound extended to 4 of 5 direct cancel submitters | ⛔ UNEXERCISED |
| #833 + #835 | Schwab reports carry `origin=broker`; per-site controls | partial — see the reach limit below |
| #834 + #836 | monotonic snapshot ordering + its composite index | ✅ active immediately |
| #826 | EOD slot reporting: COULD_NOT_TELL instead of a false `0.0%` | ✅ installed, hashes `113b86ec` |
| #831 | EOD cron wrapper adopted byte-for-byte then hardened | ✅ **installer ran 17:11 ET**, root-owned, executable, in root cron |

⛔ **The count is MERGED-AND-DEPLOYED PRs, not reviewed ones.** The 11 above are on the box.
**#838 is a 12th merge** (`a44c894a`) but is **tests and CI only — nothing to deploy**, which is
why it sits outside this table. **#827 and #828 were reviewed and BLOCKED** (7 and 6 rounds).
⇒ **12 merged today, 11 deployed, 2 blocked.**

⚠ **This line has now been wrong twice.** First it said "13 shipped", conflating review work with
shipped work — `codex-2` caught it against main. Then the correction itself went stale ninety
minutes later when #838 merged. ⭐ **A count is only true as of a timestamp; write the population
AND the as-of, or it rots.**

⛔ **Say `UNEXERCISED`, never a bare zero.** A quiet log tomorrow is not evidence for any of these.

---

# 🔴 BLOCKED — and the reasons are the useful part

## #827 — v2 boot guard (round 7)

The same vacuity has now relocated **five times**: `not dangerous` on `[]` → `all([])` → `0 != 0` →
a sticky once-ever flag → the pager. Round 7 killed 12 of 12 requested mutants. Still open:

1. ⛔ **`sh()` returns `None` on an EXCEPTION but `""` on a non-zero exit.**
   `sudo tr` on `/proc/<dead-pid>/environ` **exits 1 with empty stdout and does not raise** — the
   MainPID→`/proc` race during a restart. Result: `SKIP: flag OFF`, exit 0, **muting every branch
   including the dangerous-armed-segment page.** ⛔ The flag is genuinely `true` on the box (env file
   AND v2's live process environ), so this is ACTIVE, not latent.
   ⚠ **claude-1's spec said "the read fails"; codex implemented "the read raises."** The wording was
   the constraint. Say *raises OR non-zero OR empty*.
2. 🔴 **Mutant j2 survives all 2,741:** `_webull_ineligible_symbols` reverted to the cold cache
   **releases the hold AND emits the leg.** The loader-level test exists for **Schwab only**.
   Same broker-scope asymmetry as ever, inverted — the scope was added, one side was tested.
3. ⛔ **The fan-out branch is a venue change and it DROPS legs.** The Schwab leg emits first, the
   Webull mirror is then refused ⇒ **manufactured leg divergence**, the exact thing fan-out exists
   to prevent. `drain()` has emptied the queue and the refusal path does not requeue. The PR body
   says "no venue behavior changes" — false.
4. **`restoration_complete=0` is a hardcoded literal** that is false after the latch, so anyone
   grepping it counts mid-session DB blips as boot failures.
5. ⛔ **NEW:** a transient exclusion-read failure **freezes the watchlist mid-session with entries
   OPEN** — v2 keeps entering on names the scanner dropped, and departed symbols keep `cw_armed`.
   That is the B19 shape the code's own comment documents.

## #828 — D6 scheduler (round 6)

Six rounds of fixing named mutants; each time a one-token variation survived.
⭐⭐ **The root cause is now FIXED, and not in this PR.** It was that *the SQL was never executed
against a database anywhere in this project* — so a text check was the only proof available, and a
text check is always one edit behind. **PR #838 merged that gap shut** (`a44c894a`): CI now runs a
real PostgreSQL 16, and the three known leaks plus three novel spellings die on a **runtime
row-count assertion**, not a string anchor.

⇒ **#828 is rebased onto the harness at `1df67270`. Its window argument is over.**
⛔ **Its OTHER blockers stand and are untouched by the harness:** five installer guard lines tested
only at `:44` (`:18`, `:42`, `:43`, `:61`, `:87` all survive `|| true` at the call site, and `:42`
survives outright deletion); `install_…sh:9 target_dir` unpinned for a third round; and the
`_denominator_contract` needles unexercised — emptying the `refused_exits` tuple leaves the suite
green while a report with no `post_exit_episodes=` denominator is accepted as `denominators=present`.

---

# ⭐⭐ DECISION ON RECORD — the Postgres differential harness

> A syntactic check must enumerate every phrasing of a mistake; a differential test asks the
> database **once**. **Six rounds of text-checking a leak is the tell, not the cost.**
> Same lesson as `-c` versus `-f -`, and as #772: **you cannot statically prove a runtime property.**

✅ **DELIVERED — PR #838, merged `a44c894a`.** Verified by execution against a real PostgreSQL
16.4. Three known leaks and three *novel* spellings all die at runtime.
⛔ **But the green is NARROWER than it looks:** `target_refused`, `target_mirror_symbols`,
`target_matched_orders` and identity's `order_rows` read populations that are **empty in the seed**
— they are **UNEXERCISED, not passing**, and that is stated in `tests/integration/README.md`.

**Scope, operator-set:**

- ⛔ **Build it for the SECOND customer.** The reuse population is **six** Python acceptance scripts
  (`fanout_identity_acceptance`, `fanout_outcome_acceptance`, `fanout_pair_identity_acceptance`,
  `field_acceptance`, `post_exit_stale_held_acceptance`, `fleet_health_check`). Cover a second one
  in the same PR — **`fanout_identity_acceptance.py`, the script that had the `-c` bug.** If the
  second isn't cheap, the harness isn't reusable, and doing one is the only way to find out.
- ⛔ **The control must FLIP:** one row inside the window, one outside, and the two verdicts must
  **differ**. A test where both rows return the same thing proves nothing — the fixture trap.
- ⛔ **Import the module's real `SQL`**, never a copy. A copied query drifts, and then the test
  proves a string nobody runs.
- CI had no `services:` block. `tests/integration/` already existed — it went there. ✅ done

---

# ⛔ TESTS OWED — the behaviour is correct, the guards are missing

| where | the missing guard |
|---|---|
| **#831** | the file-existence latch mutant **survives and is NOT equivalent** — seeding a partial canonical report makes it push an empty verdict forever |
| **#832** | `test_missing_target_row_fails_open…` **exercises the wrong function**; inverting the real gate survives 2,660 tests |
| **#832** | bounded-drift-abandons-its-intent is unpinned — the mutant leaves a stuck `submitted` intent that occupies the #644 cap and suppresses entries **with zero broker traffic to reveal it** |

---

# FINDINGS NEEDING AN OWNER

- ⛔ **`event_source` is still ~91% `unknown`.** #833/#835 fix **Schwab only**: `live:orb` 857
  unknown, `paper:polygon_30s` 476 unknown ⇒ **1,333 of 1,428 remain unclassified.**
  ⛔ **No `event_source='broker'` filter is safe anywhere** — at 1% Schwab classification it would
  discard the population.
- ⛔ **The 14 "duplicate fan-out groups" are NOT gradeable.** Spreads are **235 s–3,209 s** (4–53
  min) — a claim-release duplicate is sub-24 s — and the grouping key `fanout_slot` is **empty** on
  four symbols. Most are probably first-plus-reclaim. **D6's `duplicate_legs` and its `22/22 median
  4.58%` baseline inherit the same degenerate key.** Regroup on `cw_entry_slot`; it cannot be
  regraded historically (0 coverage before 08-28).
- ✅ **We DO hold every sell** — all 7 duplicate symbol-days net exactly 0. **No naked positions.**
- **D3's ceiling for duplicates is 57%** — 8 of 14 groups had Schwab holding nothing; 6 had Schwab
  visibly holding, where a cross-venue position map would not have helped.
- **A2 slot-scope count: ≥2 extra entries over 10 sessions** — ⛔ a **floor, not an estimate**;
  replay stops at the first fill per symbol-day, so the ceiling is unknown.
- ⛔ **When #828 lands, its installer must run in the SAME window** — `fleet_health_cron.sh` runs
  the checker straight from the checkout, so the D6 freshness check arms on repo sync and pages RED.
- **No watchdog exists for "cron never fired."** A dead cron and a weekend are indistinguishable.
- **S0: the DB credential exposed in a task transcript on 08-26 is still unrotated.** Owner: operator.

---

# 🔴 WATCH TOMORROW

1. **`review-pin-audit` 06:15 UTC must be GREEN** — 12 pins were recorded today (the 11 merged plus #838).
2. **`cw_entry_slot` coverage as a fraction.** Today's denominator: **0 of 239 Schwab, 0 of 301
   Webull** BUY fills. Any non-zero numerator is the first gradeable composition reading ever.
3. **`[VIRTUAL-CLEAR-DEFERRED] deferred=N of unbacked_positive=M`** — the deferred-then-restored vs
   deferred-then-cleared ratio is the first direct measure of how many of 143 clears were wrong.
4. **The #824 / #829 / #832 markers, each with its denominator.** ⛔ The direct-cancel marker fires
   **once per (target, path) per process** while #829's fires on **every** refusal — never count
   them against each other. `[OMS-CANCEL-UNCONFIRMED]` carries **two opposite meanings**, split by
   `dead_target_bound=0|1`.
5. **D6: claude-1 hand-runs it after each close**, labelled `manual run`, until #828 ships.

---

# OPERATIONAL RULES CONFIRMED TODAY

1. ⛔ **No v2 or OMS restart between 07:00 and 16:00 ET.** The entry window is
   `entry_window_start_hour_et=7` / `end_hour_et=16`, **no prod override — not 09:30.**
2. ⛔ **Rebase, never GitHub's "Update branch."** Its merge commit carries no agent marker and the
   review gate refuses the whole range. And **delete the superseded pin record in the same commit** —
   an orphaned record fail-closes the PR in CI while only warning locally.
3. ⛔ **A local `verify` PASS is not evidence CI will pass.** The reviewer's object store still holds
   the orphaned commit; CI's does not.
4. ⛔ **Never `--admin` a behind branch** — `base.sha` becomes main's tip, which is not an ancestor
   of the reviewed head, and the audit is then permanently `COULD_NOT_TELL`.
5. ⭐ **Split adoption from hardening.** `eod_counts.py` (`c6d4cea4`) and `eod_cron.sh` (`380f1fc8`)
   both stayed provably byte-identical to the box across four rebases because of it.
6. ⛔ **A batch is not a queue** — parallel lanes, a file-collision map, and a merge order, because
   merging is serial even when building is not.
7. ⛔ **Report what you verified, not what you intended.** Four "fixed" claims were refuted by a diff
   or by a surviving mutant today.
8. ⛔ **Fixing the named mutant is not fixing the class.** Five rounds on #828 and seven on #827 both
   turned on exactly this.

## Memory pointers

`[[project-mai-tai-context]]` · `[[project-mai-tai-fleet-roster]]` ·
`[[project_mai_tai_review_pin_gate_mechanics]]` · `[[feedback_batch_of_tasks_means_parallel_lanes]]` ·
`[[feedback_who_else_writes_this_state]]` ·
`[[feedback_an_absence_is_evidence_only_against_a_known_denominator]]` ·
`[[project_mai_tai_virtual_positions_false_zero]]` · `[[feedback_unexercised_is_not_a_result]]`

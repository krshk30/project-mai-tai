# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Two authors, two scopes — both recorded:**
- **`codex-2`, 2026-08-25 06:25 ET**, read-only against production: the original refresh. No
  account-position or open-order claim was made in it.
- **`claude-1`, 2026-08-25 10:29–10:33 ET**, read-only against production: the Schwab token/restart
  section (rewritten — the previous version's conclusion was a production restart that is not owed),
  the PR state table, and the #770 lifecycle correction. No account-position or open-order claim was
  made either.

⛔ Provenance matters here because the two authors DISAGREED on the restart question and the later
evidence won. A single-author header would have hidden that.

---

# ✅ SCHWAB RE-AUTH IS ACTIVE — NO RESTART IS REQUIRED

> ⛔⭐⭐ **This section previously said the opposite** ("re-auth is persisted but not active in OMS,
> strategy, or v2 — a restart must happen after the token-store mtime"). That was WRONG for the
> production configuration, and acting on it would have meant an unnecessary restart of OMS —
> which owns the exits, and is one of the two changes this system treats as how it loses money.

`SchwabBrokerAdapter.__init__` is **not** the only caller of `_load_token_store()`. There are three:

| site | when it runs |
|---|---|
| `schwab.py:146` | `__init__` |
| **`schwab.py:958`** | **every `_get_access_token()` that needs a refresh, in refresher-owned mode** |
| `schwab.py:999` | dead-token grant recovery |

Production runs refresher-owned mode — `MAI_TAI_SCHWAB_ADAPTER_TOKEN_REFRESH_ENABLED=false` — so
`_get_access_token()` takes the pure-reader branch and **reloads the store from disk**. The
dedicated refresher (control service) owns freshness and the adapter never writes. A later re-auth
file is therefore picked up by the RUNNING process — no restart needed to see it.

⛔ **Precisely, because the imprecise version is dangerous:** the adapter does not reload on every
call, and it does NOT refuse an expired token. It returns its cached token untouched while
`_access_token_needs_refresh()` is false; only once the cached token reaches the refresh window
(or `force_refresh`) does it reload from disk. And if the STORE itself is stale, it logs
`[SCHWAB-TOKEN-STALE]` and **returns the expired token anyway** — deliberately, so a refresher
outage surfaces as a named warning instead of a silent 401 storm. ⇒ "the adapter never caches past
expiry" is FALSE. A live `[SCHWAB-TOKEN-STALE]` count means the refresher is down and needs a human,
and it is the signal to watch — not the restart.

**Evidence, as of 2026-08-25 14:33 UTC (10:33 ET), read-only:**

- `refresh_token_expires_at` = `2026-08-31T20:02:05Z` → **Mon 2026-08-31 16:02 ET** (weekday derived, not labelled)
- `expires_at` = `2026-08-25T14:55:32Z` — in the FUTURE, so the refresher is actively rotating the access token
- `[SCHWAB-TOKEN-STALE]` in the **current** log: **0**. ⛔ All 386 occurrences are in
  `oms.log-20260823.gz` — a past 08-23 incident, not today. (A concatenated `oms.log*` stream is in
  FILENAME order, not time order; `tail` on it returns the rotated file's end and reads as current.)
- `[BROKER-SYNC-CENSUS]` newest line `2026-08-25 14:30:15` with `failed=0` — broker reads are landing

⇒ **No restart is owed for the token.** If a restart happens for another reason, that is fine, but
the token is not the reason. ⛔ The next real deadline is the REFRESH token: **Mon 2026-08-31 16:02
ET**, and only that one needs a human — `expires_at` is the short-lived access token the refresher
rotates itself, and is a ready-made false alarm.

---

# PRODUCTION — THE 2026-08-24 BATCH IS MERGED AND DEPLOYED

**VPS HEAD `a4235a653aa82907e4e124f97a49fc07c374203a`, clean at 06:24 ET.** The intended batch is
`#769 → #766 → #758 → #755 → #774 → #761 → #771`; #760 and #773 were closed as superseded by
#774. Final tree `6b12b7a79…` matched the independently squash-assembled tree.

| unit | PID | NRestarts | active since UTC | current read |
|---|---:|---:|---:|---|
| OMS | 1290662 | 0 | 08-24 21:28:24 | `/health` healthy at 06:25 ET |
| strategy | 1290668 | 0 | 08-24 21:28:24 | `/health` healthy at 06:25 ET |
| schwab-1m-v2 | 1307928 | 0 | 08-25 00:28:51 | **degraded** at 06:25 ET |
| market-data | 1528374 | 0 | 07-27 17:35:52 | active/running |
| control | 44840 | 0 | 07-14 12:18:04 | active/running |
| reconciler | 141918 | 0 | 08-14 20:36:41 | `/health` healthy at 06:25 ET |
| market-capture | 3631762 | 0 | 07-08 06:47:03 | active/running |

The v2 degradation was **not** a loop crash: `quotes_live=true`, streamer connected,
`loop_health=healthy`, `loop_exceptions_total=0`, watchlist 3. Its reason was
`data_flow=stalled_offhours_rest_dry` at 06:25 ET with `market_session=premarket`. Recheck at the
07:00 ET entry boundary; do not rewrite this as healthy from the supporting fields.

## Deploy proof

- OMS run **32779632680**, migrations false, installed `bb696138…`; restarted OMS and strategy.
  OMS produced a fresh healthy heartbeat in ~33s. Strategy missed the generic 60s SLA, first fresh
  heartbeat ~113s and healthy ~181s; the operator explicitly accepted that one late recovery.
- v2 run **32793781395**, migrations false, installed final main `a4235a653…`; source write
  00:28:43Z → PID start 00:28:51Z → fresh healthy heartbeat 00:29:22Z. OMS/strategy did not restart.
- `Deploy Main` remains prohibited. Use `Deploy Service`; the v2 dispatch value is
  **`schwab-1m-v2`** with hyphens.

---

# #739 GRADE — DATA POINT 1, NOT A VERDICT

Read-only close grade at 2026-08-24 20:08 UTC:

- signal-4 control reproduced **119 / 19 / 22**;
- **0 duplicate segments of 2 measurable**, 0 extra legs;
- **7 filled fan-out legs lacked a segment ID**, so they were outside the measurement;
- signal 6 recorded five refusal-guard actions and zero calendar fail-open events, but its latest
  census was **0 of 0** — `COULD_NOT_TELL`, not PASS.

After deploy, the BUY-filtered signal-4 read stayed 2 measurable / 0 duplicate / 0 extra with the
same 7 blind legs. This is not worse than baseline and is still not enough population to grade #739.

---

# ACTIVE REVIEW / MERGE WORK

**Corrected by `claude-1`: 2026-08-25 10:29 ET.** Everything in this section below the #770 entry was written
before six PRs changed state on 08-25. It is rewritten to current truth, not amended.

## ⛔⭐⭐ #770 — CONTENT ON MAIN, LIFECYCLE NOT COMPLETED

The four handoff documents ARE on main as `06c17018`. Nothing else about #770 went to plan:

| claim | truth |
|---|---|
| PR recorded as merged | ⛔ **No** — `state=CLOSED`, `mergedAt=null`, `mergeCommit=null` |
| head branch auto-deleted | ⛔ **No** — `claude/handoff-0824-window` still exists at `dc58b9d2` |
| manifest committed | ⛔ **No** — `docs/handoff-manifest/2026-08-24.md` was absent from BOTH main and `dc58b9d2` |
| independently reviewed | ⛔ **No** — no recorded review of #770 |
| authorship attributable | ⛔ **COULD_NOT_TELL** — `dc58b9d2` carries **zero** `Co-Authored-By` trailers |

The content is genuine: `06c17018`'s tree and `dc58b9d2`'s tree are byte-identical (`f32a9d21ba12`).
⛔ But identical trees prove the DOCUMENTS landed, not that the BATCH was promoted.

⛔⭐⭐ **#770's authorship cannot be proven retroactively and never will be.** `dc58b9d2`
carries no `Co-Authored-By` trailer and nothing added later can supply one — under the agreed
rule it is permanently `COULD_NOT_TELL`. **PR #776 is a NEW REPAIR PR.** It supplies the
missing manifest and corrects the record. It is NOT evidence that #770's lifecycle completed,
and must never be cited as such.

⛔⭐⭐ **A `--squash` merge leaves no ancestry link to its branch**, so `compare/main...dc58b9d2`
reads `diverged ahead=10 behind=1`. That is the squash signature, NOT evidence of a failed merge —
the same mechanism that orphaned #773 and #760. It will recur on anything stacked.

⛔ How the false report happened, recorded so the shape is recognisable: a 502 during the merge call
left the record `OPEN`, and the two facts cited as proof of a merge were both **false negatives of
the reporter's own making** — a branch existence check run against an invented branch name, and a
service check run against an invented unit name. An empty result for an identifier you guessed is
not an absence; it is an unasked question.

**This batch is NOT promoted. Do not rotate the journals. Do not promote.** The repair path is a
small PR from current main carrying this correction plus the generated manifest, then independent
review of the Codex range, then explicit operator GO.

## PRs that changed state on 2026-08-25 (all times ET)

| PR | was | now |
|---|---|---|
| #775 retention + freshness | "Claude must re-review" | **MERGED** `0be129b0` 07:35 |
| #756 preflight fences | BEHIND, deferred | **MERGED** `6ca816ec` 09:25 |
| #759 broker-blind pager | BEHIND, deferred | **MERGED** `d270a1eb` 09:30 |
| #763 feature acceptance | BEHIND, deferred | **MERGED** `dd6c0d6c` 09:32 |
| #772 probe read-only guard | BLOCKED | **CLOSED unmerged** 11:58 — four AST rounds failed; the right control is a runtime read-only credential or a sandbox, not a fifth denylist rule |
| #770 handoff | open | content on main, lifecycle incomplete (above) |

⛔ **None of the four merges is deployed.** Production is `a4235a653`; main is `06c17018`. OMS pid
1290662 and strategy pid 1290668 (both since 08-24 21:28 UTC) and v2 pid 1307928 (08-25 00:28 UTC)
are unchanged — zero restarts on 08-25. All four are `ops/`/`docs/` only, so no restart is *owed*,
but `ops/bootstrap/08_install_runtime.sh` runs DURING a deploy, so **the next deploy is the first
time the corrected link step executes.**

⛔ **The shell gate itself is a different thing and the deploy never runs it.** `08_install_runtime.sh`
only INSTALLS/LINKS `ops/preflight/preflight_v2_restart.sh` (`ln -sfn` into `/home/trader/ops_preflight`);
nothing in the deploy path executes it. The gate Deploy Service actually runs is the PYTHON one —
`src/project_mai_tai/deploy_preflight.py`, via `run_live_preflight()` in `ops/systemd/deploy_service.sh:145`,
and only when **all three** of `HIGH_RISK=1`, `ALLOW_LIVE_RESTART=1`, `IN_MARKET_WINDOW=1` hold.
⇒ A green deploy is NOT evidence that the shell gate was exercised. `preflight_v2_restart.sh` is
invoked by a human before a v2 restart, and #756 has therefore still never run in anger.

⛔ #759's recovery-splitting is **UNEXERCISED in production**, not proven. It splits runs on
`[BROKER-SYNC-OK]`, which has never once been emitted: the marker fires only on a transition
(`if _runs.get(account_name):`), and there have been **0 broker-read failures since the emitter
deployed** 08-24 17:28 ET. The 242 `[BROKER-SYNC-UNREADABLE]` events all predate it — 242 of 242
carry no `consecutive=`, the field #774 added. That zero is honest, and the fix has no live
population until a read fails and then recovers.

---

# BOARD 22 — CORRECTED BOUNDARY

Three of four fan-out emit sites use `fanout_webull_claimed`; `_eh_resting_cross_check` does not.
Signal 4 is blind because **31 of 31** filled `eh_resting` legs had `cw_arm_bar_ts=0` as of 08-24.
The relevant co-occurrence population was **18 symbol-days since 08-01, 1 on 08-21**
(`eh_resting` + `reactive`), not the superseded 22 / 2 (`eh_resting` + any source).

“Zero live evidence” was false: three same-cross sequences exist. `resting_active` is the real
interlock while the resting order is live. The obvious latch write is a no-op because the next BUY
arm clears it, and no existing test distinguishes the mutation. JUNS was flat ~13m30s between
legs—a reclaim, not overlapping exposure. Harmful duplicate exposure remains **unproven**.
Observability first: durable identity before the ARM and a recorded Webull outcome.

---

# WEBULL VENUE-HISTORY SEAM

Five 2026-08-21 combo IDs are durably transcribed in `docs/deploy-2026-08-24-window.md`; the source
log rotation is expected to disappear around 08-29 under the current 7-day policy. Any probe must:

- enumerate to discover, then detail-call every listing miss before concluding absence;
- page to a proved terminal condition and print page/request counts;
- pace at two requests per two seconds and run after the trading window;
- return `found`, `confirmed-absent-via-detail`, `COULD_NOT_TELL`, or `VOID`;
- mark the assay VOID if the five known-positive controls do not reproduce.

`get_order_history` is not a reconciliation source until coverage back to 08-03, combo exit-child
visibility, partial-fill semantics, freshness versus detail, and cursor integrity are measured.

---

# OPERATIONAL RULES

1. `merged`, `deployed`, and `proven healthy` are separate claims.
2. A health endpoint that cannot answer is `COULD_NOT_TELL`, never healthy.
3. For squash merges, require current-base rehearsal and merge with
   `--repo --squash --match-head-commit <full reviewed SHA>`.
4. `MERGEABLE` + green CI is insufficient when `mergeStateStatus` is `BEHIND`/`BLOCKED`.
5. Do not edit another agent's owned range; the author fixes findings and the other agent re-reviews.
6. Production mutation requires explicit operator GO and an after-market-hours window unless the
   operator explicitly authorizes a narrower emergency action.

## Memory pointers

`[[project-mai-tai-context]]` · `[[project-mai-tai-fleet-roster]]` ·
`[[project-mai-tai-architecture]]` · `[[feedback_verify_before_concluding]]` ·
`[[feedback_an_absence_is_evidence_only_against_a_known_denominator]]`

# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.

**Refreshed by `codex-2`: 2026-08-25 10:13 ET, read-only against production and GitHub.** No
account-position or open-order claim was made in this refresh.

---

# 🚨 BEFORE 07:00 ET — SCHWAB RE-AUTH IS ON DISK, NOT IN THE TRADING PROCESSES

The token store was rewritten by re-auth at **2026-08-25 06:03:35 ET**. Its new refresh token
expires **2026-08-31 16:02:05 ET**. But every trading process predates that file:

| process | start UTC | relation to token-store write |
|---|---:|---|
| OMS | 2026-08-24 21:28:24 | older |
| strategy | 2026-08-24 21:28:24 | older |
| schwab-1m-v2 | 2026-08-25 00:28:51 | older |

`SchwabBrokerAdapter.__init__` calls `_load_token_store()` once; the running adapter does not
reload a later re-auth file. **Therefore re-auth is persisted but not active in OMS, strategy, or
v2.** A restart must happen *after* the token-store mtime. ⛔ No restart was inferred or performed;
production restart requires explicit operator GO and the narrow service choreography.

---

# PRODUCTION — THE 2026-08-24 BATCH IS MERGED AND DEPLOYED

**VPS HEAD `a4235a653aa82907e4e124f97a49fc07c374203a`, clean at 10:13 ET.** The intended batch is
`#769 → #766 → #758 → #755 → #774 → #761 → #771`; #760 and #773 were closed as superseded by
#774. Final tree `6b12b7a79…` matched the independently squash-assembled tree.

GitHub main is now `dd6c0d6cb…`, four code/ops commits ahead: **#775, #756, #759, #763**. They
were squash-merged under a direct operator instruction and are **not deployed**. The three trading
units below still have their 08-24 PIDs and zero restarts, so “merged” has not been mistaken for
“running.”

| unit | PID | NRestarts | active since UTC | current read |
|---|---:|---:|---:|---|
| OMS | 1290662 | 0 | 08-24 21:28:24 | active/running 10:13 ET; last `/health` healthy 06:25 ET |
| strategy | 1290668 | 0 | 08-24 21:28:24 | active/running 10:13 ET; last `/health` healthy 06:25 ET |
| schwab-1m-v2 | 1307928 | 0 | 08-25 00:28:51 | active/running 10:13 ET; last `/health` **degraded** 06:25 ET |
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

## ⛔ Next deploy: first production activation of #756

The next `Deploy Service` run will pull #756 for the first time and execute its new
`link_preflight_fences` step from `08_install_runtime.sh` after `pip install -e` and before the
restart. Treat `[PREFLIGHT-LINK-FAILED]` and the linked fence target as named first-run candidates,
not retrospective guesses. A link failure is intentionally loud but does **not** abort the deploy.

⚠ Precise boundary: `Deploy Service` does **not** invoke `preflight_v2_restart.sh`; it only links the
repo copy into `/home/trader/ops_preflight`. A separate/manual use of that fence will be its first
execution. During an explicitly allowed live-hours deploy, `deploy_service.sh` instead invokes the
Python `deploy_preflight.py` gate. Do not report the shell restart fence as exercised merely because
the link step ran.

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

## #770 — this handoff PR

Claude corrected the superseded board-22 population at head `07bfe170b…`. Ownership is now with
`codex-2` for this current-state rewrite, exact deploy evidence, and the machine-generated
`docs/handoff-manifest/2026-08-24.md`. The post-deploy commit already exists at `3ba2d7a9…`; the
manifest and update onto current main are the remaining branch work. After the final manifest
commit, Claude reviews the complete Codex range with `--since 07bfe170b…`; only independent range
coverage can authorize merging.

## #772 — closed; the production probe remains **BLOCKED**

The fourth static rule still allowed real SDK-object laundering. #772 was closed without merge:
an AST check is not a runtime capability boundary, and a fifth syntax rule would repeat the same
design error. Official Webull material documents no retail Trading-API key restricted to reads.
Sandbox credentials are isolated from production, so they cannot enumerate the production account.
The probe has **no enforced read-only guard and must not run against production credentials** unless
Webull confirms or provisions a credential-level read-only scope. “Do not run it” is the current
safe answer; the orphan-order question is already settled and only the capability question remains.

## #775, #756, #759, #763 — merged, not deployed

Main now carries 30-day log retention/freshness (#775), repo-owned restart fences (#756), the
broker-blind-while-holding pager (#759), and feature-acceptance markers (#763). The box remains at
`a4235a653…`, so none is runtime evidence yet. #775's first scheduled retention reconcile is the
first execution of its scp-to-`/tmp` production path; watch the run rather than assuming activation.

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

Credential finding, searched 2026-08-25: Webull's retail Trading API documents one App Key/App
Secret with no read-only selector; the official Python SDK uses that same client for query and
write operations. Connect/OAuth is a separately registered partner product and its documented
scope is `user:trade:wr`, not read-only. The official MCP's `account,market-data` tool filtering is
process-level filtering over the same key, not credential enforcement. Institutional documentation
even says alignment of key permissions with user permissions is “coming soon.” Therefore the
production inventory sweep is **blocked**, not merely awaiting a better script.

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

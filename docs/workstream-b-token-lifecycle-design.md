# Design: Schwab token lifecycle — dedicated refresher (#1) + auto-reload (#2) + atomic writes (#3)

**Status: DESIGN — decisions RESOLVED, code-confirms DONE. Final review → implement → additive
regression → PR → attended deploy → survival test.** Design-first, same discipline as Workstream A
(PR #249/#252). This is the **resilience half of Workstream B** and it touches the **token lifecycle
on the sole live Schwab bot (v2)** — right over fast; #1 is a new component, not an evening patch.

Supersedes the narrower `workstream-b-auto-reload-token-design.md` (which assumed a shared token
cache that STEP 0 disproved). The visibility half (dashboard/`control-plane-redesign.html`) is a
separate doc; they compose (visibility surfaces `token: dead`, this fix auto-clears it on re-auth).

---

## Why this exists (STEP 0 findings, confirmed from code 2026-06-09)

Two distinct problems on the shared Schwab token (`/var/lib/macd-webhook-server/data/schwab_tokens.json`):

1. **The SPOF (priority).** v2 reads the `access_token` from disk fresh every poll/connect
   (`schwab_v2_rest_client` / `schwab_v2_streamer` — confirmed disk-fresh + torn-read-safe) but
   **never refreshes** it. The only thing keeping the on-disk token fresh is the **OMS
   `SchwabBrokerAdapter`'s incidental refresh** (its 15s broker-sync calls `_get_access_token` →
   refresh-on-expiry → `_save_token_store`). That adapter is alive **only because the retired
   `macd_30s`/`schwab_1m` are still `provider=schwab`**. So our sole live Schwab bot's token
   freshness depends on retired-dormant-bot plumbing. Remove those dormant accounts (a plausible
   future tidy-up — the dormancy markers may even invite it) → nothing refreshes → **v2 silently
   dies at the next access-token expiry (~30 min)**. A trap.

2. **The __init__-cache bug (06-03 root cause, still present).** `SchwabBrokerAdapter._get_access_token`
   (schwab.py ~591) on the dead-token signature (`invalid_grant` / `unsupported_token_type`)
   **raises and stays pinned to the cached dead `_refresh_token`** (loaded once in `__init__` via
   `_load_token_store()`). A re-auth writes a fresh token to disk, but the running adapter keeps
   using the cached dead one until a process restart. This is exactly the 2026-06-03→05 2.6-day
   outage choreography (re-auth without restart = no-op).

**#1 is more fundamental than #2:** a perfect reload doesn't help if nothing writes fresh tokens
in the first place. Settle #1 first.

### Confirmed architecture (read-only, 2026-06-09)
- **v2 read path needs nothing** — disk-fresh on every poll/connect; `_read_access_token` catches
  `(OSError, ValueError)` → returns None → graceful retry (torn-read-safe). A re-authed token is
  picked up on the next read **without a restart**, *provided the on-disk access_token stays fresh*.
- **macd-webhook-server is fully retired** — service `disabled`+`inactive`, 0 timers, 0 cron refs,
  0 live processes. It writes nothing to the token store. (Last write was a project_mai_tai service.)
- **Token-store writers (the only ones):** `SchwabBrokerAdapter._save_token_store` (access-token
  refresh) and `control_plane._persist_schwab_token_store` (the OAuth re-auth callback,
  `schwab_auth_callback` → `_persist…`). The `authorization_code` exchange that mints a new
  refresh_token exists **only in control_plane**. No hidden second writer.
- **Control service already owns the WRITE side of the lifecycle** (authorize → exchange → persist),
  is always-on, and is the natural home for a refresh loop → full lifecycle in one owner.
- **Refresh mechanics:** Basic-auth (`client_id:client_secret`) refresh-grant POST to
  `schwab_token_url`; `refresh_margin_seconds=60` before a ~30-min access-token expiry.
- **OMS reload viability:** `_load_token_store()` (schwab.py:860) is callable to reload from disk,
  so the OMS adapter can pick up the refresher's fresh token on expiry after its own refresh is
  disabled (see #2 / single-writer below). Mechanism confirmed.

---

## Resolved decisions (operator, 2026-06-09)

1. **Refresher home → the control service.** It already owns the OAuth write side, is always-on,
   and this puts the full lifecycle (authorize → persist → refresh → persist) in one owner. NOT a
   standalone service (needless ops surface); NOT in the v2 bot (don't re-couple the token to one
   bot — v2's read-only/no-refresh separation is a feature to preserve).
2. **Writer model → single-writer + atomic writes everywhere.** Disable the OMS adapter's
   *incidental refresh* once the dedicated refresher owns freshness (single-writer is the cleaner
   invariant; multi-writer adds race surface on the token v2 depends on). Keep atomic writes (#3)
   on ALL write paths regardless (cheap insurance). **Confirmed:** after disabling adapter refresh,
   the OMS adapter still gets a fresh token via `_load_token_store()` reload-on-expiry from the
   refresher's on-disk token — so its 15s broker-sync is not starved.
3. **invalid_grant-reload → refresher + adapter (both).** Refresher is the owner (active writer,
   where recovery lives); adapter-level reload is a cheap defensive secondary for any instance
   still refreshing on its own 401s. They cache independently → additive.
4. **control_plane is the canonical re-auth path — CONFIRMED** (not assumed): macd-webhook-server
   writes nothing (retired); the control OAuth callback is the only re-auth/write path going forward.

---

## #1 — Dedicated token refresher (in the control service)

A background asyncio task in the control service that **owns keeping the on-disk access_token fresh**,
independent of any schwab bot (retired or live).

- **Loop:** every ~`check_interval_secs` (default 60s), read the store; if `access_token` is within
  `refresh_margin_seconds` of `expires_at`, run the refresh-grant and **atomically** write
  `access_token`+`refresh_token`+`expires_at` (#3).
- **Shared grant helper:** refactor the refresh-grant + token-store load/save out of
  `SchwabBrokerAdapter` into a shared token-manager helper (no logic change to the grant itself),
  so the refresher and the adapter use one implementation. This is the main refactor.
- **On `invalid_grant`/dead-token signature:** reload the store from disk (a control-service re-auth
  may have just written a fresh refresh_token); retry once. If still dead → surface dead-token state
  (ties to the visibility half) + **bounded backoff, no hot-spin**, loop stays alive. Escalate to
  `degraded-persistent` after `max_dead_token_retries`.
- **Idle-gracefully** if `client_id`/`client_secret`/`refresh_token` absent (log + wait, don't crash).
- **`CancelledError` propagates** (Workstream A rule) so shutdown works.
- **Env knobs:** refresher-enabled flag (default on), `check_interval_secs`, reuse
  `refresh_margin_seconds`, dead-token backoff, `max_dead_token_retries` → escalation. Conservative defaults.

Once this lands, the OMS adapter's incidental refresh is disabled (decision #2) and the retired
schwab accounts can be removed without darkening v2 — **the point of #1**.

## #2 — Narrowed auto-reload (defensive, in SchwabBrokerAdapter)

`SchwabBrokerAdapter._get_access_token` invalid_grant path: guarded `_load_token_store()` reload +
retry the refresh once from the disk refresh_token; if still dead → raise/surface (no hot-spin).
Per-instance (OMS + strategy-engine adapters each cache independently → each needs it). With #1 the
refresher is the **primary** recovery owner; this adapter reload is the **defensive secondary** for
any instance still refreshing on its own 401s. **Also:** with the adapter's primary refresh disabled
(decision #2), its on-expiry path becomes `_load_token_store()` reload-from-disk (use the refresher's
token) rather than a refresh-grant — so the OMS broker-sync stays fed.

## #3 — Atomic writes (fold into all writers)

Shared atomic-write helper: write to a temp file on the same filesystem + `os.replace`. Apply to ALL
token-store writers: the new refresher, `SchwabBrokerAdapter._save_token_store` (currently bare
`write_text`), and `control_plane._persist_schwab_token_store`. Cheap; matters more once reloads read
more often. (Readers are already torn-safe, so not urgent on its own — but do it while there.)

---

## Component interaction (post-change)

```
control service:  OAuth callback ──(new refresh_token)──┐
                  dedicated refresher ──(access_token)──┤── atomic write ──► token store (disk)
                                                        │                         │
SchwabBrokerAdapter (OMS): refresh DISABLED;            │      reads (disk-fresh, │ torn-safe)
   on-expiry → _load_token_store() reload ◄─────────────┘                         ▼
                                                              v2 streamer + v2 REST (read-only)
```
Single refresh-writer (refresher) + the re-auth writer (control callback). v2 + OMS adapter are
disk readers. invalid_grant-reload in refresher (primary) + adapter (defensive).

---

## Survival test (the verdict — prove on demand, like Workstream A)

In a safe window (v2 PAPER), via env-gated fault-injection (default off):
1. **Resilience proof:** inject `invalid_grant` on the refresh path → confirm **no recovery while
   disk still holds the dead token** (surfaces dead-token + backs off, no hot-spin, no crash, loop
   alive) → write a fresh token to the store (simulate a control re-auth) → confirm the refresher
   **picks it up WITHOUT a restart** (`[SCHWAB-TOKEN-RELOADED]`, NRestarts unchanged) → refresh
   resumes → v2 (disk reader) + OMS adapter both recover.
2. **SPOF proof (the #1 verdict):** **disable the retired-bot schwab sync** (simulate the dormant-
   account cleanup) → confirm v2's on-disk token **STILL stays fresh** because the dedicated
   refresher keeps writing → v2 does not depend on dormant plumbing → **trap closed.**

---

## Build sequence

1. **This doc → final review** (decisions resolved, confirms done).
2. On approval: implement — refresher loop in control service + shared grant helper refactored out
   of `SchwabBrokerAdapter` + atomic-write helper (all writers) + adapter defensive reload + OMS
   on-expiry disk-reload (refresh disabled) + `CancelledError` + idle-if-creds-absent + fault-injection hook.
3. **Additive regression** (by-name baseline, no test substitution) + unit tests: dead-token→reload→
   recover, torn/partial-file read, refresher-keeps-token-fresh-with-OMS-refresh-disabled, CancelledError.
4. **PR → stop for review.**
5. **Attended deploy** (live token path — account-flat at restart, CYN protected, stage-one verify)
   → **survival test** (both proofs above) as the verdict.
6. Update handoff with deploy + survival-test result.

**No rush to ship same-evening — #1 is a new component; settle it right. If anything surfaces a
safety blocker, stop and reassess rather than ship an unsafe change on the live token path.**

---

## Constraints (unchanged)

PR #227 stays · PR #238 untouched · v2 streamer flag stays ON (live sole Schwab bot) · retired
`schwab_1m`/`macd_30s` dormant **AND not removed until #1 lands — they are load-bearing for token
refresh** (recorded in the handoff PENDING LIST) · CYN untouched · polygon parked · v2 PAPER
(real-money is a separate later step).

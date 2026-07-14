# Schwab refresh-token expiry warning — design (2026-07-14)

**Status:** design-first. Additive; off the trading hot path.

## Problem
The Schwab **refresh_token** expires on a fixed clock (~7 days, non-rotating — routine
access-token refreshes do NOT reset it). When it dies, every Schwab call fails
`invalid_grant` and a human must re-auth at `/auth/schwab/start`. Today there is **no
proactive warning**: the token store persists only the access-token `expires_at`, never a
refresh-token expiry, so nothing can alert before it dies. The failure is surfaced only
*after* death (`[SCHWAB-TOKEN-REFRESHER-DEGRADED-PERSISTENT]`), often mid-session.

## Fix — three additive pieces

### 1. Capture the expiry at re-auth (the source)
Schwab's `authorization_code` token response includes **`refresh_token_expires_in`** (seconds,
~604800 = 7d). We currently discard it. Persist two new fields to the token store:
- `refresh_token_expires_at` = `now + refresh_token_expires_in` (fallback: `now + 7d` if absent)
- `refresh_token_obtained_at` = `now`

Written by the control-plane callback (`_persist_schwab_token_store`) on every re-auth.

### 2. Preserve it across routine refreshes (don't wipe)
`build_token_store_document` rewrites the WHOLE store every ~30 min on the refresh grant, which
would drop unknown fields. The refresh grant does not return `refresh_token_expires_in` (and the
refresh_token is non-rotating), so the refresher must **carry the existing value forward**:
- `TokenGrantResult` gains `refresh_token_expires_at`, `refresh_token_obtained_at`.
- `parse_token_grant_response(..., previous_refresh_token_expires_at, previous_refresh_token_obtained_at)`:
  if the payload carries `refresh_token_expires_in` (>0) → set fresh (`now + it`); else carry the
  previous values forward. (Mirrors how `refresh_token` itself is carried forward.)
- `build_token_store_document` includes the two fields.
- The refresher reads the previous values from the store (it already reads the store) and passes them in.

Net: a re-auth sets a fresh 7-day expiry; the ~30-min refreshes preserve it unchanged.

### 3. Proactive ntfy warning (independent cron)
New `ops/health/schwab_token_expiry_check.py` + `schwab_token_expiry_cron.sh`, mirroring the
readiness/oms-liveness health scripts (reuse `preopen_alert.sh` → topic `mai-tai-preopen-…`):
- Reads `refresh_token_expires_at` from the token store, computes hours-left.
- **≤ 48h →** AMBER "Schwab refresh_token expires in ~Xh — re-auth at /auth/schwab/start".
- **≤ 12h or already expired →** RED urgent.
- **> 48h →** GREEN/no-op (silent; optional daily liveness ping).
- If `refresh_token_expires_at` is missing (pre-capture token) → AMBER "expiry unknown — re-auth to start tracking".
- Runs ~twice daily (piggyback the readiness slot + an evening slot). Dual-UTC + ET-guard
  (CRON_TZ is ignored on the box — mirror `preopen_readiness_cron.sh`). Idempotent; only pushes
  inside the warning window.

### (Optional) Dashboard surface
Add `refresh_token_expires_at` + `days_left` to the overview `schwab_token_refresher` section.
Nice-to-have; not required for the warning. Deferred unless trivial.

## Bootstrap (current token)
The operator re-authed 2026-07-14 ~07:43 ET **before** this capture code — so the current store
has no `refresh_token_expires_at`. One-time deploy step: seed
`refresh_token_expires_at = 2026-07-21T11:43Z` (obtained_at + 7d) + `refresh_token_obtained_at =
2026-07-14T11:43Z`. Because piece #2 now preserves the field, the seed survives refreshes. Future
re-auths self-populate.

## Verification / uncertainty
`refresh_token_expires_in` field name + the 7-day/non-rotating behavior are Schwab-documented but
we log the captured value once (`[SCHWAB-REFRESH-EXPIRY-CAPTURED] in=… at=…`) so the **next real
re-auth confirms** the true lifetime. Fallback `now + 7d` covers absence.

## Tests
- `parse_token_grant_response`: captures `refresh_token_expires_in` when present; carries previous
  forward when absent; `build_token_store_document` includes the fields.
- Callback `_persist_schwab_token_store`: captures + 7d fallback.
- Warning check: ≤12h→RED, ≤48h→AMBER, >48h→GREEN, missing→AMBER.
- `test_schwab_token_grant_characterization.py` stays green (access-token path byte-identical).

## Rollback
Additive fields + a new cron. Rollback = remove the cron; the extra token-store fields are inert.
No trading-path change; no env change.

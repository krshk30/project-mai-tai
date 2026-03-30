# Schwab Onboarding

This repo now supports `MAI_TAI_OMS_ADAPTER=schwab` for live execution.

The intended live shape is:
- one shared Schwab brokerage account
- one broker account record in Postgres
- four strategies attributed through `virtual_positions`
- broker truth reconciled through `account_positions`

## Required Settings

Minimum env fields:
- `MAI_TAI_OMS_ADAPTER=schwab`
- `MAI_TAI_STRATEGY_MACD_30S_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_MACD_1M_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_TOS_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_STRATEGY_RUNNER_ACCOUNT_NAME=live:schwab_shared`
- `MAI_TAI_SCHWAB_ACCOUNT_HASH=<encrypted account hash>`
- `MAI_TAI_SCHWAB_CLIENT_ID=<app client id>`
- `MAI_TAI_SCHWAB_CLIENT_SECRET=<app client secret>`
- `MAI_TAI_SCHWAB_TOKEN_STORE_PATH=/var/lib/project-mai-tai/schwab_token.json`

Optional per-bot account-hash overrides:
- `MAI_TAI_SCHWAB_MACD_30S_ACCOUNT_HASH`
- `MAI_TAI_SCHWAB_MACD_1M_ACCOUNT_HASH`
- `MAI_TAI_SCHWAB_TOS_RUNNER_ACCOUNT_HASH`

If those overrides are not set, the adapter falls back to `MAI_TAI_SCHWAB_ACCOUNT_HASH`.

## Token Flow

Mai Tai does not run the initial browser authorization flow for Schwab.
Instead, the expected production flow is:

1. Complete the one-time Schwab OAuth consent flow outside the service.
2. Save the resulting token material into `MAI_TAI_SCHWAB_TOKEN_STORE_PATH`.
3. Start `project-mai-tai-oms.service`.
4. Let OMS refresh and rotate tokens in that file during runtime.

Expected token-store JSON shape:

```json
{
  "access_token": "replace-me",
  "refresh_token": "replace-me",
  "expires_at": "2026-03-30T13:15:00+00:00"
}
```

Fallback env-only fields are also supported:
- `MAI_TAI_SCHWAB_ACCESS_TOKEN`
- `MAI_TAI_SCHWAB_ACCESS_TOKEN_EXPIRES_AT`
- `MAI_TAI_SCHWAB_REFRESH_TOKEN`

The token store is preferred for live use because refreshed tokens can rotate and
need to survive service restarts.

## Account Hash

Schwab trading requests use the account hash, not the human-readable account number.

Before go-live:
- fetch the encrypted account hash from the Schwab Trader API account-number lookup
- copy that hash into `MAI_TAI_SCHWAB_ACCOUNT_HASH`
- keep the plain account number out of normal runtime config unless you have a separate secure lookup step

## Operational Notes

- `control-plane` will report the live wiring as `live/schwab`.
- `seed_runtime_metadata` collapses the four strategies into one broker account if all four account-name env vars match.
- OMS uses Schwab as broker truth, while strategy attribution still lives in `virtual_positions`.
- News enrichment is still separate from broker execution. If you rely on the existing Alpaca-backed news path, keep those news credentials configured independently.

## Go-Live Checklist

- confirm `mai-tai-oms` starts cleanly with `MAI_TAI_OMS_ADAPTER=schwab`
- confirm `/api/overview` shows `provider=schwab`
- confirm `/api/bots` shows `live/schwab` for each bot
- confirm only one broker account is seeded when all four strategy account names are shared
- confirm the token store refreshes after the first authenticated request
- confirm `account_positions` reconcile to the shared Schwab account
- confirm `virtual_positions` still attribute fills to the correct strategy

# TradingView Alert Automation

This sidecar service is the bridge between the strategy-engine watchlist and TradingView alert management.

Current first-pass behavior:

- consumes the `strategy-state` Redis stream
- bootstraps from the latest `strategy-state` snapshot on startup so a restart does not wait for the next watchlist change
- reads the published `watchlist` from the strategy engine
- computes add/remove diffs against its persisted alert state
- protects symbols from deletion while strategy-state still shows an open or pending bot state for that ticker
- exposes a local API for manual sync, add, remove, and status checks
- persists the latest desired/managed symbol set to disk so restarts do not lose intent

Current service endpoints:

- `GET /health`
- `GET /alerts/status`
- `POST /alerts/sync`
- `POST /alerts/add`
- `POST /alerts/remove`

Current configuration:

- `MAI_TAI_TRADINGVIEW_ALERTS_ENABLED`
- `MAI_TAI_TRADINGVIEW_ALERTS_AUTO_SYNC_ENABLED`
- `MAI_TAI_TRADINGVIEW_ALERTS_STATE_PATH`
- `MAI_TAI_TRADINGVIEW_ALERTS_OPERATOR`
- `MAI_TAI_TRADINGVIEW_ALERTS_CHART_URL`
- `MAI_TAI_TRADINGVIEW_ALERTS_USER_DATA_DIR`
- `MAI_TAI_TRADINGVIEW_ALERTS_HEADLESS`
- `MAI_TAI_TRADINGVIEW_ALERTS_TIMEOUT_MS`
- `MAI_TAI_TRADINGVIEW_ALERTS_BROWSER_CHANNEL`
- `MAI_TAI_TRADINGVIEW_ALERTS_ALERT_NAME_PREFIX`
- `MAI_TAI_TRADINGVIEW_ALERTS_CONDITION_TEXT`
- `MAI_TAI_TRADINGVIEW_ALERTS_WEBHOOK_URL`
- `MAI_TAI_TRADINGVIEW_ALERTS_WEBHOOK_TOKEN`
- `MAI_TAI_TRADINGVIEW_ALERTS_MESSAGE_TEMPLATE_JSON`
- `MAI_TAI_TRADINGVIEW_ALERTS_NOTIFICATION_PROVIDER`
- `MAI_TAI_TRADINGVIEW_ALERTS_NOTIFICATION_COOLDOWN_MINUTES`
- SMTP notification settings for email delivery
- Twilio notification settings for SMS delivery

Operator modes:

- `log_only`
  - safe default for watchlist-diff validation
  - logs add/remove requests and persists desired state
- `playwright`
  - launches a persistent Chromium profile using the configured `user_data_dir`
  - navigates the TradingView chart page, creates alerts, and removes named alerts
  - depends on the repo environment having the Python `playwright` package plus installed browser binaries

Browser setup reminder:

- package install: `python -m pip install playwright`
- browser install: `python -m playwright install chromium`
- or use a system Chrome/Chromium build with `MAI_TAI_TRADINGVIEW_ALERTS_BROWSER_CHANNEL`

Current limitation:

- the Playwright operator is implemented conservatively with selector fallbacks, but it still needs a live TradingView session to validate and tune the exact UI selectors on the VPS/browser profile you will use
- if the profile is not signed in, TradingView shows a `Join for free` prompt instead of the alert dialog and the operator will now fail fast with a sign-in-required error

Session behavior:

- TradingView sign-in is intended to be a one-time setup per persistent browser profile, not a daily ritual
- in practice, TradingView can still expire or invalidate that session later
- when that happens, the service now marks the operator as `auth_required` and can send a one-shot relogin notification
- notifications are rate-limited by `MAI_TAI_TRADINGVIEW_ALERTS_NOTIFICATION_COOLDOWN_MINUTES` so repeated sync failures do not spam you

Recommended rollout:

1. Start with `MAI_TAI_TRADINGVIEW_ALERTS_OPERATOR=log_only`
2. Verify the service is receiving the strategy watchlist and computing correct add/remove diffs
3. Install Playwright browsers in the target environment
4. Switch to `MAI_TAI_TRADINGVIEW_ALERTS_OPERATOR=playwright`
5. Use a persistent browser channel that matches the local install, for example `MAI_TAI_TRADINGVIEW_ALERTS_BROWSER_CHANNEL=chrome`
6. Sign into TradingView in the persistent browser profile once
7. If the operator reports a sign-in-required error, open that same profile and finish TradingView login before retrying
8. Validate one manual `POST /alerts/add` and `POST /alerts/remove` cycle before enabling auto-sync

Local-machine recommendation:

- if TradingView login is blocked from a datacenter VPS IP, run the `tradingview-alerts` service on a local machine with a persistent signed-in Chrome profile
- keep the webhook server on the VPS
- keep the webhook URL pointed at `https://hook.project-mai-tai.live/webhook`

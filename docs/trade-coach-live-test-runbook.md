# Trade Coach Live Test Runbook

## Purpose

This runbook is for the first controlled live-market validation of the Mai Tai
trade coach on the VPS.

Current deployment state:

- trade coach code is deployed to the VPS
- trade coach database migration is deployed
- trade coach API key is configured on the VPS outside the repo
- persistent VPS config still keeps trade coach disabled
- there is still **no** dedicated `project-mai-tai-trade-coach` systemd unit
- the first live test should therefore be a **manual one-off VPS run**

Primary goal for the first live test:

- prove that real closed `macd_30s` trades generate persisted
  `ai_trade_reviews`
- verify that the control plane exposes those reviews cleanly
- confirm there is no impact on normal bot trading behavior

Secondary goal:

- confirm `webull_30s` stays logically separate if it produces any completed
  cycles

## Scope

Tomorrow's test should stay narrow:

- `Schwab 30 Sec Bot` first
- `Webull 30 Sec Bot` observed second
- post-trade coaching only
- no OMS gating
- no strategy gating
- no always-on service install yet

## Where To Look

Use these surfaces in this order.

1. Browser dashboard context

- Main control plane:
  - [https://project-mai-tai.live/](https://project-mai-tai.live/)
- Schwab 30-second bot page:
  - [https://project-mai-tai.live/bot/30s](https://project-mai-tai.live/bot/30s)
- Webull 30-second bot page:
  - [https://project-mai-tai.live/bot/30s-webull](https://project-mai-tai.live/bot/30s-webull)

These pages are useful for live bot context, fills, and whether the bots are
behaving normally.

2. Source-of-truth coach output

- Bot API:
  - [https://project-mai-tai.live/api/bots](https://project-mai-tai.live/api/bots)

This is the main place to inspect coach output tomorrow because each bot object
already includes:

- `recent_trade_coach_reviews`
- `recent_fills`
- `recent_orders`
- `recent_intents`

Recommended quick inspection:

```powershell
curl https://project-mai-tai.live/api/bots
```

What to look for inside each bot object:

- `strategy_code`
- `account_name`
- `recent_trade_coach_reviews`

Expected review row fields:

- `strategy_code`
- `broker_account_name`
- `symbol`
- `review_type`
- `cycle_key`
- `verdict`
- `action`
- `confidence`
- `summary`
- `created_at`

3. VPS database truth

If the API shows something unexpected, verify the rows directly:

```powershell
ssh mai-tai-vps "sudo bash -lc 'source /etc/project-mai-tai/project-mai-tai.env && psql \"$MAI_TAI_DATABASE_URL\" -c \"select strategy_code, broker_account_name, symbol, review_type, verdict, action, confidence, created_at from ai_trade_reviews order by created_at desc limit 20;\"'"
```

4. Manual coach process logs

For the first test we should use a temporary systemd-run unit so logs are easy
to follow:

```powershell
ssh mai-tai-vps "sudo journalctl -u project-mai-tai-trade-coach-smoke -f"
```

## Pre-Market Checklist

- [ ] Open [https://project-mai-tai.live/health](https://project-mai-tai.live/health) or verify
      `https://project-mai-tai.live/` is healthy
- [ ] Confirm `Schwab 30 Sec Bot` is listening normally on
      [https://project-mai-tai.live/bot/30s](https://project-mai-tai.live/bot/30s)
- [ ] Confirm `Webull 30 Sec Bot` is listening normally on
      [https://project-mai-tai.live/bot/30s-webull](https://project-mai-tai.live/bot/30s-webull)
- [ ] Confirm persistent VPS config is still disabled
- [ ] Confirm no stale temporary coach unit is already running

Suggested checks:

```powershell
ssh mai-tai-vps "sudo bash -lc 'source /etc/project-mai-tai/project-mai-tai.env && printf \"enabled=%s shadow=%s promote=%s\n\" \"$MAI_TAI_TRADE_COACH_ENABLED\" \"$MAI_TAI_TRADE_COACH_SHADOW_ENABLED\" \"$MAI_TAI_TRADE_COACH_PROMOTE_ENABLED\"'"
ssh mai-tai-vps "systemctl status project-mai-tai-trade-coach-smoke --no-pager || true"
```

Expected values:

- `enabled=false`
- `shadow=false`
- `promote=false`

## Start Procedure

Do **not** edit the VPS env file for the first test.

Instead, start the coach with a temporary runtime override so the persistent VPS
state remains disabled by default.

Start command:

```powershell
ssh mai-tai-vps "sudo systemd-run \
  --unit=project-mai-tai-trade-coach-smoke \
  --uid=trader \
  --gid=trader \
  --property=WorkingDirectory=/home/trader/project-mai-tai \
  /bin/bash -lc 'set -a; source /etc/project-mai-tai/project-mai-tai.env; export MAI_TAI_TRADE_COACH_ENABLED=true; exec /home/trader/project-mai-tai/.venv/bin/mai-tai-trade-coach'"
```

Immediate verification:

```powershell
ssh mai-tai-vps "systemctl status project-mai-tai-trade-coach-smoke --no-pager"
ssh mai-tai-vps "sudo journalctl -u project-mai-tai-trade-coach-smoke -n 50 --no-pager"
```

Healthy early log signs:

- trade coach starts without config errors
- no API key warning
- no database connection failure
- no structured-output parsing failure

## Live Monitoring During Market Hours

Keep these open:

1. Bot behavior

- [https://project-mai-tai.live/bot/30s](https://project-mai-tai.live/bot/30s)
- [https://project-mai-tai.live/bot/30s-webull](https://project-mai-tai.live/bot/30s-webull)

2. Coach API output

- [https://project-mai-tai.live/api/bots](https://project-mai-tai.live/api/bots)

3. Coach logs

```powershell
ssh mai-tai-vps "sudo journalctl -u project-mai-tai-trade-coach-smoke -f"
```

What should happen:

- normal bot trading continues unchanged
- once a real flat-to-flat cycle closes, the coach picks it up on its poll loop
- a new row lands in `ai_trade_reviews`
- the bot's `recent_trade_coach_reviews` list becomes non-empty in `/api/bots`

## Validation Checklist After First Closed Trade

- [ ] Closed trade came from a real `macd_30s` flat-to-flat cycle
- [ ] Manual smoke unit is still running
- [ ] `ai_trade_reviews` has a new row
- [ ] `/api/bots` shows the new review under the correct bot
- [ ] `broker_account_name` matches the real bot account
- [ ] `strategy_code` matches the correct bot
- [ ] `summary` is readable and useful
- [ ] `verdict`, `action`, and `confidence` look plausible
- [ ] no bot behavior regression is visible on control plane pages

Recommended checks:

```powershell
ssh mai-tai-vps "sudo bash -lc 'source /etc/project-mai-tai/project-mai-tai.env && psql \"$MAI_TAI_DATABASE_URL\" -c \"select strategy_code, broker_account_name, symbol, verdict, action, confidence, created_at from ai_trade_reviews order by created_at desc limit 10;\"'"
curl https://project-mai-tai.live/api/bots
```

## Success Criteria

The first live test is a success if all of this is true:

- at least one real closed `macd_30s` trade is reviewed
- review row persists to `ai_trade_reviews`
- `/api/bots` exposes the review under the correct bot
- there is no evidence of impact on live trading services
- no urgent parsing, API, or database errors appear in logs

## Stop And Rollback

If anything looks wrong, stop only the temporary smoke unit.

Stop command:

```powershell
ssh mai-tai-vps "sudo systemctl stop project-mai-tai-trade-coach-smoke"
```

Confirm it stopped:

```powershell
ssh mai-tai-vps "systemctl status project-mai-tai-trade-coach-smoke --no-pager || true"
```

Why this is safe:

- persistent env file still says `MAI_TAI_TRADE_COACH_ENABLED=false`
- there is no permanent trade coach systemd unit yet
- stopping the temporary smoke unit fully disables the coach again

## Do We Need New UI Tomorrow?

No.

For the first live validation, the existing control-plane surfaces are enough:

- browser bot pages for context
- `/api/bots` for coach review visibility
- direct database query for source-of-truth persistence
- smoke-unit logs for runtime debugging

A dedicated coach UI becomes worth building only after tomorrow proves:

- the reviews are being created consistently
- the content is actually useful
- we know what fields matter most to operators

## Recommended Next Step After A Successful Test

If tomorrow goes well, the next implementation step should be:

1. add a dedicated permanent `project-mai-tai-trade-coach.service`
2. keep it advisory-only
3. optionally add a first small HTML/API review panel on the control plane

Do **not** jump to OMS gating or strategy gating yet.

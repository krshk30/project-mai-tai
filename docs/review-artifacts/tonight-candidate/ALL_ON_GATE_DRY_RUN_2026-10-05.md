# ALL-ON standing-gate dry run, October 5

**HISTORICAL helper dcd35a35 only. Independent review subsequently found four
parser/evidence gaps; these results do not certify the corrected helper.
Final exact-byte verification is in FINAL_ALLOWANCE_DRY_RUN_2026-10-05.md.**

Own read-only box runs15:12-15:19 ET. No remote file, service, env, token,
ledger, lock or journal write. The policy source is a reviewed release artifact,
not a new app env switch; no install/GO or future flatness is inferred.

## Result

Final hash-bound helper: OMS0 / strategy0 / strict-flat0, printed only exact
MI180/NXL2 finding allowances and MI current-session180. Both fresh direct
brokers flat, managed/virtual/account/working-order/inflight-intent rows empty,
source timestamps fresh. Schwab body2151B, Webull32B; overview<2MB.
Original general evaluator still reported the three real ledger/degraded
failures; narrowly admitted copy had no remaining failure. Sources unchanged.

Initial strategy run returned2 on WebullHTTP429, printed NO allowance; retained
below. Reads are now serialized/spaced. Spaced retry15:16:54-15:17:07 returned0,
not a substitution of old broker data. V2 fence returned1 ONLY its daytime
clock; no override used. OMS strict fence0. These are NOT all-gates-green or
permission for a daytime restart. Every gate must be repeated at execution.

Two initial development probes refused2 rather than passing: unused SQL
service_heartbeats table (reconciler publishes Redis events), then cached
30s overview run vs newer SQL run. Source correction uses the fresh published
raw heartbeat, binds it to its fresh overview run/details, independently
validates latest SQL findings, and requires all non-time summary fields and
full finding identities equal. Both run times are printed. No stale/changed
source is excused; no database/Redis/source health changed.

## Safety Checks

34 policy tests PASS (0.30s). Seven independent in-memory mutations give
assertion-RED, never edit a live/local gate file: fingerprint1 failure;
balance3; account-quantity1; current-session3; degraded-error1; deepcopy1;
broker-freshness2. Command:

```bash
/Users/velkris/Projects/project-mai-tai/.venv/bin/python -m pytest -q docs/review-artifacts/tonight-candidate/test_standing_allowance.py
/Users/velkris/Projects/project-mai-tai/.venv/bin/python docs/review-artifacts/tonight-candidate/mutate_standing_allowance.py
```

Original DB/Redis sources remain read-only. SQL READ ONLY repeatable read,
5s statements/64-row sentinel; overview2MB capped; broker body1MB post-decode
rejection (not a wire allocation cap), Webull50/page<=20pages. Helper no Redis.
Context only INFO sections/TYPE/HLEN/HSTRLEN; HGETALL pre-length<=100KB, no
snapshot-batches payload, XINFO/EVAL or multi-entry snapshot read.

## Redis Before/After

| Own context UTC | evicted_keys before / after | used_memory before / after (bytes) | streams / owners |
|---|---|---|---|
| 2026-10-05T19:12:24.596448+00:00 | 0 / 0 | 806525952 / 806525952 | nine stream types; five owners + marker1 |
| 2026-10-05T19:18:48.548092+00:00 | 0 / 0 | 806694176 / 806694176 | nine stream types; five owners + marker1 |

Each capture rc0. Clean box SHAe1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89; import/home/trader/project-mai-tai/src/project_mai_tai/__init__.py.
No checkout advance. Original source/script/preopen hashes are below. Context
is diagnostic, not a gate or an atomic fleet snapshot.

```json
{
  "/home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py": "6cd2b73b81c5a6bca5a5e0a1633008b7243875be4ee813d138ad32d4173552f8",
  "/home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh": "9631da8c40c7bb64879873c32509c5fb21d2a276be019398770f14bc966eb6c8",
  "/home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh": "3a0e05ee40ae2e5431b3418765189fe4ca1470d9742d60de24117c2fc741f225",
  "/home/trader/preopen.sh": "2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3"
}
```

## Exact Commands And Unedited Outputs

### oms_final

Actual rc=0. Raw local path /tmp/oct5-allon-oms-final.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - --service oms' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:33.906611+00:00 overview_checked_at=2026-10-05T19:17:03.727303+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:33.906611+00:00 overview_checked_at=2026-10-05T19:17:03.727303+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:17:30.713955-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T19:17:29.397223+00:00",
    "live:orb": "2026-10-05T19:17:29.701550+00:00"
  },
  "overview_bytes": 1143707,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:17:29.733331+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:17:29.791418+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T19:17:36.638809+00:00",
  "original_general_failures": [
    "reconciliation reports 2 total findings in the latest run.",
    "reconciliation reports 2 critical findings in the latest run.",
    "service reconciler is not healthy before deploy (status=degraded)."
  ],
  "allowance_findings": [
    {
      "symbol": "MI",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for MI (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:MI",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "180.00000000",
        "fill_delta": "180.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    },
    {
      "symbol": "NXL",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for NXL (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:NXL",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "2.00000000",
        "fill_delta": "2.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    }
  ],
  "allowance_run": {
    "id": "bc9fb022-ad42-4921-8022-3062ad2f534d",
    "status": "completed",
    "completed_at": "2026-10-05 19:17:33.906761+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:17:33.906611+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "allowance_heartbeat": {
    "status": "degraded",
    "observed_at": "2026-10-05T19:17:33.918537Z",
    "payload": {
      "details": {
        "cutover_confidence": "30",
        "total_findings": "2",
        "critical_findings": "2",
        "run_status": "completed"
      }
    }
  },
  "allowance_audit": [
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:33.906611+00:00 overview_checked_at=2026-10-05T19:17:03.727303+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:33.906611+00:00 overview_checked_at=2026-10-05T19:17:03.727303+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

### strategy_final

Actual rc=0. Raw local path /tmp/oct5-allon-strategy-final.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - --service strategy' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:03.727303+00:00 overview_checked_at=2026-10-05T19:16:33.625101+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:03.727303+00:00 overview_checked_at=2026-10-05T19:16:33.625101+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:16:56.126510-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T19:16:54.595326+00:00",
    "live:orb": "2026-10-05T19:16:55.024169+00:00"
  },
  "overview_bytes": 1142983,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:16:59.402379+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:16:59.442146+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T19:17:07.501382+00:00",
  "original_general_failures": [
    "reconciliation reports 2 total findings in the latest run.",
    "reconciliation reports 2 critical findings in the latest run.",
    "service reconciler is not healthy before deploy (status=degraded)."
  ],
  "allowance_findings": [
    {
      "symbol": "MI",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for MI (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:MI",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "180.00000000",
        "fill_delta": "180.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    },
    {
      "symbol": "NXL",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for NXL (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:NXL",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "2.00000000",
        "fill_delta": "2.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    }
  ],
  "allowance_run": {
    "id": "f43e13c0-2038-4cf3-8fd7-7291b3e3bb4e",
    "status": "completed",
    "completed_at": "2026-10-05 19:17:03.727367+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:17:03.727303+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "allowance_heartbeat": {
    "status": "degraded",
    "observed_at": "2026-10-05T19:17:03.740595Z",
    "payload": {
      "details": {
        "cutover_confidence": "30",
        "total_findings": "2",
        "critical_findings": "2",
        "run_status": "completed"
      }
    }
  },
  "allowance_audit": [
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:03.727303+00:00 overview_checked_at=2026-10-05T19:16:33.625101+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:17:03.727303+00:00 overview_checked_at=2026-10-05T19:16:33.625101+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

### flat_final

Actual rc=0. Raw local path /tmp/oct5-allon-flat-final.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python -' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:18:04.015829+00:00 overview_checked_at=2026-10-05T19:18:04.015829+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:18:04.015829+00:00 overview_checked_at=2026-10-05T19:18:04.015829+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:18:26.012710-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T19:18:24.685925+00:00",
    "live:orb": "2026-10-05T19:18:25.005788+00:00"
  },
  "overview_bytes": 1145614,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:18:14.593297+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:18:14.646814+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T19:18:27.557720+00:00",
  "original_general_failures": [],
  "allowance_findings": [
    {
      "symbol": "MI",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for MI (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:MI",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "180.00000000",
        "fill_delta": "180.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    },
    {
      "symbol": "NXL",
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "payload": {
        "title": "Our records claim a position missing at the broker for NXL (entry target $600 per order)",
        "fingerprint": "position-quantity:live:schwab_1m_v2:NXL",
        "account_name": "live:schwab_1m_v2",
        "account_quantity": "0",
        "virtual_quantity": "0",
        "managed_quantity": "0",
        "our_quantity": "0",
        "quantity_delta": "0",
        "net_fill_balance": "2.00000000",
        "fill_delta": "2.00000000",
        "direction": "broker_missing_owned_position",
        "ownership": "ours_or_conflicting",
        "configured_entry_quantity": null,
        "configured_entry_notional_usd": 600,
        "strategy_codes": [
          "schwab_1m_v2"
        ]
      }
    }
  ],
  "allowance_run": {
    "id": "a37b3828-20b7-4f31-a485-c04138ead349",
    "status": "completed",
    "completed_at": "2026-10-05 19:18:04.015882+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:18:04.015829+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "allowance_heartbeat": {
    "status": "degraded",
    "observed_at": "2026-10-05T19:18:04.023003Z",
    "payload": {
      "details": {
        "cutover_confidence": "30",
        "total_findings": "2",
        "critical_findings": "2",
        "run_status": "completed"
      }
    }
  },
  "allowance_audit": [
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:18:04.015829+00:00 overview_checked_at=2026-10-05T19:18:04.015829+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:18:04.015829+00:00 overview_checked_at=2026-10-05T19:18:04.015829+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

### strategy_initial_429

Actual rc=2. Raw local path /tmp/oct5-allon-strategy.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - --service strategy' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

```text
[WEBULL-ENDPOINT-CALLS] minute=2026-10-05T19:12+00:00 endpoint=/account/positions success=0 failure=1 total=1 partial=1
UNKNOWN strict-flat: ServerException: HTTP Status: 429, Code: TOO_MANY_REQUESTS, Msg: Too many requests, RequestID: 3172431c-4140-4991-8bed-308cd66269c1
```

### v2_fence

Actual rc=1. Raw local path /tmp/oct5-allon-v2-fence.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh'
```

```text
=== V2 RESTART PRE-FLIGHT — 2026-10-05 15:12:43 EDT ===
  [BLOCK] it is before 18:00 ET. ⛔ See the corrected note above: this threshold is a
          PROXY for 'nothing is armed', and it is known to be the wrong number in both
          directions. Gate 1 measures armed state directly — if it is green and you need
          to proceed, override deliberately:
            --clock-override '<reason>' --i-accept-clock
  [ok]    zero armed segments [published state, 0.3s old]
  [ok]    zero open managed rows
  [ok]    broker flat on both real-money accounts (operator manuals excluded)

  ===> NO-GO. Do not restart v2. Re-run when the blocking lines clear.
```

### oms_fence

Actual rc=0. Raw local path /tmp/oct5-allon-oms-fence.txt.

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh --require-all-account-positions-flat'
```

```text
=== OMS RESTART PRE-FLIGHT — 2026-10-05 15:13:01 EDT ===
  [ok]    database reachable
  [info]  session=REGULAR (09:30-16:00 ET) — a native stop CAN exist here
  [info]  strict all-account-position flatness enabled; no symbols are excluded
  [ok]    zero open managed rows
  [ok]    live:schwab_1m_v2 flat [4s old]
  [ok]    live:orb flat [4s old]

  ===> GO. Flat on every real-money account, zero managed rows, all sources fresh.
       Safe to restart the OMS.
```

## Hashes

```text
dcd35a3531418986e2fdeb3a1282c88001142e00cb19c15bba812077215323c0  docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
9bd8f17a43ffaf2bd7799c36e119b710ed6042ce82ba897b2bec80a5cca99546  docs/review-artifacts/tonight-candidate/test_standing_allowance.py
c7df7175e3aec2d1cadcc0087aa6c931b36b0b8aece3569d5043fc2e52930c07  docs/review-artifacts/tonight-candidate/mutate_standing_allowance.py
6949f8fae9ecb48eb54bc799b7ce5747217ef279a70543625b1d51871f8a6682  /tmp/oct5-allon-oms-final.txt
62733e2988f64954a2c6b1075553c5af8072a38cdaf12ee9cca3b2756f931f17  /tmp/oct5-allon-strategy-final.txt
7acd99088731d53ec18ff90ba1f9cdda2fad46e3bb9f38ea956089ce9348eb47  /tmp/oct5-allon-flat-final.txt
f700dad66d1871c360efb2501a7f07d43d2eb6c1f900c3e7504b8ce58ca91c64  /tmp/oct5-allon-strategy.txt
85929bdb20c5a4b37ec236c3f110d3fa59529fcceb4b5b035903c5d360d387dc  /tmp/oct5-allon-context-before.json
95feb09dd5891dbe912dc1e58cc3d78c80942a8c362d25ce168f1a2b0622aba9  /tmp/oct5-allon-context-after.json
404b1e24c556842689f79b0d4e54e89d8e4550aca5fbe0bce0abd191ef5d28c2  /tmp/oct5-standing-mutations.txt
```

## Owners After The Read-Only Runs

```json
{
  "momentum-paper": "[]",
  "orb-schwab": "[\"APUS\", \"MI\", \"VEEA\"]",
  "orb": "[\"APUS\", \"MI\", \"VEEA\"]",
  "strategy-engine": "[\"APUS\", \"JAGX\", \"MI\", \"SCKT\", \"VEEA\"]",
  "schwab-1m-v2": "[\"APUS\", \"JAGX\", \"MI\", \"VEEA\"]",
  "_migration_complete": "1",
  "_last_applied_id": "1791224650034-0"
}
```

The all-four-switch candidate trading replay and L5/L7 are separate proofs,
not established by this gate. No live flag/numeric test or process restart run.

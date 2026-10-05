# October 5 own read-only preflight dry run

Taken 2026-10-05 14:42-14:45 ET on mai-tai-vps. This is NOT an install,
not tonight's flat authorization, and not a substitute for the final runner's
review/pins/operator GO. No remote artifact, lock, journal, token, env, flag,
database row or service was written by these commands. Python bytecode disabled;
root/nice19; source imports from the clean box checkout e1ce3b39. The commands
below are the exact initial five preflight invocations required in the revised
candidate; SSH transports the flat helper through stdin, without staging it.
The runner itself does not exist/stay scheduled; its execution-only gates are
NOT claimed dry-run PASS. General gates run initially with services still up.

## Verdict

BLOCKED. General oms/strategy rc1 (two total, two critical, degraded reconciler).
Strict no-exception flat rc1 ONLY MI current-session net fills+180; fresh direct
brokers flat, managed/virtual/account rows empty. NXL+2 is historical; both
NXL and MI still block the reconciliation gate. OMS fence rc0. V2 fence rc1
ONLY daytime clock: no override passed, zero armed/managed, stored brokers flat.
No APUS holding or exception. Named operator disposition is requested in the
plan; it is neither approved nor implemented. No ledger write is proposed.

## Exact Commands And Unedited Output

### general_oms

Local transport, cwd is the candidate plan worktree:

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python /home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py --service oms --overview-url http://127.0.0.1:8100/api/overview'
```

Actual exit code: **1**.

```text
Live deploy preflight failed for oms.
- reconciliation reports 2 total findings in the latest run.
- reconciliation reports 2 critical findings in the latest run.
- service reconciler is not healthy before deploy (status=degraded).
```

### general_strategy

Local transport, cwd is the candidate plan worktree:

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python /home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py --service strategy --overview-url http://127.0.0.1:8100/api/overview'
```

Actual exit code: **1**.

```text
Live deploy preflight failed for strategy.
- reconciliation reports 2 total findings in the latest run.
- reconciliation reports 2 critical findings in the latest run.
- service reconciler is not healthy before deploy (status=degraded).
```

### v2_restart

Local transport, cwd is the candidate plan worktree:

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh'
```

Actual exit code: **1**.

```text
=== V2 RESTART PRE-FLIGHT — 2026-10-05 14:42:58 EDT ===
  [BLOCK] it is before 18:00 ET. ⛔ See the corrected note above: this threshold is a
          PROXY for 'nothing is armed', and it is known to be the wrong number in both
          directions. Gate 1 measures armed state directly — if it is green and you need
          to proceed, override deliberately:
            --clock-override '<reason>' --i-accept-clock
  [ok]    zero armed segments [published state, 1.1s old]
  [ok]    zero open managed rows
  [ok]    broker flat on both real-money accounts (operator manuals excluded)

  ===> NO-GO. Do not restart v2. Re-run when the blocking lines clear.
```

### oms_restart

Local transport, cwd is the candidate plan worktree:

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh --require-all-account-positions-flat'
```

Actual exit code: **0**.

```text
=== OMS RESTART PRE-FLIGHT — 2026-10-05 14:42:59 EDT ===
  [ok]    database reachable
  [info]  session=REGULAR (09:30-16:00 ET) — a native stop CAN exist here
  [info]  strict all-account-position flatness enabled; no symbols are excluded
  [ok]    zero open managed rows
  [ok]    live:schwab_1m_v2 flat [6s old]
  [ok]    live:orb flat [7s old]

  ===> GO. Flat on every real-money account, zero managed rows, all sources fresh.
       Safe to restart the OMS.
```

### strict_flat

Local transport, cwd is the candidate plan worktree:

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python -' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

Actual exit code: **1**.

```text
{
  "as_of_et": "2026-10-05T14:43:03.549782-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "blockers": [
    "net_bot_fills"
  ],
  "rc": 1
}
```

## Bounded Context, Not A Deploy Gate

```bash
ssh mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python -' < docs/review-artifacts/tonight-candidate/pull_gate_context.py
```

Actual rc=0. Unedited stdout below. Success means capture succeeded,
NOT gate PASS. Sequential live reads are not one atomic fleet snapshot; latest
SQL finding row may be newer than the overview. Final source SHA256:
`320d8026e818559115709d19d2f29977324349dd5026b1fc3a3d16d39f80cbf9`.
SQL is READ ONLY,5s statement bound,64-row sentinel. Overview read rejects beyond2MB;
actual1101314B. Redis INFO replies are small section-only metadata; TYPE/HLEN/
HSTRLEN scalar; HGETALL pre-read lengths sum<=100KB. No snapshot-batches payload
read, XINFO, EVAL or Redis write. Payload phases are diagnostic counts only:
null top-level account/symbol are not an identity proof, not five cleared tickets.

```json
{
  "as_of_utc": "2026-10-05T18:44:59.791612+00:00",
  "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
  "dirty": "",
  "import_path": "/home/trader/project-mai-tai/src/project_mai_tai/__init__.py",
  "overview_bytes": 1101314,
  "reconciliation": {
    "latest_run": {
      "status": "completed",
      "started_at": "2026-10-05 02:44:26 PM ET",
      "completed_at": "2026-10-05 02:44:26 PM ET",
      "summary": {
        "checked_at": "2026-10-05T18:44:26.548789+00:00",
        "accounts_checked": 4,
        "total_findings": 2,
        "critical_findings": 2,
        "warning_findings": 0,
        "info_findings": 0,
        "cutover_confidence": 30
      }
    },
    "findings": [
      {
        "severity": "critical",
        "finding_type": "position_quantity_mismatch",
        "symbol": "NXL",
        "title": "Our records claim a position missing at the broker for NXL (entry target $600 per order)",
        "created_at": "2026-10-05 02:44:26 PM ET"
      },
      {
        "severity": "critical",
        "finding_type": "position_quantity_mismatch",
        "symbol": "MI",
        "title": "Our records claim a position missing at the broker for MI (entry target $600 per order)",
        "created_at": "2026-10-05 02:44:26 PM ET"
      }
    ]
  },
  "overview_counts": {
    "strategies": 14,
    "broker_accounts": 12,
    "pending_intents": 0,
    "recent_fills": 7,
    "open_virtual_positions": 0,
    "open_account_positions": 0,
    "open_incidents": 14,
    "latest_reconciliation_findings": 2,
    "blacklisted_symbols": 0
  },
  "overview_errors": [],
  "services": [
    {
      "service_name": "market-data-gateway",
      "status": "healthy",
      "effective_status": "healthy",
      "observed_at_raw": "2026-10-05T18:44:59.857463Z",
      "observed_at": "2026-10-05 02:44:59 PM ET"
    },
    {
      "service_name": "oms-risk",
      "status": "healthy",
      "effective_status": "healthy",
      "observed_at_raw": "2026-10-05T18:45:09.508492Z",
      "observed_at": "2026-10-05 02:45:09 PM ET"
    },
    {
      "service_name": "orb",
      "status": "healthy",
      "effective_status": "healthy",
      "observed_at_raw": "2026-10-05T18:45:07.369084Z",
      "observed_at": "2026-10-05 02:45:07 PM ET"
    },
    {
      "service_name": "reconciler",
      "status": "degraded",
      "effective_status": "degraded",
      "observed_at_raw": "2026-10-05T18:44:56.631550Z",
      "observed_at": "2026-10-05 02:44:56 PM ET"
    },
    {
      "service_name": "schwab-1m-v2",
      "status": "healthy",
      "effective_status": "healthy",
      "observed_at_raw": "2026-10-05T18:45:06.306325Z",
      "observed_at": "2026-10-05 02:45:06 PM ET"
    },
    {
      "service_name": "strategy-engine",
      "status": "healthy",
      "effective_status": "healthy",
      "observed_at_raw": "2026-10-05T18:44:50.440032Z",
      "observed_at": "2026-10-05 02:44:50 PM ET"
    }
  ],
  "latest_findings": [
    {
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "symbol": "MI",
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
      "finding_type": "position_quantity_mismatch",
      "severity": "critical",
      "symbol": "NXL",
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
  "nonterminal_orders": [],
  "inflight_intents": [],
  "hold_census": [
    {
      "snapshot_type": "atr_reprice_handoff",
      "account": null,
      "symbol": null,
      "phase": "filled",
      "n": 1
    },
    {
      "snapshot_type": "atr_reprice_handoff",
      "account": null,
      "symbol": null,
      "phase": "held_unknown",
      "n": 5
    },
    {
      "snapshot_type": "atr_reprice_handoff",
      "account": null,
      "symbol": null,
      "phase": "refused",
      "n": 8
    }
  ],
  "schema_revision": [
    {
      "version_num": "20260916_0021"
    }
  ],
  "redis_before": {
    "evicted_keys": 0,
    "used_memory": 813121400
  },
  "owners": {
    "momentum-paper": "[]",
    "orb-schwab": "[\"APUS\", \"MI\", \"VEEA\"]",
    "orb": "[\"APUS\", \"MI\", \"VEEA\"]",
    "strategy-engine": "[\"APUS\", \"JAGX\", \"MI\", \"SCKT\", \"VEEA\"]",
    "schwab-1m-v2": "[\"APUS\", \"JAGX\", \"MI\", \"VEEA\"]",
    "_migration_complete": "1",
    "_last_applied_id": "1791224650034-0"
  },
  "stream_types": {
    "heartbeats": "stream",
    "market-data": "stream",
    "market-data-subscriptions": "stream",
    "order-events": "stream",
    "runtime-controls": "stream",
    "snapshot-batches": "stream",
    "strategy-intents": "stream",
    "strategy-state": "stream",
    "strategy-state-isolated": "stream"
  },
  "redis_after": {
    "evicted_keys": 0,
    "used_memory": 813117224
  },
  "hashes": {
    "/home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py": "6cd2b73b81c5a6bca5a5e0a1633008b7243875be4ee813d138ad32d4173552f8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh": "9631da8c40c7bb64879873c32509c5fb21d2a276be019398770f14bc966eb6c8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh": "3a0e05ee40ae2e5431b3418765189fe4ca1470d9742d60de24117c2fc741f225",
    "/home/trader/preopen.sh": "2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3"
  }
}
```

## Other Read-Only Observations

14:43-14:44 ET systemctl show of10 app units: OMS23705,
strategy24025, v226811, gateway2907, orb30225, orb-schwab27173,
control2916, reconciler2918, capture2917 all active/NRestarts0 with the previously
saved starts. Paper MainPID0/inactive/NRestarts0, Result=success,
InactiveEnterTimestamp=2026-10-05T13:40:01Z. It was NOT assumed active27320:
last guard records read from
/home/trader/after-hours/2026-10-05/option-a-treatment/option-a-guard.jsonl:

```json
{"action": "stop_paper", "at_utc": "2026-10-05T13:40:01.301207+00:00", "reason": "monitor_UNKNOWN:Blind:coverage load=9600/9600 1008=9599/9600", "systemctl_rc": 0}
{"action": "owner_release_confirmed", "attempt": 0}
{"action": "page_delivery", "at_utc": "2026-10-05T13:40:03.117089+00:00", "delivered": true, "title": "Option A paper STOP"}
```

This is a guard-recorded paper-only stop, not a live service failure. No start,
restart or repair follows from this audit. Its inactive identity must be bound
honestly at release review; an old active paper pin must not silently be called
valid. Current preopen hash2ef22340 is only READ, with the correct10-06 REPORT
line. Tomorrow's fixed-date gate was NOT run early. Source preflights were read
and have no writes; their existing manual exclusions do not permit a holding
through the separate no-exception direct helper. The old10-01 helper83191320
was read/hash-checked as an assessment input, NOT executed or approved for this
candidate. Its NXL/manual exceptions do not carry to this plan.

## Limits

No fresh pins/APPROVED_SHA/release manifest, execution clock/rotation, lock/claim,
future migration, post-restart logs/BOOT-HOLD, startup per-leg recovery, full
broker working-order inventory, catalog install/FLAGGATE/numeric, warm-up or
preopen edit was run. DB nonterminal orders0 is not a claim of complete broker
inventory.14 jobs/5 held_unknown remain identity-specific proof work, never age
clearance. The plan remains BLOCKED even though some flat/freshness checks pass.


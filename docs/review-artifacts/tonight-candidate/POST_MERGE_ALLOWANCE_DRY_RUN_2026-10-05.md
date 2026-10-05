# Post-merge final-byte allowance dry run, October 5

Application candidate: `7823a6fa7f63649b3f75ae3bcd07e16dc9b5dfaf`, whole tree
`ee6f058c248eeebf475fd392845eadfef7af59eb`, exactly equal to pinned #1093
`3b4e193ee1399c08045944ef79ce6ff67e4fbb41`. Merge completed17:10:40 ET;
fresh GitHub/main/tree checks repeated for this receipt. #1095 remains draft,
excluded; rebased head6b47c461 has unresolved restart/integration blockers.

Exact final helper SHA256 `ce3ab15bf95a5910e9b84b147d1460c9c494bc359fa6facfa80ebf9884e55ab8` (unchanged accepted bytes).
Local final-byte parser/policy suite53PASS in0.36s. No new mutation run claimed;
the accepted18RED and reviewer12RED are historical independent controls.

All commands serialized, fresh direct GETs to BOTH brokers per invocation.
Root/nice19/PYTHONDONTWRITEBYTECODE=1, helper sent through SSH stdin without
remote staging, token refresh, database/Redis write or service/config action.
SQL transactions READ ONLY; helper reads no snapshot-batches or Redis streams.
Broker body bound1MB, Webull page50/cap20, overview bound2MB, SQL rows<=64
(sentinel LIMIT65 refuses overflow). Actual replies are in the full output.

Own box reads during this rerun: HEAD
`e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89`, git status porcelain empty.
New application is NOT installed; the candidate's merged tree and final helper
bytes do not imply its services, migration, env, catalogs or runner are live.

| Command mode | Fresh direct read starts UTC (Schwab / Webull) | Completed UTC | Process/helper rc | Remaining blockers |
|---|---|---|---|---|
| oms | 2026-10-05T21:22:40.967139+00:00 / 2026-10-05T21:22:41.527411+00:00 | 2026-10-05T21:22:59.807594+00:00 | 0 / 0 | none |
| strategy | 2026-10-05T21:23:11.331881+00:00 / 2026-10-05T21:23:11.886015+00:00 | 2026-10-05T21:23:28.037750+00:00 | 0 / 0 | none |
| flat | 2026-10-05T21:23:34.091846+00:00 / 2026-10-05T21:23:34.981285+00:00 | 2026-10-05T21:23:51.417013+00:00 | 0 / 0 | none |

Every invocation had zero broker holdings, open managed/virtual/account rows,
working DB orders and inflight intents. MI+180 current-session fills admitted
only with the matching exact finding; NXL+2 standing finding separately printed.
General OMS/strategy modes preserved the original3 failures (total/critical
counts and reconciler degraded) and removed only those attributable to the two
exact accepted findings. No broadened exception or ledger adjustment.

The standalone flat mode does NOT run the general preflight. v2 armed/clock
fence, strict OMS restart fence, broker working-order census, all-date durable
startup ticket census, fresh process identity, Redis warm-up proofs and actual
install-time checks remain required separately. They are not claimed passed
by these three calls. This receipt is not an execution GO or future flatness
certificate. No runner, release, service or timer has been staged by this task.

## oms

Exact command (stdin file is the final helper above):

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - --service oms' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

Process exit0. Full stdout below; no stderr was emitted.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:27.852342+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:27.852342+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T17:22:42.528970-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "schwab_mapping_response_bytes": 109,
  "schwab_identity_bound": true,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T21:22:40.967139+00:00",
    "live:orb": "2026-10-05T21:22:41.527411+00:00"
  },
  "overview_bytes": 1380126,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 21:22:51.573079+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 21:22:51.615566+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "45ce391f-2534-4d77-a46b-e193293f5a1f",
    "status": "completed",
    "completed_at": "2026-10-05 21:22:27.852394+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:22:27.852342+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "overview_sql_findings": [
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
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T21:22:59.807594+00:00",
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
    "id": "5cff9037-cd04-43b0-8e3a-11a98ceed02f",
    "status": "completed",
    "completed_at": "2026-10-05 21:22:57.900977+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:22:57.900919+00:00",
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
    "observed_at": "2026-10-05T21:22:57.906150Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:27.852342+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:27.852342+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## strategy

Exact command (stdin file is the final helper above):

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python - --service strategy' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

Process exit0. Full stdout below; no stderr was emitted.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:57.900919+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:57.900919+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T17:23:12.702544-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "schwab_mapping_response_bytes": 109,
  "schwab_identity_bound": true,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T21:23:11.331881+00:00",
    "live:orb": "2026-10-05T21:23:11.886015+00:00"
  },
  "overview_bytes": 1380247,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 21:23:21.447679+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 21:23:21.477381+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "5cff9037-cd04-43b0-8e3a-11a98ceed02f",
    "status": "completed",
    "completed_at": "2026-10-05 21:22:57.900977+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:22:57.900919+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "overview_sql_findings": [
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
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T21:23:28.037750+00:00",
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
    "id": "5cff9037-cd04-43b0-8e3a-11a98ceed02f",
    "status": "completed",
    "completed_at": "2026-10-05 21:22:57.900977+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:22:57.900919+00:00",
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
    "observed_at": "2026-10-05T21:22:57.906150Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:57.900919+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:22:57.900919+00:00 overview_checked_at=2026-10-05T21:22:57.900919+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## flat

Exact command (stdin file is the final helper above):

```sh
ssh -o BatchMode=yes -o ConnectTimeout=10 mai-tai-vps 'sudo nice -n 19 env PYTHONDONTWRITEBYTECODE=1 /home/trader/project-mai-tai/.venv/bin/python -' < docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
```

Process exit0. Full stdout below; no stderr was emitted.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:23:27.968441+00:00 overview_checked_at=2026-10-05T21:23:27.968441+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:23:27.968441+00:00 overview_checked_at=2026-10-05T21:23:27.968441+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T17:23:35.826158-04:00",
  "fill_session_start_et": "2026-10-05T04:00:00-04:00",
  "broker_holdings": [],
  "schwab_response_bytes": 2151,
  "schwab_mapping_response_bytes": 109,
  "schwab_identity_bound": true,
  "webull_response_bytes": [
    32
  ],
  "manual_exceptions": [],
  "direct_read_started_at": {
    "live:schwab_1m_v2": "2026-10-05T21:23:34.091846+00:00",
    "live:orb": "2026-10-05T21:23:34.981285+00:00"
  },
  "overview_bytes": 1380610,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 21:23:36.550051+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 21:23:36.596318+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "c6360415-21b3-44af-bbcb-e48dcd6d5466",
    "status": "completed",
    "completed_at": "2026-10-05 21:23:27.968484+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:23:27.968441+00:00",
      "accounts_checked": 4,
      "total_findings": 2,
      "critical_findings": 2,
      "warning_findings": 0,
      "info_findings": 0,
      "cutover_confidence": 30
    }
  },
  "overview_sql_findings": [
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
  "net_bot_fills": [
    {
      "account": "live:schwab_1m_v2",
      "symbol": "MI",
      "net": "180.00000000",
      "total": 3,
      "known": 3
    }
  ],
  "proof_completed_at_utc": "2026-10-05T21:23:51.417013+00:00",
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
    "id": "c6360415-21b3-44af-bbcb-e48dcd6d5466",
    "status": "completed",
    "completed_at": "2026-10-05 21:23:27.968484+00:00",
    "summary": {
      "checked_at": "2026-10-05T21:23:27.968441+00:00",
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
    "observed_at": "2026-10-05T21:23:27.973463Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:23:27.968441+00:00 overview_checked_at=2026-10-05T21:23:27.968441+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T21:23:27.968441+00:00 overview_checked_at=2026-10-05T21:23:27.968441+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

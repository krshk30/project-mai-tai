# Reviewer six-guard follow-up and fresh dry run, October 5

Final helper SHA256 `ce3ab15bf95a5910e9b84b147d1460c9c494bc359fa6facfa80ebf9884e55ab8`.
Only helper change from accepted5e9d236b is its explanatory docstring; no policy
or broker read semantics changed. Tests pin the six existing guards. No service,
remote staging, config/env, token, database/ledger, migration or production write.

## Six Requested Controls

53 tests PASS (0.39s). All18 isolated in-memory guard-removal mutations
assertion-RED; no source edit or setup/import-error counted as a kill.
Independent Mencius verified each requested six specifically raises an assertion
failure after only its guard is removed; original tests pass (53 in0.38s).
Ruff PASS.

| Guard | Test | Guard removed |
|---|---|---|
| duplicate symbol | test_duplicate_exact_mi_finding_refused_even_when_all_counts_match | RED |
| severity critical | test_noncritical_finding_refused_even_when_cached_sql_and_overview_agree | RED |
| finding type | test_other_finding_type_refused_even_when_all_identity_sources_agree | RED |
| MI finding required for MI fill | test_current_mi_180_requires_its_matching_finding_not_only_nxl | RED |
| current fill symbol MI | test_other_symbol_180_never_uses_mi_current_session_allowance | RED |
| raw heartbeat status | test_raw_heartbeat_status_must_agree_even_when_overview_is_degraded | RED |

Cached SQL/overview/full finding identities, counts and details remain coherent
in each negative fixture. Other guards cannot mask these deletions.

## CI Cancellation Is Runner Acquisition Failure

Own GitHub API reads15:45-15:46 ET: oldhead5ddb push37363490768 passed;
pull37363497022 concludedfailure with its jobcancelled after15m02s. The actual
job111943287588 had runner_id0, runner_name empty, runner_group_id0, steps[].
No setup or test step began. Check-run failure annotation exactly:

> The job was not acquired by Runner of type hosted even after multiple attempts

[Run](https://github.com/krshk30/project-mai-tai/actions/runs/37363497022),
[job](https://github.com/krshk30/project-mai-tai/actions/runs/37363497022/job/111943287588).
API requests: actions/jobs/111943287588 and check-runs/111943287588/annotations.
No slow or hanging test is implicated because no runner executed the job.
This diagnosis does NOT substitute for BOTH Validate PASS on the new exact head.
Do not pin/merge a queued, cancelled or otherwise non-green validation.

## Exact New-Byte Dry Runs

Each command serialized and awaited before the next broker call, root/nice19,
PYTHONDONTWRITEBYTECODE=1, /home/trader/project-mai-tai/.venv/bin/python -,
stdin exact finalhelper; --service oms, then --service strategy, then nooption.
Fresh direct reads BOTH brokers percommand. Every process exit0; no broker
holding, managed/virtual/account row, working order or inflight intent.
Original general failures retained, only exactMI/NXLcounts/degraded admitted;
remaining failures empty. Each audit line printed below. Never execution GO.

### oms

Raw path /tmp/oct5-six-guards-oms.txt; process exit0.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:10.451818+00:00 overview_checked_at=2026-10-05T19:46:40.384266+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:10.451818+00:00 overview_checked_at=2026-10-05T19:46:40.384266+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:47:14.352625-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:47:12.910155+00:00",
    "live:orb": "2026-10-05T19:47:13.429439+00:00"
  },
  "overview_bytes": 1197187,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:47:20.368478+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:47:20.411754+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "e1bc378f-0718-4bf1-9a40-0c1791143b66",
    "status": "completed",
    "completed_at": "2026-10-05 19:46:40.384319+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:46:40.384266+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:47:26.359957+00:00",
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
    "id": "c2543c3e-b409-402f-956c-1e692b543941",
    "status": "completed",
    "completed_at": "2026-10-05 19:47:10.451927+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:47:10.451818+00:00",
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
    "observed_at": "2026-10-05T19:47:10.470078Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:10.451818+00:00 overview_checked_at=2026-10-05T19:46:40.384266+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:10.451818+00:00 overview_checked_at=2026-10-05T19:46:40.384266+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

### strategy

Raw path /tmp/oct5-six-guards-strategy.txt; process exit0.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:40.546116+00:00 overview_checked_at=2026-10-05T19:47:10.451818+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:40.546116+00:00 overview_checked_at=2026-10-05T19:47:10.451818+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:47:38.402060-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:47:36.918996+00:00",
    "live:orb": "2026-10-05T19:47:37.522952+00:00"
  },
  "overview_bytes": 1197397,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:47:35.527779+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:47:35.564059+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "c2543c3e-b409-402f-956c-1e692b543941",
    "status": "completed",
    "completed_at": "2026-10-05 19:47:10.451927+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:47:10.451818+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:47:50.770561+00:00",
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
    "id": "d7cae45e-8943-434f-82a8-4de5ebe18ccc",
    "status": "completed",
    "completed_at": "2026-10-05 19:47:40.546167+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:47:40.546116+00:00",
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
    "observed_at": "2026-10-05T19:47:40.551512Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:40.546116+00:00 overview_checked_at=2026-10-05T19:47:10.451818+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:47:40.546116+00:00 overview_checked_at=2026-10-05T19:47:10.451818+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

### flat

Raw path /tmp/oct5-six-guards-flat.txt; process exit0.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:48:10.626967+00:00 overview_checked_at=2026-10-05T19:47:40.546116+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:48:10.626967+00:00 overview_checked_at=2026-10-05T19:47:40.546116+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:47:56.121089-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:47:54.606495+00:00",
    "live:orb": "2026-10-05T19:47:55.201834+00:00"
  },
  "overview_bytes": 1200663,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:48:05.705356+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:48:05.746500+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "d7cae45e-8943-434f-82a8-4de5ebe18ccc",
    "status": "completed",
    "completed_at": "2026-10-05 19:47:40.546167+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:47:40.546116+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:48:13.715592+00:00",
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
    "id": "3ad04c3d-a739-4504-ae0b-54ae76d8c45a",
    "status": "completed",
    "completed_at": "2026-10-05 19:48:10.627036+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:48:10.626967+00:00",
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
    "observed_at": "2026-10-05T19:48:10.635220Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:48:10.626967+00:00 overview_checked_at=2026-10-05T19:47:40.546116+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:48:10.626967+00:00 overview_checked_at=2026-10-05T19:47:40.546116+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## Redis Before/After: Metadata Only, No Snapshot Payload Reads

```json
{
  "when": "before",
  "asof": "2026-10-05T19:46:42.644937+00:00",
  "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
  "dirty": "",
  "redis_before": {
    "evicted_keys": 0,
    "used_memory": 807123416
  },
  "redis_after": {
    "evicted_keys": 0,
    "used_memory": 807118216
  },
  "owners": {
    "momentum-paper": "[]",
    "orb-schwab": "[\"APUS\", \"MI\", \"VEEA\"]",
    "orb": "[\"APUS\", \"MI\", \"VEEA\"]",
    "strategy-engine": "[\"APUS\", \"MI\", \"SCKT\", \"VEEA\"]",
    "schwab-1m-v2": "[\"APUS\", \"MI\", \"VEEA\"]",
    "_migration_complete": "1",
    "_last_applied_id": "1791229026133-0"
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
  "hashes": {
    "/home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py": "6cd2b73b81c5a6bca5a5e0a1633008b7243875be4ee813d138ad32d4173552f8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh": "9631da8c40c7bb64879873c32509c5fb21d2a276be019398770f14bc966eb6c8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh": "3a0e05ee40ae2e5431b3418765189fe4ca1470d9742d60de24117c2fc741f225",
    "/home/trader/preopen.sh": "2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3"
  }
}
```

```json
{
  "when": "after",
  "asof": "2026-10-05T19:48:29.151067+00:00",
  "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
  "dirty": "",
  "redis_before": {
    "evicted_keys": 0,
    "used_memory": 807156096
  },
  "redis_after": {
    "evicted_keys": 0,
    "used_memory": 807162448
  },
  "owners": {
    "momentum-paper": "[]",
    "orb-schwab": "[\"APUS\", \"MI\", \"VEEA\"]",
    "orb": "[\"APUS\", \"MI\", \"VEEA\"]",
    "strategy-engine": "[\"APUS\", \"MI\", \"SCKT\", \"VEEA\"]",
    "schwab-1m-v2": "[\"APUS\", \"MI\", \"VEEA\"]",
    "_migration_complete": "1",
    "_last_applied_id": "1791229026133-0"
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
  "hashes": {
    "/home/trader/project-mai-tai/src/project_mai_tai/deploy_preflight.py": "6cd2b73b81c5a6bca5a5e0a1633008b7243875be4ee813d138ad32d4173552f8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_v2_restart.sh": "9631da8c40c7bb64879873c32509c5fb21d2a276be019398770f14bc966eb6c8",
    "/home/trader/project-mai-tai/ops/preflight/preflight_oms_restart.sh": "3a0e05ee40ae2e5431b3418765189fe4ca1470d9742d60de24117c2fc741f225",
    "/home/trader/preopen.sh": "2ef22340d34ebe5630d140729384409e69c8953485d3ee409e7c5551832f5af3"
  }
}
```

## Raw Hashes

```text
ce3ab15bf95a5910e9b84b147d1460c9c494bc359fa6facfa80ebf9884e55ab8  docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
cf2aaf18610792e05820f62cd602dcd8bbace4f81ae9a4cdbbdfada94fc3df8c  /tmp/oct5-six-guards-oms.txt
73f19a74cc4b8d09bee1dd03be90e022a67628ee419949b8f02a4b1ac0889716  /tmp/oct5-six-guards-strategy.txt
46e47f0bbb1b51417cf0dca36a10f0214813dd4f39815ce830594b9850ac6338  /tmp/oct5-six-guards-flat.txt
89806a8c12bbd6ba9975dd5df434d7562d259299cb4ebe6c4f196d2d509861c1  /tmp/oct5-six-guards-context-before.json
175852ecf01191d1783e3af802f5582263470f945ab88e2988518a76fa0b3afb  /tmp/oct5-six-guards-context-after.json
ee15411b5153587e1181f35547feeeed3fb4e842fbf1dacae4b551a658607c3f  /tmp/oct5-standing-review-six-mutations.txt
```

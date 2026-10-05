# Final standing-allowance verification, October 5

Exact reviewed helper SHA256 `5e9d236b0e139b5b59868dfc4a90b57eaa4edee6981b3a3f37dc183df0428b04`.
No production write, staging, token refresh, restart, ledger change or install.
Fresh broker calls were serialized, never reused between gates. This is a dry run,
not execution authorization or proof of future flatness.

## Tests And Independent Impact Read

47 tests PASS (0.26s); 12/12 isolated in-memory guard-removal mutations
assertion-RED: fingerprint, balance, account quantity, current-session fill,
degraded error, deepcopy, freshness, cached identity, cached title,
Webull conflicting collections, explicit pagination, Schwab account identity.
Ruff PASS. Mencius independently re-reviewed final exact bytes: no remaining
unsafe-pass finding identified. Its independent collection-conflict mutation
also fails the specific negative test. Collector orchestration lacks direct
unit coverage; live serialized reads exercise it, not every transport race.
Direct broker working-order/unknown-dispatch census remains a separate gate.

Four findings fixed before this final run: cached overview bound to its exact
full SQL run; explicit Webull completion; empty Schwab response bound by a fresh
accountNumbers mapping; conflicting holdings/positions rejected. Old dcd35a35
successful runs remain HISTORICAL in ALL_ON_GATE_DRY_RUN_2026-10-05.md.

## Gate Results

Fresh OMS and strategy general gates and strict-flat each exit0 below.
Only exact MI180/NXL2 findings and MI current-session180 admitted; all broker
holdings, managed/virtual/account rows, working orders and inflight intents empty.
Original general evaluator reports its original three failures, then adjusted
in-memory evaluation has zero remaining failures. No source status is changed.
Schwab mapping109B/positions2151B, Webull32B, overview below2MB.
Original restart fences remain unchanged: prior own v2 dryrun exits1 ONLY
before18:00 clock (armed0/rows0), OMS strict fence0. These are not all-gates-green;
no clock override and no early Oct6 gate. All gates rerun under future GO.

## oms: exact stdout

Command: ssh mai-tai-vps, root nice19/PYTHONDONTWRITEBYTECODE=1,
/home/trader/project-mai-tai/.venv/bin/python - --service oms;
stdin = final helper above. Process exit0.
Raw local path: /tmp/oct5-allon-oms-reviewed-final.txt.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:35:07.989886+00:00 overview_checked_at=2026-10-05T19:35:07.989886+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:35:07.989886+00:00 overview_checked_at=2026-10-05T19:35:07.989886+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:35:32.468321-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:35:30.313023+00:00",
    "live:orb": "2026-10-05T19:35:31.168058+00:00"
  },
  "overview_bytes": 1174408,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:35:18.189144+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:35:18.256330+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "bfc2ad3e-52eb-4c59-98c4-817963605066",
    "status": "completed",
    "completed_at": "2026-10-05 19:35:07.989956+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:35:07.989886+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:35:33.329402+00:00",
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
    "id": "bfc2ad3e-52eb-4c59-98c4-817963605066",
    "status": "completed",
    "completed_at": "2026-10-05 19:35:07.989956+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:35:07.989886+00:00",
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
    "observed_at": "2026-10-05T19:35:08.009117Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:35:07.989886+00:00 overview_checked_at=2026-10-05T19:35:07.989886+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:35:07.989886+00:00 overview_checked_at=2026-10-05T19:35:07.989886+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## strategy: exact stdout

Command: ssh mai-tai-vps, root nice19/PYTHONDONTWRITEBYTECODE=1,
/home/trader/project-mai-tai/.venv/bin/python - --service strategy;
stdin = final helper above. Process exit0.
Raw local path: /tmp/oct5-allon-strategy-reviewed-final.txt.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:08.226362+00:00 overview_checked_at=2026-10-05T19:35:38.127159+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:08.226362+00:00 overview_checked_at=2026-10-05T19:35:38.127159+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:36:11.153299-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:36:09.730111+00:00",
    "live:orb": "2026-10-05T19:36:10.235370+00:00"
  },
  "overview_bytes": 1177631,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:36:18.206556+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:36:18.246464+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "007876be-cd2f-4199-97c6-9ba297038d5b",
    "status": "completed",
    "completed_at": "2026-10-05 19:35:38.127229+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:35:38.127159+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:36:23.192111+00:00",
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
    "id": "bf48aa05-10d8-448a-8859-a51ed476e92a",
    "status": "completed",
    "completed_at": "2026-10-05 19:36:08.226427+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:36:08.226362+00:00",
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
    "observed_at": "2026-10-05T19:36:08.233720Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:08.226362+00:00 overview_checked_at=2026-10-05T19:35:38.127159+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:08.226362+00:00 overview_checked_at=2026-10-05T19:35:38.127159+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## flat: exact stdout

Command: ssh mai-tai-vps, root nice19/PYTHONDONTWRITEBYTECODE=1,
/home/trader/project-mai-tai/.venv/bin/python -;
stdin = final helper above. Process exit0.
Raw local path: /tmp/oct5-allon-flat-reviewed-final.txt.

```text
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:38.319641+00:00 overview_checked_at=2026-10-05T19:36:08.226362+00:00
[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:38.319641+00:00 overview_checked_at=2026-10-05T19:36:08.226362+00:00
[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh
{
  "as_of_et": "2026-10-05T15:36:28.325043-04:00",
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
    "live:schwab_1m_v2": "2026-10-05T19:36:26.739940+00:00",
    "live:orb": "2026-10-05T19:36:27.408059+00:00"
  },
  "overview_bytes": 1177544,
  "managed_rows": [],
  "virtual_rows": [],
  "account_rows": [],
  "account_stamps": [
    {
      "account": "live:orb",
      "updated_at": "2026-10-05 19:36:33.181310+00:00"
    },
    {
      "account": "live:schwab_1m_v2",
      "updated_at": "2026-10-05 19:36:33.218969+00:00"
    }
  ],
  "working_orders": [],
  "inflight_intents": [],
  "overview_sql_run": {
    "id": "bf48aa05-10d8-448a-8859-a51ed476e92a",
    "status": "completed",
    "completed_at": "2026-10-05 19:36:08.226427+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:36:08.226362+00:00",
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
  "proof_completed_at_utc": "2026-10-05T19:36:46.310837+00:00",
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
    "id": "78518c64-677d-4137-895c-0434df2ef39d",
    "status": "completed",
    "completed_at": "2026-10-05 19:36:38.319726+00:00",
    "summary": {
      "checked_at": "2026-10-05T19:36:38.319641+00:00",
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
    "observed_at": "2026-10-05T19:36:38.332132Z",
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
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:MI balance=180 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:38.319641+00:00 overview_checked_at=2026-10-05T19:36:08.226362+00:00",
    "[STANDING-ALLOWANCE] fingerprint=position-quantity:live:schwab_1m_v2:NXL balance=2 account_quantity=0 virtual_quantity=0 managed_quantity=0 broker_flat=fresh sql_checked_at=2026-10-05T19:36:38.319641+00:00 overview_checked_at=2026-10-05T19:36:08.226362+00:00",
    "[STANDING-ALLOWANCE] current_session_net_fill account=live:schwab_1m_v2 symbol=MI balance=180 broker_flat=fresh"
  ],
  "remaining_general_failures": [],
  "general_warnings": [],
  "blockers": [],
  "rc": 0
}
```

## Redis And Box Context Before/After

Metadata only (INFO, TYPE, pre-length-bounded owners HGETALL); no snapshot
payload, XINFO or EVAL. Context captures are not an atomic fleet snapshot.

```json
{
  "when": "before",
  "as_of": "2026-10-05T19:35:12.444674+00:00",
  "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
  "dirty": "",
  "redis_before": {
    "evicted_keys": 0,
    "used_memory": 806872544
  },
  "redis_after": {
    "evicted_keys": 0,
    "used_memory": 806874264
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
  "as_of": "2026-10-05T19:36:50.045085+00:00",
  "box_sha": "e1ce3b3978fbcb1ff00daecdf6dc2618e6b5fb89",
  "dirty": "",
  "redis_before": {
    "evicted_keys": 0,
    "used_memory": 806999032
  },
  "redis_after": {
    "evicted_keys": 0,
    "used_memory": 806999032
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
5e9d236b0e139b5b59868dfc4a90b57eaa4edee6981b3a3f37dc183df0428b04  docs/review-artifacts/tonight-candidate/strict_flat_readonly.py
8786eb213e51d33a9e35a7521c2c3d7633b4283336395af0e9925c9f6c548ec0  /tmp/oct5-allon-oms-reviewed-final.txt
8150da4592b01e5872de1c819cb8b86cd048ab1d2b7a93093bab29e7cac2a0be  /tmp/oct5-allon-strategy-reviewed-final.txt
f1645b4134d94eb86fdab4838f4d6debda436098585a84816a4c145668d0b3a5  /tmp/oct5-allon-flat-reviewed-final.txt
d64144c4b053ce08be5be75bbe8104d3325687482ecbacff48501b71031a2030  /tmp/oct5-standing-mutations-final.txt
fd059f3c51d79d17468ae0d53c029d0b58bd0381fb096d10d7dcbd78e45f90c5  /tmp/oct5-allon-context-identity-before.json
6f35c24287790805188ab67a3a1694efb8cb779aa2354d208795390ee4db34fb  /tmp/oct5-allon-context-identity-after.json
```

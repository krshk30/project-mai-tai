# Operator Holdings Install2 Contract

[codex] Own Step0 assessment: AGREE on the zero current-session bot-order COUNT
AND fill COUNT rule, with zero residual historical ownership, fresh complete
source evidence and no open rows, pending/unknown order/intent or unowned SELL.
The transferred reconciler-only build is superseded: its tolerance-filtered
ledger and narrow active-status aliases were insufficient for gate adoption.

No production or new broker read was made by this build. Parent's bounded
15:22:35.239166 ET DB receipt shows APUS78 (7 orders/1 fill, residual78) and
MOBX504 (5 orders/3 fills, residual504): both remain bot-owned. It does not
establish direct broker truth, complete activity, or present IPDN1000.
The raw receipt remains parent evidence, not falsely relabeled an independent
direct GET by this lane. Exact current IPDN activation remains UNMEASURED.

## Published Python Interface

`project_mai_tai.operator_holding_evidence` exports `DirectAccountPositions`,
`OperatorHoldingsProof`, and `build_operator_holdings_proof`.

Kant's Install2 producer must perform complete direct broker GETs for BOTH
`live:schwab_1m_v2` and `live:orb`, validating raw account identity, every
holding/quantity, and full response shape. Do NOT claim `complete=True` from an
OMS DB cache, a failed/truncated list, skipped malformed nodes, a cached adapter
fallback or a status-only hint. Empty is valid only for a complete successful
direct response. No token refresh, submit, cancel or ledger correction is needed
or authorized by this interface. Adapter list methods alone are not sufficient
completeness attestation (Schwab skips malformed rows, Webull can serve cache).

Construct immutable `DirectAccountPositions(account_id, account_name, read_at,
complete, positions)` with complete `(symbol, Decimal(quantity))` tuples. Then,
inside a bounded READ ONLY / REPEATABLE READ DB transaction AFTER those GETs,
read the exact active-live account inventory and call:

```python
proof = build_operator_holdings_proof(
    session, accounts,
    direct_positions=direct_reads,
    now=database_read_at,
    fill_balance_since=settings.reconciliation_fill_balance_since,
)
failures = evaluate_live_deploy_preflight(
    overview, service_target="oms", now=gate_now,
    operator_holdings_proof=proof,
)
```

The builder reads actual DB account identities and rejects supplied inventories
that differ. Query exceptions propagate, never turn into an empty census.
The gate validates complete/fresh (120s, no future clock), exact duplicate-free
account and position inventories, DB read after direct GET, exact counts/books/
ledger and equal direct/overview nonzero position counts. Without a typed proof,
the existing broker-position count still blocks. Pending/recent-fill/critical
reconciliation/heartbeat gates are NOT waived by an operator proof. Proof is an
in-process trusted producer contract, not a signed artifact or bool/JSON waiver.
The caller owns actor fencing and its last-moment repeat before any restart;
120s freshness is not an atomic concurrent-writer guarantee.

General exemption requires BOTH zero session counts from the existing04:00ET
anchor. Rejected/aborted/no-submitted orders still count; null timestamps use
linked intent creation/order update, fully unknown or future times stay unknown.
Open zero-quantity virtual/managed rows and pending SELL aliases block. Nonzero
historical strategy balances cannot cancel across strategies or disappear under
quantity tolerance. The separately dated October6 IPDN1000 item is exact account,
symbol, quantity and ET day, requires completed bot activity with no residual
ownership/pending/unowned SELL, and never generalizes a net-zero round trip.

## Adoption Boundary

Only the normal Python preflight evaluator has the typed operator-proof path.
Its ordinary CLI still supplies no direct proof and therefore remains literal
flat. `preflight_oms_restart.sh --require-all-account-positions-flat` retains its
literal exposure requirement for throughput/load studies; no operator waiver
is added there. Older shell protected-symbol copies are NOT evidence under this
contract and are not represented as fixed or adopted.

Install1 is immutable at source4805. This branch does not modify Kant's worktree,
runner, shared HANDOFF or source pin. Kant must review/adopt the typed producer
and actual Install2 caller separately, with its own direct-read, readonly
transaction, actor-fence and fresh final-recheck controls. Until that caller is
adopted/tested, this PR MUST remain draft, not ready for installation.

Existing MI180/NXL2 historical mismatch allowances remain separate, unchanged
in source/settings; no general operator classification or ledger normalization
is introduced for them. Their nonzero historical balances cannot pass this
helper. Kant's already-approved historical gate dispositions remain their own
exact item policy, not a mask over current bot activity/open rows or unowned SELL.
The reconciler requires a separately reviewed restart to load its change;
neither that restart nor Install2 approval follows from this build.

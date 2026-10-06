# WEBULL429 / WBPOWER1 stable read contract

[codex] Released by the human parent2026-10-06. WEBULL429 writes only its own
branch; WBPOWER1 writes only its own branch. No dependency cherry-pick, main
merge, production write, or shared handoff/ledger edit is authorized by this
contract. Parent composes the two lanes.

## Public Budget API

These exports from `project_mai_tai.broker_adapters.webull_order_reads` are stable:

```python
shared_budget(host: str, app_key: str) -> QueryBudget
QueryBudget.claim(endpoint: str, owner: str, *, strict: bool = False) -> None
QueryBudgetUnavailable(RuntimeError)
```

Use `shared_budget(adapter.host, adapter.app_key)`, not a new `QueryBudget()` in
production. Identical host/app-key strings return the SAME process-local budget
across adapter instances, account aliases, and threads. Secrets are registry
keys only, never evidence/log fields. This is not distributed enforcement across
multiple OMS processes and not a measured legacy venue quota.

Own bounded source-only SSH inspection2026-10-06 confirms the installed core
`ApiClient` defaults `auto_retry=False`, selecting `NO_RETRY_POLICY`; its
`get_response` calls one transport operation. `Response.get_response_object`
uses a default Requests Session with `allow_redirects=False`. This adapter's
production constructor does not enable auto retry. Do not inject retry-enabled
clients under this one-per-HTTP contract. No SDK/auth function or broker API
was called during this source inspection. Higher-level Account/BaseApi auth
side effects remain WBPOWER1's separate safety inspection, not this attestation.

Source prefix: `/home/trader/project-mai-tai/.venv/lib/python3.12/site-packages/`.
Read ranges were core/client.py1-240 and265-640, core/http/response.py1-220,
core/retry/retry_policy.py1-200, plus one SHA256-only read of those three files:

| File | SHA256 |
| --- | --- |
| `webull/core/client.py` | `cf148188823d283c5ce6d1e94deac97d5fcf28fb7c13d7b7a133a989a900b49c` |
| `webull/core/http/response.py` | `882b924f71271d4d8dc04bef564d018e1ff471f355d0fc7b20c73c75cdc803fd` |
| `webull/core/retry/retry_policy.py` | `cb52b073b970d4de2c82f47bd407146ecbfc71860b00945d0ccc7b5563bab9bb` |

Canonical keys (logical endpoint names, not broker URL paths):

| Key | Scope / Owner |
| --- | --- |
| `list-today` | WEBULL429; exact real account ID |
| `detail` | WEBULL429; exact account ID plus client ID |
| `account-balance` | WBPOWER1; exact real account ID |

The counters are independent per endpoint in one shared app-key registry.
Every HTTP attempt, including failed attempts and pagination, consumes one
permit immediately before the SDK call. Each endpoint has a conservative
ceiling2 starts per rolling2s. Ordinary `detail` has ceiling1 to reserve a
second permit for fresh strict RPG/EOD/cancel proof. Strict calls can bypass
ordinary fair queue order, but never the ceiling2. WBPOWER1 uses `strict=False`
and must not consume the `detail`/`list-today` keys or borrow strict priority.

```python
budget = shared_budget(adapter.host, adapter.app_key)
budget.claim("account-balance", exact_real_account_id)
# Immediately make ONE verified read-only SDK balance request.
```

Claims are nonblocking, consumed permits, not locks or leases. No HTTP request
runs while the budget's internal lock is held. Denial raises
`QueryBudgetUnavailable`: retain UNKNOWN, do not sleep, silently retry, invent
zero/available funds, or interpret denial as flatness/clearance. Fair queued
owners are real accounts; abandoned owners expire after30s to avoid an immortal
queue head. This expiry is query bookkeeping, not a scheduler/trading timer.

WBPOWER1 confirmed acceptance of this key/API through human-authorized thread
coordination. Its installed-SDK balance-path validation remains its own source
assessment; this document does not invent a returned day-buying-power field.

## Cache and Detail Boundaries

Order-only public exports are `shared_reader(host, app_key, cycle_seconds)`,
`TodayOrderReader.read(account_id, client_id, fetch_page, fresh_seconds=2.0)`,
`TerminalProofStore(session_factory)`, and `terminal_version(account_id,
client_id, body)`. WEBULL429 owns their order semantics. Do not feed balance
responses into the order cache/proof store, or use list/detail proof as funds.

`read` returns a fresh exact-client positive row plus `_webull_list_evidence`,
or `None` for UNKNOWN, and raises on HTTP/malformed traversal failure. It never
returns affirmative absence. Metadata binds real account/client, ET session,
original page acquisition time, page count, completeness, cursor, and truncation.
Each row has2s freshness; a logical20-page scan is NOT one HTTP request. Fair
continuation allows at most2 page attempts/account/current scheduler cycle.

WEBULL429 ordinary terminal detail confirmation has one process-local concurrent
budgeted attempt per normalized execution version. Failed429/budget denial is
not completed proof and can retry later. Successful proof uses deterministic
durable identity and survives crash retry. Strict RPG/EOD/cancel readback never
consumes this cached ordinary proof. First valid FILLED child still returns
without querying its sibling; EOD still freshly reads BOTH children.

WBPOWER1 owns its once-per-placement memo/cache and concurrency coalescing,
binding real account plus durable placement/client identity and relevant wire
version and currency. Pure cache hits consume zero permits; each actual fetch consumes one.
Do not silently copy the2s order TTL into a power policy. WBPOWER1 now reports
human acceptance of2s freshness and Webull-only skip on unknown/stale, with
Schwab independent. Exact returned
day-buying-power field, available-funds meaning, freshness, unreadable/budget
policy, and MARKET-without-wire-price handling must be resolved from its card,
SDK evidence, or the human parent. Missing retained power evidence remains
UNMEASURED. No new trade policy follows from this shared-budget contract.

## Composition Receipt

WEBULL429 is rebased only on its own unreviewed branch to authorized main
`4805ddc81184c76b4d5cef5c483c809edb666fe6`. Candidate full pair, all source
mutations, ALL-ON/effective-zero controls, and both exact-head Validate checks
are re-run before a ready receipt. Runtime budget API above is unchanged by
that rebase. Parent owns review pin and C-row; no merge/install is performed.

# Recorded replay input boundaries

The SXTC/DKI replay is a controlled SLOT proof, not actual both-leg placement.

| Input | Provenance / limit |
| --- | --- |
| OHLCV | Read-only strategy_bar_history: SXTC 60 bars, DKI 12 bars; no invented bars |
| ATR flip/state/trail/age | Recorded V2-ATR-PROBE logs joined to actual bars; not recomputed ATR |
| Watch/boot times, restart opportunity and phase | Recorded logs; copied into test state |
| SXTC 16 suppressed bars, live SELL, retired opportunity | Recorded log events; retirement is not proof that durable RPG tickets cleared |
| Reconstructed CW bits/provenance | Test-seeded through actual cap helper; partial prior claim is a counterfactual, old cap-only marker was not logged |
| Position/owner book | Test-injected fresh empty account-neutral book at every probe; not a historical persisted book snapshot |
| Owner recovery control | Injected unknown owner and terminal-unfilled rows, informed by logged retirement; no historical held-position fill asserted |
| Window/liquidity/stop-ask and readiness gates | Controlled test prerequisites; not recorded quotes or historical broker eligibility |
| Persistence, time, transport | In-memory callbacks, recorded probe time, intent drafts only; no OMS submission or broker fills |
| Original replay RPG ticket dictionary | Empty/injected; per-account draft counts cannot establish actual SXTC placement |
| New durable-ticket control | Exact current DB payloads read at 2026-10-07T19:09:28.572621Z, injected unchanged into restored in-memory ticket dictionary; NOT a 13:45 historical snapshot |

The new present-ticket control includes SXTC PRIMARY held_unknown ticket
8258b37f-dfb3-5ebe-911c-93546fa11ca7 and MIRROR expired ticket
262ccac4-4b8c-5fc4-830f-b034aa74671c. Expired alone is not proof of old-order
or replacement clearance. No reconciliation or positive clearance is invented.
SLOT must not force ticket ownership release, even with handoff OFF. Any remaining
durable ownership veto is outside SLOT scope; #1115/RPGSTALE remains parked.

The empty-book/empty-ticket SXTC ON replay produces 3 open + 2 cancel drafts per
account across 19 probes: initial normal rest after 3 short bars and 2 ordinary
reprices. These are hypothetical drafts, not executed buys or actual both-leg
admission. OFF preserves 16 suppressions and zero drafts. DKI has no fresh flip,
retains its reconstructed cap and emits zero drafts; 11 joined probes are used
because the final bar closes outside the bounded log window.

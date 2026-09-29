# Card 10: identical exit rule across sessions

Status: code built on `codex/card10-session-exits`; not deployed. This is a live-money change and
requires independent review, a pinned exact SHA, fresh broker flatness, and a separate after-hours
operator GO. No production flag or service was changed in this build.

## Observed Webull CW_FLIP exits

Read-only production joins of `trade_intents`, `broker_orders`, `fills`, and managed rows:

| Symbol | Sell fill (UTC) | Qty / price | Intent-to-fill | Managed row |
| --- | --- | --- | --- | --- |
| BMGL | 2026-09-14 19:45:08.982 | 1 / 7.135 | 0.303 s | `4dad8f7a-c29b-4a88-9cf5-59da696be605`, closed |
| FTFT | 2026-09-16 16:17:04.197 | 1 / 7.59 | 0.192 s | `7beed3f2-098a-4133-bdef-a2fd49a80754`, closed |
| TOPS | 2026-09-22 13:16:03.932 | 1 / 1.32 | 0.179 s | `1527bc09-daee-41b2-b7da-7f972f5123d1`, closed |

All three filled; none proves the generic path handles a future unfilled or refused limit.

## Behavior

- `CW_TARGET`, `CW_HARD_STOP`, `CW_FLOOR`, and `CW_FLIP` on Webull use the shared
  cancel-then-sell recovery path. Extended-hours unfilled limits remain tracked for retry and
  the 20:00 unsold incident. An intent is not counted as a fill.
- A *confirmed* RTH native bracket still owns target/stop. An unconfirmed Webull bracket can be
  cancelled with readback before a software full close; scale-outs cannot take it back.
- From 16:00 until 19:55 ET, every held row is blocked from a software sell until its own RTH broker
  children are confirmed gone. Schwab rereads its native order tree. Webull cancels its
  deterministic T/S children and then rereads both; a cancel acknowledgement or a Webull
  `confirmed_after_accepted_request` fallback alone is not confirmation. Unknown/missing handles
  and working legs retain the block and open the existing INC1 exit-release incident. Probes are
  paced at 10 seconds. A later confirmation closes the incident and releases only that row.
- At 19:55, the OMS still attempts to cancel and verify the RTH legs, but a still-working or
  unreadable leg does **not** block the flatten sell. The DAY legs cannot execute in extended
  hours. The 19:55 sell uses the existing EH limit and retry-until-filled path. If the broker
  rejects it because shares are committed/unavailable, a separate critical INC1 incident opens
  once per managed row, even if the 16:00 release incident was already delivered. That incident
  is not closed merely by a later leg-release readback; a sell fill still needs verification.
- The floor flag is **not** changed by this PR. The deployment must set
  `MAI_TAI_OMS_V2_CW_FLOOR_EXIT_ENABLED=false`, restart OMS under the exact-SHA GO, and verify
  `/proc` plus effective +5%/-8% parameters. The existing floor-OFF decision ignores a
  previously armed floor; target, hard stop, and flip remain active.

Read-only `/proc/1328348/environ` on 2026-09-29 confirmed current OMS parameters: target
`5.0`, hard stop `8.0`, and floor exit `true`. No environment value was changed.

## Checks and rollout

The new tests were red before implementation: extended-hours Webull target/flip limits were not
tracked (2 failures), and a working EOD leg did not block a software sell (1 failure). Focused
adapter, OMS, handover, overnight, and regression suites passed (347 tests). The final full macOS
unit run was `47 failed, 4,488 passed`; its failures matched the previously reported host/tooling
set (Linux `sha256sum` paths, installer shell tests, and the dead-consumer timing case), with no
Card 10 failures.

After #1064 merged, the 19:55 ruling changed to SELL ANYWAY. The working-leg and committed-share
reject tests were red on the rebased branch before the follow-up edit (2 failures). The focused
handover, overnight, Webull shared-path, and INC1 watcher suite is now 115 passed. Restoring the
inner handover veto made the Schwab and Webull unconfirmed-leg tests fail; removing the incident
write made the reject test fail. The full macOS unit run on this head was `56 failed, 4,638 passed`;
the failures were in host-dependent installer and dead-consumer tests, not the touched exit tests.
An exact failed-name control on fresh `origin/main` has not yet been run, so the full-suite baseline
comparison remains unverified. Ruff and `git diff --check` passed.

Deliberate mutations killed: bypassing the central handover guard made a direct sell place;
removing Webull target/flip shared routing failed all four AM/PM retry tests; accepting a Webull
cancel acknowledgement without terminal readback falsely released the pair. Each mutated source
was restored and the passing tests rerun.

The Webull strict readback method is additive to the router and adapter. Deployment changes OMS
and the installed INC1 watcher; broker adapters are loaded into OMS. The repository watcher
change does **not** update `/home/trader/unexercised_watch/watch.py` by itself. Install the
reviewed copy outside market hours and re-pin **both** root crontab SHA guards against its exact
hash before relying on the new 19:55 page. No v2, strategy, or gateway restart is part of Card
10. Keep `V2_OVERNIGHT_FLATTEN` enabled. Before deploying, require both live
accounts flat, no open managed rows or armed legs, and independent review. First read must verify
the broker child states, exact row identity, exit intent/order/fill distinction, and INC1 page.

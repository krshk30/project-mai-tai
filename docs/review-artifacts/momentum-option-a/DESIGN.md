# Momentum Option A: bounded gateway consumer

Operator ruling, 2026-09-28: supersede draft #1029's whole-market `T.*` relay. The
scheduled overnight replay was cancelled before it started; its frozen 3.5-load
protocol was not relaxed and no PASS/FAIL result exists. This branch is paper-only
and does not authorize a production restart, flag change, or live trading route.

## What changes

- The Momentum service opens no Massive websocket. It consumes the gateway's
  existing five-second whole-market `snapshot_batch` events to detect 20% moves
  over sampled 30/60-second windows. A snapshot sample is never a fill or exit.
- On detection, Momentum publishes `mode=replace` subscriptions under its own
  `momentum-paper` consumer name. Only the gateway's trade ticks for those
  symbols can fill, exit, or extend the paper path. The cap is 16 distinct
  symbols (at most 32 active 30/60 events). Excess proposals are skipped and
  counted; expired candidates release their subscription, with at most one
  second of removal debounce.
- Gateway trade ticks now carry the SDK's condition codes and an explicit
  provenance bit. A trade tick from an older gateway, without that bit, is
  refused rather than treated as an eligible unconditioned print. The gateway
  also durably records each consumer's symbols and applied stream ID. After a
  restart it restores all owners and replays updates newer than the checkpoint.
  A Momentum replace/remove never alters the scanner's or v2's owner set.

This changes the studied signal population: snapshot-sampled 30/60 moves can
miss brief trade-only spikes or nominate different candidates from `T.*`.
Snapshot sample counts and zero synthetic share volume must not be presented as
raw tape counts. We will judge Option A on its own forward paper sessions, not
claim parity with the old detector or reuse #1029's replay verdict.

## Boundaries and gates

- No v2, strategy-engine, OMS, order, broker, gateway snapshot cadence, or live
  flag/env code is changed. Gateway changes are limited to additive trade
  metadata and subscription-owner recovery, but the gateway is shared live
  infrastructure and needs independent review and a separate exact-SHA GO.
- Before that GO, inspect the retained subscription stream on the box and prove
  its first-restart migration contains the active scanner and v2 owner events.
  If either owner cannot be reconstructed, do not restart the gateway; obtain
  fresh replace events or an independently reviewed migration plan first.
- Deploy only after review/pin and the operator's exact-SHA approval for the
  momentum-paper and gateway restarts. Confirm both owner sets survive a
  gateway restart, the union never loses a scanner/v2 symbol, Momentum has no
  second Massive connection, trade condition provenance is present, and the
  paper service has no broker route. A missing condition-provenance bit keeps
  paper results degraded, not silently gradable.
- After deployment, run a 20-minute one-second load census at 16:05 ET and
  compare with a no-Momentum control. The frozen 3.5 guard remains; an abort is
  unmeasured, not a PASS. Do not run another whole-market replay.

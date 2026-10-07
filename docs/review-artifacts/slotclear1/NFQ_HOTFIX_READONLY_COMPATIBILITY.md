# Read-only composition assessment

SLOT source/test freeze: `e246cc144d4efa86ec4ddbd60ddb5a9de6ade368`, tree
`f046e51cdb2081f2e9ff82beda215a4e5c5e984f`.
Common ancestor with current-main `994f08ae` and own NFQ+HOTFIX runtime is
`f9c9bd332392e2c905fc39b954421c88970844d7`. No integration, merge, cherry-pick,
rebase, or source edit was performed in the NFQ proof checkout during this follow-up.

Read-only checkout:
`/Users/velkris/.codex/worktrees/nfq2-hotfix1-activehold-proof/project-mai-tai`
Runtime freeze `494bbeefdc0cd392747f528010c4f73043d2477b`; receipts head
`e4578d44c8e48a1bbbe0f8b0185ae05908bafb1c`; status remains clean.
Original NFQ branch/worktree remains unmodified.

## Compatibility boundaries

- Fresh SELL writes only reconstruction/slot state after flat ownership proof;
  NFQ2's `held_no_fresh_quote` sets `resting_active`, which vetoes this release.
  KEEP-REST cancellation remains owned by the existing cancel producer. NFQ2's
  separate EH-hold cancellation code must remain in the joint tree.
- Fresh BUY still emits `slotclear_first=true`. The SLOT OMS addition caps that
  at 0.5% unless `_nfq2_held_dispatch(event)` proves serial held authority.
  NFQ2 independently changes the same reactive cap area to 1% ONLY for serial
  held dispatch. This shared pricing hunk requires deliberate parent integration
  and a combined fresh/held/forged-authority test, not blind source replacement.
- NFQ2 changes shared strategy identity/feedback/cancel methods and Settings;
  HOTFIX changes OMS quote drift/cache handling. The new SELL service-cap stamp
  does not alter either quote-handler hot path. This is source assessment,
  not a rerun or recertification of combined performance.
- Catalog counts cannot be copied wholesale. Standalone SLOT has 149 boolean
  process checks +8 numeric =157. Existing NFQ+HOTFIX proof has 149 boolean +8
  numeric =157, but different flag sets. A straightforward union would have
  151 boolean +8 numeric =159; this is a derived expectation, NOT an executed
  joint-tree audit. Shared catalog/all-on/KEEP-REST/MIRROR test-count changes
  require reconciliation on the eventual integrated tree.

The accepted NFQ benchmark used full quote handling, real enabled HOTFIX drift,
one active hold, 14,400 events /60 seconds near 240/s, no tick SQL and explicit
off-loop periodic SQL. Three retained pass receipts and earlier failures remain
in that worktree. No live broker/OMS performance or all-gap readiness is claimed.
This fresh-SELL follow-up ran no additional NFQ/HOTFIX sustained performance
benchmark. Existing unrelated timing tests are included in the full-unit pair;
their results are not a fresh-SELL or OMS performance certification.

Remaining gates: reviewed current-main/joint exact tree; joint focused/full-unit
and serial held-authority controls; target-platform validation and separate
install GO. No #1115/RPGSTALE integration is implied or authorized.

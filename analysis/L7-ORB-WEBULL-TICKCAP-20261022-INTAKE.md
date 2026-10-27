# L7-ORB-WEBULL-TICKCAP-20261022

- Source: human additive assignment; Claude TOP 2026-10-22 09:29:25.
- Priority/owner: L7 source-only, sole assigned lane author; main owns qualification.
- Base: 55204356807fd9f081d14a235abafce888a8838a.
- Worktree: /private/tmp/mai-tai-l7-orb-webull-tickcap-20261022.
- Branch: codex/l7-orb-webull-tickcap-20261022.
- Status: source assessment; no runtime or qualification claimed.
- Next action: reuse provider-aware ORBPASTLEVEL for Webull opens, author controls,
  commit and hand source evidence to main.
- Completion: source review plus main-owned actual selected tests/qualification;
  authoring alone does not complete incident acceptance or authorize live replay.
- Scope: OMS ORB selector/callsite and new tests/handoff only. No actor, runtime,
  UI, operations, broker implementation, POM, shared queue/status/LATEST edits.
- Assessment: Webull bypasses selection; legacy 1.65 cap equals 1.65 stop and
  existing adapter correctly refuses before transport. Do not weaken that guard.
- Geometry: Webull retains the legacy floor nominal cap where legal, lifting only
  to at least one legal tick above its ceiling-rounded trigger. This preserves
  existing healthy caps (including levels >=10) without a price-threshold branch.
  Schwab keeps its existing ceiling nominal cap and equality-to-LIMIT selection.
- Controls: STAK incident-faithful synthetic input, provider ticks, fresh/stale/
  absent quotes, cap refusals, fences, real OMS callsite, fake SDK wire, EH/RTH,
  unchanged exits/ATR/Schwab/defaults/higher-price geometry. All author UNRUN.
- POM L4: copy rehearsal completed by main; source frozen, no rerun/live authority.

# Galileo coordination: scoped clocks, no global monotonic patch

For Main to relay to Galileo; no direct agent message has been sent.
Exact inspected APP: 4f0150c188e92063ebfb160b6437a4ab5d432498.

The APP already installs an instance port in SchwabV2BotService.__init__:
    self._keepwatch_boot_retry_clock = time.monotonic
_keepwatch_boot_retry_quiescent consumes that port for elapsed per-symbol
deferral. Twelve intervals represent 60 elapsed seconds, not twelve callbacks.
Its 180000ms pending-age check separately uses strategy._now_ms() and bar time.

Proposed controlled recorded-boot interface, awaiting Main/Galileo source review:
bind ONLY this service instance's _keepwatch_boot_retry_clock to an explicit
elapsed control-step clock for the finite modeled state-publish lifetime.
Start at zero per fresh service/arm; advance by actual modeled step deltas
including gaps, never by number of callbacks. Restore the exact prior callable
on success, cancellation and failure. Record origin, step stamps and elapsed
values in the boot receipt. No assignment to pending work, exclusions, readiness
or deferral counters. Actual APP decides exclusions/retry and publishes reasons.
This is a proposal, NOT an implemented or qualified clock adapter in this head.

Do not patch time.monotonic, module.time, asyncio loop time, sleep, wait_for,
provider/DB deadlines, NonceCopyLease._WALL_NS or expiry clocks. Existing
l12_boot_owner_clock.AsyncioPort.wait_for delegates all non-owner waits to real
asyncio.wait_for; preserve that boundary. Market/session model clock and scanner
alert_engine.now_provider retain their explicit BOOTONLY mapping; durable row
times and native headers stay original. No retimed native freshness claim.

Current l12_recorded_boot_driver.PublishControl calls actual holdcheck once in
phase zero after membership/history/owners settle and line publication, not from
observation. It deliberately reports full publisher UNEXERCISED and monotonic
warmup timeout UNKNOWN. Neither label is upgraded by the proposed deferral port.
The older _boot_warmup_wait_started_monotonic uses module time.monotonic: do not retime
it through this port or infer its 300000ms boundary was exercised by model time.

For a future controlled clock adapter, require repeated calls at identical time
do not consume elapsed budget; 59.999s defers / 60s excludes only own symbol;
age180000 stays consumable / 180001 stale; real nonowner wait deadlines remain
real; cancellation restores port; two fresh arms do not reuse clock state.
These are proposed new controls, not claims that this source ran them.

For actual34 C11229 classification, a CONTROLLED injected read error is NOT an
actual retained-input exclusion witness. Preserve real-UNKNOWN provenance,
noentry, local block and rest-fleet release; unjustified block remains FAIL.

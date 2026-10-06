# Complete Pre-Write Read-Only Rehearsal, October5

BEGIN20:44:07.946 ET; COMPLETE20:45:23.336 ET, rc0. Application7823a6fa,
box e1ce3b39 clean, unchanged identities. Execution was remote root Python
stdin, bytecode off; new helper compiled in memory, unchanged read helpers
loaded from prior reviewed job. No remote file staged/written, no git fetch,
token refresh, service action or DB/Redis mutation by this rehearsal.

Exact helper SHA256:
658ca1c0ed85c18b9f898643dc84fc49ac4826ffa06f38c8a471f9e0f8565670.
Full466235-byte raw receipt:/tmp/oct5-2040-complete-rehearsal.log.
SHA25684ea738e750963e0d15e9544c39aaf5827d069c18640c21ec5eee61535556c7e.
The full raw receipt is preserved, not reconstructed from truncated tool text.

| Read gate | Real-time result |
|---|---|
| Native clock, box HEAD and clean tree | rc0; native UTC, e1ce3b39 clean |
| General OMS with fresh direct positions and strict books | rc0 first attempt, exact MI/NXL and off-hours audits printed |
| General strategy with fresh direct positions and strict books | rc0 first attempt; no remaining general failures |
| Reviewed all-date ticket census and serial broker detail GETs | rc0;14 reviewed tickets,11 linked parents terminal; no unreadable/retry |
| Redis bounded checkpoint |0 evictions,806932144B; all5 consumers+marker1/all9 streams |
| preflight_oms_restart.sh --require-all-account-positions-flat | rc0, both accounts flat and zero managed rows |
| Fresh strict-flat before v2 fence | rc0 first attempt |
| preflight_v2_restart.sh | rc0, no override,0 armed,0 managed, fresh brokers |
| Full identity/env/log/preopen/schema capture | clean e1ce, revision0021,0 binding columns;10 unit states recorded; paper PID0 observed |
| Box ancestor/remote main/target ancestor/docs-only ahead | all rc0; main7823a6fa, no ahead paths; ls-remote only, no fetch |
| Application whole tree/blob checks and pinned preopen | SOURCE_BLOBS PASS; preopen2ef22340; no install/write |
| Official restart-evidence snapshot collector/parser | PASS10/10 services;2/2 accounts flat,0 managed;0021; UTC systemd strings parse. Output destination replaced ONLY by an in-memory sink, never a file |
| Import path | rc0 /home/trader/project-mai-tai/src/project_mai_tai/__init__.py |
| Final fresh strict-flat | rc0 first attempt |
| Final Redis/owner/stream and all identity comparison |0 evictions,806954936B; exact owner sets/streams/identities unchanged |

Rehearsal code:job/rehearsal_readonly.py. Original collector executes with
the same runner; its mkdir/write_text target is an explicit in-memory sink.
Approval/file manifests and flock/exclusive attempt creation run at execution,
not pretended PASS before release staging. Post-install gates, migration,
newPIDs/proc/ticket dispositions/scanner/bar proofs are NOT run early.

Example measured audit (not the reviewer's numbers):
```text
[STANDING-ALLOWANCE] service=schwab-1m-v2 effective_status=degraded observed_at=2026-10-06T00:44:12.275531+00:00 heartbeat_max_age_seconds=120 data_flow=stalled_offhours_rest_dry market_session=closed loop_health=healthy loop_exceptions_total=0 streamer_connected=true enabled=true warmed_size=5 watchlist_size=5 secs_since_last_bar=2650 seconds_since_20_et=2666.635201 bar_age_limit_seconds=2966.635201 admission=in_memory_only
READ_ONLY_REHEARSAL_COMPLETE 2026-10-06T00:45:23.335987+00:00 all_pre_write_read_gates=PASS approval_and_post_install_gates=execution_only no_remote_files_or_services_changed
```

## Tests And Mutations

121 PASS =53 existing standing-allowance tests +29 measured off-hours tests
+39 runner/census/actions/timezone tests. No application code changed.
Ruff and bash-n PASS. Existing MI/NXL18 mutations all assertion-RED,
raw log SHA2561c02134d44c87c3c9a3bf2071eaac5f4c3513f64bc617c8b81f7b5ba28bb32d3.

New mutations, independently applied to the in-memory admission function:

| Removed guard | Result |
|---|---|
| data_flow | RED |
| nonregular known session | RED |
| loop_health | RED |
| zero loop exceptions | RED |
| streamer connected | RED |
| enabled | RED |
| warmed equals watchlist | RED |
| positive watchlist | RED |
| fresh/nonfuture heartbeat | RED |
| nonnegative bar-age end-of-session bound | RED |
| raw degraded status | RED |
| in-memory copy only | RED |

Mutation raw log:/tmp/oct5-v2-offhours-mutations.log,
SHA2568916eafea8218739c57e4482d09260a42f10e87afb789c79b1545833426f7707.
The final GREEN baseline ran before this final mutation run; early fixture
name errors were fixed, not used as mutation evidence.

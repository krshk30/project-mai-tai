# WBREAD1: read-only position-read scenario sweep

Status: evidence for draft PR #1063 only. No production code, configuration, watcher, or
service was changed. The box checkout read on 2026-09-29 was `3141bbd5`; the OMS was still
PID 1328348, started 2026-09-28 22:39:55 UTC with zero systemd restarts. WBREAD1 is not live.

## Window and coverage

The requested window is the last 30 **completed** ET trading sessions, 2026-08-17 through
2026-09-28, with each session measured from 04:00 to 20:00 ET. Labor Day 09-07 and weekends
are excluded from this denominator; they are not evidence that weekend syncs are safe.
Application log rotation retains only 20 of the 30 sessions (08-31 through 09-28). The ten
sessions 08-17 through 08-28 are **UNKNOWN**, not zero: no older `oms.log*` was present under
`/var/log/project-mai-tai/` or `/home/trader/`, and the OMS systemd journal has no application
matches for those dates. Thus a complete 30-session event rate cannot be calculated.

Raw paths: `/var/log/project-mai-tai/oms.log-20260901.gz` through
`/var/log/project-mai-tai/oms.log-20260928.gz`,
`/var/log/project-mai-tai/oms.log-20260929`, and the 20:00 ET terminal census in
`/var/log/project-mai-tai/oms.log`. The source for the code comparison is production service
SHA `8f69a08a`; the checkout SHA is later ops-only and does not imply a service restart.

## Observed counts in the retained 20 sessions

| Surface | Denominator | Connection reset | Timeout | Bad body | Incomplete pagination | 429 | Other |
| --- | ---: | ---: | ---: | --- | --- | ---: | ---: |
| Webull adapter `live:orb` error lines | 2 logged errors; 77,328 census-declared successful sync reads, 0 declared failed | 1 | 1 | UNKNOWN | UNKNOWN | 0 logged | 0 logged |
| Webull `/account/positions` endpoint, instrumented 09-21 onward | 19,888 page requests: 19,887 success, 1 failure | 0 in this subwindow | 1 | UNKNOWN | UNKNOWN | 0 logged | 0 logged |
| Schwab adapter `live:schwab_1m_v2` warning lines | 610 logged warnings; sync census 606 failed / 76,486 total reads | 5 | 8 | UNKNOWN | Not applicable to this endpoint log | 7 | 590, of which 589 contain 401 authorization text |

The Webull endpoint is counted once per minute using the last cumulative
`[WEBULL-ENDPOINT-CALLS]` row; the 09-24 20:35 UTC minute logged twice, so summing rows would
falsely count two failures. Its `partial=1` is a partial **minute summary**, not proof of a
partial pagination response. Endpoint instrumentation starts only on 09-21 and counts page
requests, not complete adapter reads. The 5-minute sync census is a different denominator from
adapter warnings; bucket boundaries and non-sync callers prevent attributing the four-count
difference. None of these
numbers should be divided across unlike surfaces.

The two Webull failures are:

- 2026-08-31 12:20:10 ET, connection reset,
  `/var/log/project-mai-tai/oms.log-20260901.gz`.
- 2026-09-24 16:35:24 ET, SDK read timeout,
  `/var/log/project-mai-tai/oms.log-20260925.gz`.

In both intervals the `live:orb` sync census reported `failed=0`. The production adapter's
non-429 exception branch logged the error and returned `[]`; the sync loop treats an empty
list as a successful flat read. This is a **real code-path defect**, not proof that a held
share was actually zeroed on either date. Historical held quantity and a per-call correlation
ID are unavailable, so realized position harm is UNKNOWN. The old `_positions_blocking` also
used `break` then returned an empty or partial list on an unreadable body, missing holdings,
or an incomplete page. Those shapes were silent; their observed frequency is UNKNOWN, not 0.

Schwab's position adapter raises on request errors and non-dict top-level responses. Its
unreadable sync account is excluded rather than zeroed. The 401-heavy warning count is a
separate broker authorization episode, not a WBREAD1 bad-body count.

## Caller and safe-ending check

- `sync_broker_positions` calls `list_account_positions` per account and excludes a raised
  account from both position zeroing and virtual-position clearing. Its five-minute census
  observes this path, but cannot reveal a Webull error that returned `[]`.
- `_broker_symbol_position_state` makes a fresh read for flat/held reconciliation. A raised
  read becomes UNKNOWN and preserves protection. `[RECONCILE-READ]` is observable, but no
  per-call adapter ID ties it to a historical Webull error.
- `_refresh_broker_position_quantity` reads for a sell quantity and returns `None` on an
  exception, without syncing a zero. Its callers are the ordinary sell recheck and the
  rejected-stop market fallback. Both issue `broker_position_unreadable`, not
  `no_broker_position_available_to_sell`; the strategy's no-position predicates do not
  classify the UNKNOWN code as flat. Historical caller-specific read denominators are not
  logged, so their live event rates remain UNKNOWN.

The WBREAD1 tests pin connection errors, malformed bodies, and failed later pages to a typed
unavailable result. The 429 cached-snapshot path is unchanged. This sweep adds evidence but
does not authorize merging or deploying #1063 or installing #1059. Independent review and an
exact-SHA operator GO for the live OMS remain required.

# Session Handoff — CURRENT STATE (read this first)

> **OVERWRITE this file.** It answers: *what is true right now?* Historical narrative belongs in
> [`handoff-log.md`](handoff-log.md). Numbers without an as-of time are not current-state evidence.
> The 10-16 state (Group D install) is in git history of this file (main 54549912).

## PRODUCTION NOW — as of Tue 2026-10-20 23:00 ET (claude-1, install verified; operator asleep from ~20:30, authorized "complete all the tasks")

| Item | State now | Evidence (as-of) |
|---|---|---|
| Box checkout | **`a8239959`** (detached; src byte-equal to reviewed fef624dd) = b79ef160 + F minus Fix A: WORX L7, WBATTACH3/AIXI final wire, PAGE1020 ATR partial ledger, momentum Fix B, MOMORDER1 L1–L8, L6 phase-1 continue, Q05/O13, **OWNMIX2** (only own books count; manual shares never block), **SAFEX1** (R5 ORB close = min(broker, own) on every path + incident; R7 mandatory momentum exits never stall on UNKNOWN read; R4 own=0/broker>0 incident) | `git log -1` 22:57 |
| Held | **Fix A** (momentum exit-time read-back) — swallowed exception on the entry path (BBCI); FIXA-2 lane released 23:00 | LATEST 18:36 / 23:00 |
| Installed tonight | 22:57:51 ET; env file unchanged since 10-17 (F adds no flags); 8/8 services active, NRestarts 0 | `systemctl` 22:59 |
| Boot proof | `MOMEXIT-B-RESTORE-GUARD … entry_admission=flat_terminal`; 0 ERROR/CRITICAL since boot (all services); 0 Webull ServerException | logs 22:59 |
| Records | SQL repairs committed (dry-run then COMMIT, stopped window 22:47–22:57): AIXI external 95 @ 2.9701 (managed 8c09507e closed, 6 incidents closed, −16.14); SAIQ external 60 @ 4.88 (intent 04b27ec5 filled, −8.40); CRMT exit time | /var/tmp/c1sw receipts |
| Pages | ATR **+$16.97** CLEAN (6 closed, 12 fills); momentum READABLE **+$15.68** (4); ORB 0 | fullscan 22:58 |
| Positions / orders | flat both brokers; 0 working orders (native read) | 22:47 |
| Backups | `/var/backups/project-mai-tai/install-20261020-F/` b79 tree tgz 13d6f6fb… + full DB dump ce8e2fc3… | 22:57 |
| Rollback | stop services → `git checkout --detach b79ef160` → start | — |

## TOMORROW (Wed 10-21) — first checks

| Time ET | Check | Pass looks like |
|---|---|---|
| 03:40–04:00 | momentum guard + paper start; restore guard still `flat_terminal` | both active |
| 06:20 | preopen | **RED expected** (stale pins/identities for a8239959, O-1019-2) unless re-baselined tonight |
| 07:00+ | scanner, `#ERROR SIGNATURES`, fix ledger; watch `own_position_evidence_UNKNOWN`, `query budget unavailable`, `OMS-ORB-CLOSE-BROKER-BELOW-OWN`, `SCHWAB-FALLBACK-OWN-ZERO-BROKER-NONZERO`, `SAFEX1-R7-CANCEL-READ-UNKNOWN`, `cancel_publication_waiting` >15 min, v2 callback lateness | 0 / explained |

Open cards 10-21: FIXA-2, DCARRY1 (pre-existing owner-bound after deferred cancel), V2FF-LOOP cause, SAIQ cancel-receipt, inspect_episode single snapshot, PERF-HANDLER1 (+8–10 ms ATR callback tail), G2 receipt limit vs 15–17 s sync cadence, R8 Webull re-arm after exhausted retries, native (a)/(b) coverage gaps, EXTCLOSE1, L7b.

---

## (previous) PRODUCTION NOW — as of Mon 2026-10-19 20:4x ET (claude-1 close-out read of the box)

| Item | State now | Evidence (as-of) |
|---|---|---|
| Box checkout | **`b79ef160`** = installed `9df73a18` + Track A only: **Q** (O-1019-15 Webull unreadable-ACK never blocks the software stop/exit; list-proof exits; C6 original-pair proof; kill-switch inventory) + **O17** (control-plane survives one malformed DB row) | `git rev-parse` 20:0x; tree clean |
| Installed tonight | 19:58 ET Track A, operator-approved ("go ahead with the install") **despite node-8 ORB rate absolute FAIL**, which fails identically on installed 9df (O-1019-25) | install receipts `analysis/` |
| Services | all 8 restarted 19:58 ET; OMS restarted again 20:3x ET (after O-1019-26). Momentum guard + paper inactive since the 18:10 scheduled close → **Tue 03:40 ET timer** | `systemctl` 20:3x |
| Guard | manifest `gateway_identity` re-pinned to market-data pid 408893 / start 23:58:07Z (backup `install-20261019-trackA/manifest.json.before-repin`); `daily_guard.py --dry-run-prestart` **PASS** 00:35Z; no safety-stop file | 20:35 |
| Live momentum | boots clean (0 `MOMLIVE1-BOOT-REFUSED`) after O-1019-26 guarded settlement of WORX episode f723 (claimed → terminal_no_fill; codex main+Q+R AGREE 7fa2da28) | 20:3x |
| Records | YMAT #2 external close booked (operator manual sell 165 @ 1.78 15:53 ET; guarded repair 2684e9c0, dry run then commit); 4 YMAT incidents closed | 19:38 |
| Pages | ATR page closed=3, day **+$24.33** = broker; momentum live −$19.34; PAGES CLEAN | 20:0x |
| Positions / orders | flat both brokers; 0 working orders; 0 open managed rows | 20:3x |
| Backups | DB dump `install-20261019-trackA/project_mai_tai.dump` sha256 3caf4487…; tree 9df tgz f28dd562… | 19:26 |

## TOMORROW (Tue 10-20) — first checks, in order

| Time ET | Check | Pass looks like |
|---|---|---|
| 03:47 | momentum guard + paper start | both active, `live_handoff=True`, no safety-stop file |
| 04:05 | v2 session roll: prior-session `v2_removed_wait` rows (WORX ec6d28d9 retry_exhausted, EXYN bccca82e) | cleared / not gating; the scan's "WAITING BUY PER WATCHED STOCK" shows no `<<< NO WAITING BUY` from a 10-19 row |
| 06:20 | preopen | known RED until O-1019-2 re-baseline lands (not a trading block) |
| 07:00→ | full scan every 30 min + momentum watch every 10 min | first ATR Webull fill: pair attaches or Q list-proof software exit; no `WBATTACH1-BLOCKED` without an "Exit waiting for broker proof" incident |

## TO BE EXERCISED (installed tonight)

| Fix | First live event | Proof |
|---|---|---|
| Q (O-1019-15) | first ATR Webull bare fill | no pair cancel on unreadable ACK (`attach_ack_unknown_requires_child_proof` incident instead); any stop/flip/hard-stop exit runs after a complete TODAY-list proof; never a blind sell |
| O17 | any malformed DB row | page banner + `[CONTROL-PLANE-ROW-SKIPPED]`, rest of the page intact |

## OPEN — Track B (tomorrow night; NOT installed)

| ID | Item | Why not tonight |
|---|---|---|
| O-1019-18 | fresh-sell owner release counts an `aborted` buy as working (WORX blocked 14:18→) — root cause confirmed by claude-1 + codex | in full set with 63 NEW failures |
| O-1019-16 | OMS admission refusal never reaches v2 (ghost rest) + momentum unsent order holds shared account | same |
| O-1019-26 class | momentum ledger keeps a claimed unsent episode active (live release, J) | same; tonight settled by data fix |
| MOMNEG / N2 | down-day filter; re-price within +1.5% | VPS speed FAIL (93–162 ms / 129–170 ms max handler) |
| MOMBOOT1, COUNTRYUS2, PAPEREXIT5, M (AAME), M2 (restart orphan) | reviewed | carried with the full set |
| O-1019-22 | 63 NEW full-suite failures on 1c245c0e | triage per lane |
| O-1019-23 | guard exits 2 at scheduled close → 2 false pages | policy gap proven; cause of owner-release UNKNOWN open |
| O-1019-24 / -25 | gateway A 60 s (~100 ms) and ORB aggregate (~90 ms) loop stalls on the box — fail on installed 9df too | diagnose |

## Today's record (10-19)

- ATR +$24.33 (YMAT +0.03, WORX +29.25, YMAT #2 −4.95); momentum live −19.34; ORB 0.
- O-1019-15 happened twice (YMAT 11:36, 15:33): pair cancelled on unreadable ACK, software exits blocked → flattened by claude-1 (12:07) and by the operator (15:53).
- Operator rulings: no rollback, fix forward; every root cause validated by codex; scan lists a waiting buy per watched stock; no in-session restarts as band-aids.
- MOMEXIT2 replay: trails/splits worse (90% of +5% touches fall back within 1 s); "take the bid if ≥ +5% at the touch" best (+0.5 pp mean). `analysis/MOMEXIT2-REPLAY-RESULTS-20261019.md`.

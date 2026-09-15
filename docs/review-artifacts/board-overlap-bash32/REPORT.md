# `overlap.sh` on macOS bash 3.2 — replace `mapfile` with a read loop

Date: 2026-09-15 · Author: claude-1 · Carried item from #971 (owner claude-1)

## Defect

`~/.claude/mai-tai-fleet/overlap.sh:70` used `mapfile -t L < <(held)`. `mapfile` is a bash 4
builtin; Apple ships **bash 3.2.57**. On this Mac the real-overlap gate died with
`L: unbound variable` (exit 1, fail-closed) on every run, so `./board.sh overlap` has been
unusable here since the toolkit landed. Recorded 2026-09-09, 2026-09-13, boarded by #971.

## Change (one hunk, `overlap-bash32.patch`)

```bash
L=()
while IFS= read -r l; do [ -n "$l" ] && L+=("$l"); done < <(held)
```

`L+=()` and `"${!L[@]}"` exist in bash 3.2. Nothing else in the file changes; the three matching
rules and `--candidate` are untouched.

## Proof — isolated copy of the live toolkit, `/bin/bash` 3.2.57, `MAI_TAI_REPO` = this repo

| case | held claims | patched copy | unpatched live copy (control) |
|---|---|---|---|
| A | none | `(no held claims)`, exit 0 | `L: unbound variable`, exit 1 |
| B | `src/project_mai_tai/oms/service.py` (claude-1) vs `src/project_mai_tai/oms/*` (codex-2) — rule 1, real tracked file | `⛔ OVERLAP claude-1 [...service.py] x codex-2 [...oms/*]`, exit 1 | `L: unbound variable`, exit 1 |
| C | `.../oms/service.py` vs `ops/health/v2_restart_evidence.py` | `✅ no two held claims collide`, exit 0 | — |
| D | `--candidate codex-2 .../oms/service.py` against claude-1's held claim | `⛔ OVERLAP — collides with a claim HELD BY claude-1`, exit 1 | (this branch never used `mapfile`) |
| E | identical literal path held by both agents — rule 2 | `⛔ OVERLAP`, exit 1 | `L: unbound variable`, exit 1 |
| F | three held, one colliding pair plus one disjoint | lists 3, reports the one pair, exit 1 | — |

⛔ My first run of case B used a path that does not exist in the repo and read `✅ no two held
claims collide`. That was a false clean of MY FIXTURE, not of the rule: rule 1 needs a tracked
file. Re-run with the real path (`git ls-files | grep oms/service.py`) → OVERLAP. Kept here so the
next person does not repeat it.

## Not done here

- The live toolkit is NOT edited. It is checksum-pinned (`.checksums` line 7, live sha in
  `SHA256.txt`). Applying the patch turns `./checksums.sh verify` RED until re-pinned, and the
  rule is *review and re-pin, never re-pin to silence it*. Apply + re-pin after this review.
- `selftest.sh` section F (claim-time overlap) was not run against the patched copy; it needs
  `MAI_TAI_REPO` and the full overlay from `MACOS.md`. The six cases above exercise every branch
  of `overlap.sh` that the hunk feeds.

## Apply (after review, on the Mac)

```bash
cd ~/.claude/mai-tai-fleet
./checksums.sh verify                           # GREEN before
patch -p0 overlap.sh < ~/Projects/project-mai-tai/docs/review-artifacts/board-overlap-bash32/overlap-bash32.patch
shasum -a 256 overlap.sh                        # must equal the `patched/` line in SHA256.txt
MAI_TAI_REPO=~/Projects/project-mai-tai ./board.sh overlap   # runs, no `mapfile` error
./checksums.sh record                           # re-pin, THEN verify GREEN
```

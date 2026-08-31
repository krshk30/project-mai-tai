# Fleet toolkit macOS compatibility report

Date: 2026-08-30

## Outcome

The isolated macOS patch is green under Apple Bash 3.2 and native system tools:

```text
120 passed, 0 failed
every guard refused its known-bad input, and every control still worked
```

The live toolkit at `/Users/velkris/.claude/mai-tai-fleet` was not edited. Its
`.checksums` SHA-256 remains
`74822d944fcc055f4d05b2a38227c74983e81864c7dada43c3ef2a38de95d6e3`, identical
to the untouched snapshot and isolated patched copy. `.checksums` was not re-recorded.
Its entries contain absolute live-toolkit paths, so `checksums.sh verify` from the isolated copy
correctly verifies the untouched live files; it does not bless or pin the patched files.

The patch is staged for independent review only. It has not been promoted to the live toolkit.

## Controlled environment

The valid native-BSD run used a private tool overlay at
`/tmp/fleet-macos-tools.ThHBTz` containing only these links:

```text
gh      -> /opt/homebrew/bin/gh       (2.98.0)
python3 -> /opt/homebrew/bin/python3  (3.10.8)
```

The test PATH was exactly:

```text
/tmp/fleet-macos-tools.ThHBTz:/usr/bin:/bin:/usr/sbin:/sbin
```

`git`, `awk`, `sed`, `stat`, and `date` resolved to Apple system paths. No Homebrew GNU
coreutils directory was exposed. `/sbin/sha256sum` is Apple-signed
(`com.apple.sha256sum`), not a Homebrew GNU binary. Exact BSD controls separately forced
`/usr/bin/shasum`, `/sbin/md5`, `/usr/bin/stat`, and `/bin/date`; exact GNU-interface
controls used local stubs that delegated to those Apple tools.

## Counts

| Platform / state | Passed | Failed | Interpretation |
|---|---:|---:|---|
| macOS untouched, system-only PATH | 69 | 33 | Original requested reproduction; missing `gh` is mixed with portability failures |
| macOS interim patch, system-only PATH | 75 | 30 | Requested checkpoint; still a failed gate |
| macOS untouched, valid dependency overlay | 76 | 26 | Untouched snapshot measured with dependencies present |
| macOS interim patch, valid dependency overlay | 83 | 22 | Valid starting point for this repair pass |
| macOS final isolated patch, valid dependency overlay | 120 | 0 | Green; includes 15 new portability/CRLF controls |
| Windows Git Bash untouched toolkit | 105 | 0 | Current evidence supplied by the user |
| Windows Git Bash exact patched toolkit | UNMEASURED | UNMEASURED | Must run separately after transferring this exact patch |

The later deliberate no-overlay rerun produced 74/31 because the live-PR control for stacked
PR #772 also lost `gh`. That extra dependency failure is evidence that a missing dependency must
not be classified as a BSD/GNU defect; the valid-overlay counts are the compatibility result.

## Captured 75/30 labels by root cause

### Apple Bash 3.2 lacks associative arrays (3)

- `a non-repo directory says COULD_NOT_TELL, not clean`
- `a ref that does not exist says COULD_NOT_TELL`
- `CONTROL: a known-clean pair still reads CLEAN`

### Required dependency missing from system-only PATH (8)

- `CONTROL: the OTHER agent with a real verdict is accepted`
- `check passes while the head is unchanged`
- `TOCTOU merge`
- `short sha`
- `merge cmd lacks --repo`
- `bad repo slug`
- `wrong basis`
- `CONTROL: the non-author can still review it`

### Review fixtures emitted pre-JSON raw values (11)

- `...and the other agent CAN review that same range`
- `a full-PR review covers all five #770 commits`
- `merge-cmd works only after every #770 commit is covered`
- `CONTROL: an audit claim cannot block review of commits the claimant did not write`
- `Codex can review a Claude-marked commit`
- `unmarked commit accepted or misclassified`
- `Claude can review the Codex-marked range`
- `complementary independent ranges cover the whole mixed-author PR`
- `ambiguous commit accepted`
- `legacy Codex OID f3ebec984 is reviewable by Claude via explicit attestation`
- `legacy Codex OID 59fc115a4 is reviewable by Claude via explicit attestation`

### Updated-PR fixture was not portable across default branch names (3)

- `CONTROL: the original PR range can be reviewed`
- `a new full review authorises the updated PR despite stale review history`
- `merge-cmd ignores stale history once the current PR is fully covered`

### Apple Bash 3.2 retained the `IFS` assignment on `read` (5)

- `wrong note count`
- `carry notes duplicate`
- `11 path(s) missing from real output`
- `MUTANT C SURVIVED`
- `MUTANT B SURVIVED`

Total: 3 + 8 + 11 + 3 + 5 = 30 failing labels.

## Patch behavior

- `board.sh` now uses parallel indexed arrays, preserving all pairwise matrix behavior on Bash 3.2.
- `portable.sh` covers SHA-256, MD5, BSD/GNU `stat`, BSD/GNU epoch conversion, and Python selection.
- Every exact override is variant-specific, requires an absolute executable path, and fails closed.
- `gh` calls still request JSON; fixture records now emit the same JSON contract as production.
- Carry-note controls restore `IFS`, exercise the real eleven-claim promotion, and kill all three mutants.
- Historical legacy-OID classification is tested without requiring an unreachable object to remain in the clone.
- The updated-PR fixture resets its temporary `main` branch portably instead of assuming the initial branch name.

`promote.sh` gate semantics are preserved. The PR-head manifest and live/pinned manifest still pass
through the same byte stream and SHA-256 comparison; no parsing, field comparison, or canonicalization
was introduced. A mismatch still exits nonzero before rotation, an identical VOID manifest is refused,
carrier/head/main bindings remain fail-closed, and failed moves leave journals intact.

## CRLF proof

The final self-test creates a fresh copy, converts every `*.sh` and `fleet.cmd` to CRLF, and proves a
CRLF shebang cannot execute directly on macOS. It then applies the supported preparation path:

```bash
/usr/bin/perl -pi -e 's/\r$//' ./*.sh ./fleet.cmd
chmod +x ./*.sh
```

The control verifies zero CR bytes remain in shell scripts, every script passes Apple Bash 3.2
`bash -n`, and `freeze.sh status` executes directly through its LF shebang.

## Artifacts

- `fleet-macos-compat.patch`: normalized diff from `live-original-normalized` to `live-copy-normalized`
- `CHANGED_SHA256.txt`: before/after SHA-256 for every changed or added file
- `macos-patched-full.log`: complete 120/0 native-BSD run
- `macos-applied-patch-full.log`: independent apply-to-fresh-copy 120/0 run
- `macos-untouched-valid-path.log`: complete untouched 76/26 valid-overlay run
- `macos-75-30-no-overlay.log`: dependency-missing diagnostic run

Patch SHA-256:
`3071847fa5e843e4edc106fcb6bf5babbe9399f3e1bc07fc6b7c6151ca411adf`

Transfer rehearsal: the patch applied to a fresh untouched normalized copy without offsets; the
result matched `live-copy-normalized` exactly, retained the original `.checksums` bytes, and passed
the complete suite at 120/0.

Remaining macOS failures: none.

Patched Windows Git Bash result: `UNMEASURED`. Do not substitute the untouched 105/0 evidence for a
patched result; run the exact transferred patch independently before any live update or checksum re-record.

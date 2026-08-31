# Fleet toolkit macOS compatibility report

Date: 2026-08-31

## Outcome

The complete isolated patch passes under Apple Bash 3.2 with native BSD tools:

```text
120 passed, 0 failed
every guard refused its known-bad input, and every control still worked
```

The same complete patch applied without offsets to a fresh copy of the untouched normalized
baseline. The applied tree matched the tested corrected tree byte-for-byte and independently
passed `120 passed, 0 failed` under the same controlled environment.

The live toolkit at `/Users/velkris/.claude/mai-tai-fleet` was not edited. Its `.checksums`
SHA-256 remains `74822d944fcc055f4d05b2a38227c74983e81864c7dada43c3ef2a38de95d6e3`,
identical to the untouched snapshot and isolated patched copy. `.checksums` was not re-recorded.
The patch is review-only and has not been installed or promoted.

## Controlled environment

The final native-BSD runs used a private dependency overlay at
`/tmp/fleet-macos-update-tools.6HtvPh` containing only these links:

```text
gh      -> /opt/homebrew/bin/gh
python3 -> /opt/homebrew/bin/python3
```

The test PATH was exactly:

```text
/tmp/fleet-macos-update-tools.6HtvPh:/usr/bin:/bin:/usr/sbin:/sbin
```

`git`, `awk`, `sed`, `stat`, and `date` resolved to Apple system paths. No Homebrew GNU
coreutils directory was exposed. Exact compatibility controls separately forced the BSD and
GNU command interfaces through declared absolute paths and local test stubs. Missing `gh` or
`python3` is classified as a dependency failure, not a BSD/GNU incompatibility.

## Counts

| Platform / state | Passed | Failed | Interpretation |
|---|---:|---:|---|
| macOS untouched, system-only PATH | 69 | 33 | Original requested reproduction; dependency and portability failures are mixed |
| macOS interim patch, system-only PATH | 75 | 30 | Failed checkpoint whose labels were captured and grouped |
| macOS untouched, valid dependency overlay | 76 | 26 | Untouched snapshot with required dependencies present |
| macOS final isolated patch, valid dependency overlay | 120 | 0 | Green native-BSD result |
| macOS fresh application of final patch | 120 | 0 | Green independent apply control; tree matched exactly |
| Windows Git Bash untouched toolkit | 105 | 0 | Earlier user-supplied untouched evidence |
| Windows Git Bash supplied corrected tree | 116 | 0 | User-supplied evidence for the stated reference file hashes, not this final patch |
| Windows Git Bash exact final patch | UNMEASURED | UNMEASURED | No compatibility claim; must be run separately |

The final macOS fixture adds an explicit `/bin/date` fallback required by the native run after
the synthetic counter became deterministic. Therefore its `selftest.sh` differs from the
Windows-tested reference hash supplied with the defect report.

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

## Final harness controls

- The forced-rollover date stub requires `CC_DATE_COUNTER`. Both the mutant and control reset
  the same explicit counter to zero, then the variable is unset after both runs.
- Darwin requires direct execution of an initial CRLF shebang to fail.
- MINGW, MSYS, and CYGWIN prove the fresh copy contains raw CR bytes instead, because Git Bash
  may execute a CRLF shebang.
- Both pre- and post-normalization CR checks use the binary-safe
  `LC_ALL=C tr -cd '\r' | wc -c | awk '{print $1}'` count; unknown platforms fail closed.
- The shared preparation path removes CR bytes, validates every shell script, and proves direct
  LF-shebang execution.
- Exact-path portability overrides remain absolute, executable, implementation-specific, and
  fail closed.

`promote.sh` gate semantics remain unchanged. The PR-head manifest and live/pinned manifest still
pass through the same byte stream and byte-for-byte SHA-256 comparison. No parsing,
canonicalization, or field comparison was introduced; mismatches and unreadable state still fail
closed before rotation.

## Artifact integrity

- Complete patch SHA-256: `cfd6a7f41a94b94e5373d41318a808e3967f07dce05642eef10112f9499f5c84`
- Final `selftest.sh`: `de69906851b9c4a411c45325d7863daa0847e9d70fae320f332cdb33e4e477bf`
- Final `MACOS.md`: `0007701f4410019d99e458085f2e2cce268dd8f4fcb217097b1b828f990eb727`
- Remaining macOS failures: none.
- Final patched Windows result: `UNMEASURED`; no Windows compatibility claim is made.

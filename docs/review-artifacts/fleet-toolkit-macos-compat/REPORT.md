# Fleet toolkit macOS compatibility and safety report

Date: 2026-08-31

## Outcome

The corrected isolated toolkit passes under Apple Bash 3.2 with native BSD tools:

```text
130 passed, 0 failed
every guard refused its known-bad input, and every control still worked
```

The complete patch applied without offsets to a fresh untouched normalized baseline. The applied
tree matched the corrected normalized tree byte-for-byte before execution and independently passed
`130 passed, 0 failed` under the same controlled environment.

The live toolkit at `/Users/velkris/.claude/mai-tai-fleet` was not edited. Its `.checksums`
SHA-256 remains `74822d944fcc055f4d05b2a38227c74983e81864c7dada43c3ef2a38de95d6e3`.
The source snapshot, normalized baseline, corrected isolated tree, and fresh applied tree retain
those exact `.checksums` bytes. No persistent checksum record was re-recorded.

## Controlled environment

Both final runs used Apple Bash 3.2 with this exact PATH:

```text
/tmp/fleet-macos-update-tools.6HtvPh:/usr/bin:/bin:/usr/sbin:/sbin
```

The private overlay contained links only to the installed `gh` and `python3` dependencies. Apple
system `git`, `awk`, `sed`, `stat`, `date`, `cmp`, and `patch` remained authoritative; no Homebrew
GNU coreutils directory was exposed.

## Safety fixes

### Checksum records and diagnostics

- `checksums.sh record` extracts the digest from either hash implementation and writes one
  canonical `hash  path` record through a temporary file.
- Verification accepts BSD/shasum `hash  path` and GNU binary-mode `hash *path` records.
- Malformed records, missing files, and mismatches fail closed.
- Verification prints the promised drift header and failed path before its nonzero exit.
- Controlled pairs prove unchanged and drifted behavior for both BSD and GNU formats.

### Exact manifest bytes

- The PR manifest and live or pinned manifest are materialized into separate temporary files.
  Neither byte stream passes through command substitution.
- `cmp -s` compares those files directly, and SHA-256 diagnostics hash those exact files.
- Exact balanced bytes complete promotion, and the archived manifest must match the verified PR
  file byte-for-byte.
- One added non-newline byte refuses, creates no archive, and leaves the journal unchanged.
- One added terminal newline byte also refuses, creates no archive, and leaves the journal unchanged.
- A generated mutant disables the comparison. The non-newline mismatch then clears a journal,
  proving the missing-guard mutation is distinguished.
- Exit cleanup removes the temporary comparison directory.

### Archived-file fallback

- The fallback locates an archived journal with `find`, then calls `hash_md5` in the parent Bash
  process where the compatibility function is defined.
- The rotation fixture moves the first journal and fails the second move, forcing that path.

## Counts

| Platform / state | Passed | Failed | Interpretation |
|---|---:|---:|---|
| macOS untouched, system-only PATH | 69 | 33 | Original untouched reproduction |
| macOS prior safety patch | 129 | 0 | Superseded; normalized trailing newlines before comparison |
| macOS corrected isolated tree | 130 | 0 | Native BSD full run |
| macOS fresh application of corrected patch | 130 | 0 | Independent full run; exact tree match |
| Windows Git Bash prior head `2b25851a` | 125 | 0 | Real count, but trailing-newline mutation remained false green |
| Windows Git Bash corrected exact patch | UNMEASURED | UNMEASURED | Must be rerun independently; no claim made |

The macOS count is higher because its BSD/GNU exact-path controls include Darwin-specific cases.
Windows counts are reported separately and are not inferred from macOS.

## Artifact integrity

- Complete patch SHA-256: `ed2244944285beae12eb126c9c436a128dabad8c77962e9bad5a23d51a54babb`
- Final `checksums.sh`: `3195502d98394fb053fdf2214e538ecd8ae5a77bd28dbecba075857cef18f60a`
- Final `promote.sh`: `bc69bf0366afcf7cc9d51b8e004c9bf91a1267e32d0f3002c8ed99c85929ae23`
- Final `selftest.sh`: `a4465f1d0a8c803881e374bd2c6c909403aaf64f841b99efbb8c520ef646dcbf`
- Final `MACOS.md`: `0007701f4410019d99e458085f2e2cce268dd8f4fcb217097b1b828f990eb727`
- Remaining macOS failures: none.
- Corrected Windows result: `UNMEASURED`; no Windows compatibility claim is made.

The package is review-only. It was not installed, pinned, merged, or promoted.

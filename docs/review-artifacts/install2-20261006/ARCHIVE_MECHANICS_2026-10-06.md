# Archive Bound Correction

As-of 2026-10-06 21:12 ET, codex-2. First attended attempt stopped at
backup-and-source-env, completed=0, with `command output exceeds bound`.
Measured `git archive 4805ddc8 | wc -c` = 40,673,280 bytes, larger than the
literal 40,000,000-byte limit. No source-before.tar was persisted. Checkout
remains 4805ddc8, environment and all service identities unchanged; abort page
adapter rc0 confirmed. Original STOP and incomplete attempt are preserved.

Mechanics-only change: source archive limit is 64 MiB. No source, environment
update, gate admission, service sequence, trading rule or policy is changed.
Tests through the real command/prepare path accept the recorded 40,673,280-byte
shape, while 64 MiB + 1 still refuses before output persistence. All 79 literal
runner tests pass in 11.09 seconds. Fresh package/manifest/approval and exclusive
attempt required; no reuse of the refused package or overwritten evidence.

Original attempt:
`/home/trader/after-hours/2026-10-06/install2-5b8b4f64/job/attempt-install2-oct6-attended`.
Application remains `5b8b4f642bbc3c312be436d0e92adbc22d9e9f95` with the same
reviewer pin, merge tree and actual source-combination evidence.

# Laptop migration — Windows → macOS

> ⛔⭐⭐ **THIS FILE LIVES IN THE REPO ON PURPOSE.** The instructions for moving Claude's memory
> must not live *in* Claude's memory — if the memory directory fails to transfer, a memory note
> explaining how to transfer memory is useless exactly when it is needed. A fresh `git clone`
> carries its own recovery instructions.

**Written 2026-08-30 by `claude-1`.** Source machine: Windows, user `kkvkr`. Target: macOS.

**Revised 2026-08-31 by `claude-1`, after the migration actually ran.** Everything written on 08-30
was a rehearsal. The sections marked ⭐ **08-31** are what the real cutover taught — they are the
parts the rehearsal had wrong or missing, and one of them would have deleted the live memory
directory.

---

# What is actually at risk

Almost everything is recoverable: **the repo is on GitHub, the fleet runs on the VPS.** Exactly two
directories exist only on the laptop.

| what | Windows path | size |
|---|---|---|
| **Claude's memory** | `C:\Users\kkvkr\.claude\projects\C--Users-kkvkr\memory\` | 168 files, 1.6 MB (as of 2026-08-31) |
| **Fleet board** | `C:\Users\kkvkr\.claude\mai-tai-fleet\` | 783 KB — journals, claims, checksums |
| SSH keys | `C:\Users\kkvkr\.ssh\` — `mai_tai_vps`, `id_ed25519_codex_vps`, `config`, `known_hosts` | small |
| Claude settings | `C:\Users\kkvkr\.claude\settings.json`, `settings.local.json` | small |

**Optional:** `.claude\projects\C--Users-kkvkr\*.jsonl` — 17 transcripts, ~150 MB. Only needed for
`claude --resume`. The memory files carry the durable knowledge; these are conversation logs.

---

# ⛔ The certain breakages, Windows → macOS

*(Four were predicted on 08-30; the fifth was found by running it.)*

## 1. The memory folder name encodes the old path — and this one fails SILENTLY

`C--Users-kkvkr` is an encoding of `C:\Users\kkvkr`. On macOS the working directory is
`/Users/<you>`, so **the folder needs a different name**. Get it wrong and nothing errors — Claude
simply behaves as though it has no memory of the project.

⇒ **Determine the target name empirically, do not guess it.** On the Mac, start Claude Code once in
the working directory you intend to use, let it create `~/.claude/projects/<generated-name>/`, then
copy the `memory/` directory *into that existing folder*.

## 2. `~/.ssh/config` hard-codes a Windows path

```
IdentityFile C:\Users\kkvkr\.ssh\mai_tai_vps      ← meaningless on macOS
```
Rewrite to `IdentityFile ~/.ssh/mai_tai_vps`. The `Host mai-tai-vps` block otherwise transfers
unchanged (HostName `104.236.43.107`, User `trader`, `IdentitiesOnly yes`).

## 3. macOS REFUSES a private key with loose permissions

Files copied off Windows arrive group/world-readable and `ssh` will reject them outright:
```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/mai_tai_vps ~/.ssh/id_ed25519_codex_vps ~/.ssh/config
chmod 644 ~/.ssh/*.pub ~/.ssh/known_hosts
```

## 4. `.venv` cannot be copied

A Windows virtualenv is unusable on macOS — absolute paths and Windows launchers are baked in.
**Rebuild it.** Never copy it.

## 5. ⭐ **08-31** — `.checksums` pins ABSOLUTE paths, so the gate cries tampering after any move

`~/.claude/mai-tai-fleet/.checksums` records each fleet gate script as `<sha256>  <absolute path>`.
Copied verbatim, every line names a path that does not exist on the new machine:

```
e3b0c442…  /c/Users/kkvkr/.claude/mai-tai-fleet/08_install_runtime.sh    ← what actually moved
e3b0c442…  /Users/velkris/.claude/mai-tai-fleet/08_install_runtime.sh    ← what the Mac needs
```

`./checksums.sh verify` then reports **every** script `FAILED` and exits 1 — reproduced on the Mac,
12 of 12. It **fails closed**, which is correct. The trap is the report and its remedy: the output
is indistinguishable from real tampering, and the line printed underneath it — *"review the change,
then ./checksums.sh record"* — **re-pins whatever arrived, without ever proving it survived the
move.** The pin is a drift detector for one machine; it cannot see a machine change at all.

⇒ **Order matters. Prove the CONTENT first, then re-pin:**
```bash
cd ~/bundle && shasum -a 256 -c ../MANIFEST.sha256   # relative paths ⇒ portable; THIS is the proof
~/.claude/mai-tai-fleet/checksums.sh record          # re-pin under the new absolute paths
~/.claude/mai-tai-fleet/checksums.sh verify          # must now print ✅
```
⛔ **Never run `record` to turn a red `verify` green.** The migration manifest is the only artefact
that distinguishes *moved machine* from *modified in transit*; `record` cannot, and never could.

---

# Do NOT copy

The repo itself (`git clone`) · `.venv` · `~/.claude/cache`, `shell-snapshots`, `paste-cache`,
`downloads`, `session-env` · any scratchpad temp directory.

---

# Before leaving the old machine

The working checkout historically carries uncommitted work and local-only branches. **Push
everything rather than deciding what matters:**

```bash
cd <repo>
git add -A && git commit -m "wip: pre-migration snapshot"
git push origin HEAD:refs/heads/backup/pre-migration
git push origin --all
```

⚠️ Also check for untracked documents that exist nowhere else — `docs/handoff-manifest/`,
loose `docs/*.md`, and any `data/` or `*.csv` capture output.

---

# ⭐ **08-31** — The transfer itself, and it must FAIL CLOSED

Verification **gates** the replacement, and nothing is ever deleted.

```bash
# WINDOWS — build the bundle, and a manifest of RELATIVE paths
S=~/Desktop/mac-cutover && mkdir -p "$S/bundle"
cp -r ~/.claude/projects/C--Users-kkvkr/memory "$S/bundle/memory"
cp -r ~/.claude/mai-tai-fleet "$S/bundle/mai-tai-fleet"
cd "$S/bundle" && find . -type f -print0 | sort -z | xargs -0 sha256sum > "$S/MANIFEST.sha256"
cd "$S" && tar -czf mac-cutover.tar.gz bundle MANIFEST.sha256 && sha256sum mac-cutover.tar.gz
```

```bash
# MAC — ⛔ FAILS CLOSED. `shasum -c` gates every line below it.
set -euo pipefail
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
MEM=~/.claude/projects/<generated-name>/memory     # ⛔ the GENERATED name — see breakage 1
FLEET=~/.claude/mai-tai-fleet

tar -xzf ~/mac-cutover.tar.gz -C ~
cd ~/bundle
shasum -a 256 -c ../MANIFEST.sha256          # non-zero exit here ABORTS under set -e

mv "$MEM"   "$MEM.pre-cutover-$STAMP"        # rename — NEVER rm -rf
mv "$FLEET" "$FLEET.pre-cutover-$STAMP"
cp -R ~/bundle/memory "$MEM"
cp -R ~/bundle/mai-tai-fleet "$FLEET"
chmod +x "$FLEET"/*.sh
```

⛔⭐⭐ **Why this exact shape.** The first draft piped verification into `grep -c "OK$"` — which
**prints a number and gates nothing** — and the `rm -rf` ran regardless. Without `pipefail` a
`shasum` failure did not propagate either, so **a corrupt archive would have deleted the live memory
and the fleet board anyway.** In the shape above a bad hash exits non-zero under `set -e` *before*
anything is touched, and the originals are renamed, so rollback is one `mv` back.

⛔ Delete the `.pre-cutover-*` copies only after the behavioural check below passes.
Real run 2026-08-31: **253 files, 253 OK**, then the `.checksums` re-pin from breakage 5.

---

# On the new machine

```bash
gh auth login                     # the token is not portable
ssh mai-tai-vps 'hostname'        # proves key + config + permissions all landed
cd <repo> && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
git worktree prune                # old worktrees point at dead Windows temp paths
```

---

# ⭐ Verify by EFFECT, not by "the copy finished"

```bash
ls ~/.claude/projects/<generated-name>/memory/ | wc -l    # 168 on 2026-08-31 — smoke, NOT a gate
ls ~/.claude/mai-tai-fleet/                               # expect board.sh, agents/, claims/
ssh mai-tai-vps 'hostname'                                # no password prompt
MAI_TAI_REPO=<repo> ~/.claude/mai-tai-fleet/board.sh | head
```

## ⭐⭐ **08-31** — A COUNT IS NOT A GATE

A count is something you *read*; a gate is something that *stops*. `grep -c "OK$"` printed 253 and
authorised nothing. `wc -l` on the memory directory proves files exist, not that Claude loaded them.
The same day, the fleet's own promote gate — the one that authorises close-outs — was found
returning a false **PASS**: both sides of a manifest comparison were normalised through `$(...)`, so
**a manifest differing by ONE TERMINAL NEWLINE compared equal and authorised a clear.**

⇒ A check earns the word *gate* only when a failure exits non-zero **and** something downstream
refuses to run. Everything else is a number in a log.

**The real test is behavioural.** In a new session, ask something only memory would know — *"what is
the v2 entry window?"* A correct install answers **07:00–16:00 ET** and warns that the older
"7:00–18:00" note was wrong. Vagueness means the memory directory did not land, regardless of what
the file count said.

---

# ⛔⭐⭐ **08-31** — After the cutover, the OLD machine must STOP WRITING

**One writer per file is the whole guarantee of the fleet board.** `agents/<name>.md` and
`claims/<name>.md` are append-only journals; two machines appending to the same journal inside one
batch diverge **irreconcilably** — `manifest.sh` cannot reconcile them, and the loss looks exactly
like nobody wrote anything.

⇒ The moment the Mac's `checksums.sh verify` prints ✅, **Windows becomes read-only.** Keep it intact
as a rollback, never as a second writer.

---

⛔ **Keep the Windows machine intact until the BEHAVIOURAL check passes** — not until the copy
finishes, and not until the file counts match. Nothing here is destructive to the
old laptop, and the fleet on the VPS is unaffected by any of it.

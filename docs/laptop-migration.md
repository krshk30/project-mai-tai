# Laptop migration — Windows → macOS

> ⛔⭐⭐ **THIS FILE LIVES IN THE REPO ON PURPOSE.** The instructions for moving Claude's memory
> must not live *in* Claude's memory — if the memory directory fails to transfer, a memory note
> explaining how to transfer memory is useless exactly when it is needed. A fresh `git clone`
> carries its own recovery instructions.

**Written 2026-08-30 by `claude-1`.** Source machine: Windows, user `kkvkr`. Target: macOS.

---

# What is actually at risk

Almost everything is recoverable: **the repo is on GitHub, the fleet runs on the VPS.** Exactly two
directories exist only on the laptop.

| what | Windows path | size |
|---|---|---|
| **Claude's memory** | `C:\Users\kkvkr\.claude\projects\C--Users-kkvkr\memory\` | 167 files, 1.6 MB |
| **Fleet board** | `C:\Users\kkvkr\.claude\mai-tai-fleet\` | 783 KB — journals, claims, checksums |
| SSH keys | `C:\Users\kkvkr\.ssh\` — `mai_tai_vps`, `id_ed25519_codex_vps`, `config`, `known_hosts` | small |
| Claude settings | `C:\Users\kkvkr\.claude\settings.json`, `settings.local.json` | small |

**Optional:** `.claude\projects\C--Users-kkvkr\*.jsonl` — 17 transcripts, ~150 MB. Only needed for
`claude --resume`. The memory files carry the durable knowledge; these are conversation logs.

---

# ⛔ The four certain breakages, Windows → macOS

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
ls ~/.claude/projects/<generated-name>/memory/ | wc -l    # expect 167
ls ~/.claude/mai-tai-fleet/                               # expect board.sh, agents/, claims/
ssh mai-tai-vps 'hostname'                                # no password prompt
MAI_TAI_REPO=<repo> ~/.claude/mai-tai-fleet/board.sh | head
```

**The real test is behavioural.** In a new session, ask something only memory would know — *"what is
the v2 entry window?"* A correct install answers **07:00–16:00 ET** and warns that the older
"7:00–18:00" note was wrong. Vagueness means the memory directory did not land, regardless of what
the file count said.

⛔ **Keep the Windows machine intact until all four checks pass.** Nothing here is destructive to the
old laptop, and the fleet on the VPS is unaffected by any of it.

# CLAUDE.md

Guidance for Claude Code (claude.ai/code) and other agents working in this repository.

**This file is about writing code for the public `okfmem` repo.** The engine (the tool) lives
here; your private memory *data* lives in a separate store (`~/okfmem-store`, remote
`github.com/<you>/okfmem-store`). Never conflate the two — code ships here, data never does.

## Hard rules (no exceptions)

- **No private strings in tracked files.** The repo is public. Never commit: a real home path
  (`/Users/<yourname>`), a private session URL (`claude.ai/code/session_…`), a `Claude-Session:`
  trailer, or a personal email. Use the documented placeholders instead — `~/okfmem-store`,
  `$OKFMEM_STORE`, `/Users/you`. **`python3 scripts/check-leaks.py` enforces this** (wired into
  the `verify` CI job as the first, fail-fast step). It scans the *content* of tracked files and
  exits non-zero naming the offending `file:line`. It does **not** scan commit history, and it
  cannot judge whether prose is private — that judgement is still yours. Run it before committing.

- **No model provenance in git.** No `Co-Authored-By:` trailer naming a model, no `Claude-Session:`
  trailer, no `https://claude.ai/code/session_…` URL, no `🤖 Generated with …` badge — not in a
  commit message, not in a PR body's git metadata. The Bash tool's default commit template
  suggests these; **ignore it.** Model provenance that helps a reader (which model, how much was
  AI-assisted, what a human reviewed) belongs in the **PR body only**, as a short prose
  `## Provenance` block a reviewer can actually read. The private session link helps no one but
  the author and ties public commits to a personal account — keep it out.

- **`internal/` never ships.** Marketing, launch drafts, positioning notes live under `internal/`,
  which is git-ignored here (it is its own separate private repo). Nothing under it is public.

## Project overview

`okfmem` is a self-maintaining memory engine over Open Knowledge Format (OKF) markdown pages —
no database, no server. It adds mathematical decay, OKF frontmatter, and automatic archival on
top of a native agent's file reading (e.g. Claude Code auto-loading `MEMORY.md`) so the
always-loaded index stays lean instead of growing into an endless scratchpad.

**Engine ⇄ Store split** (like `chezmoi`): this repo is the *engine*; your memory *pages* live in
a separate private *store* resolved per-command as `--store PATH`, else `$OKFMEM_STORE`, else
`~/okfmem-store`. The store is never committed here.

## Confirmation discipline

okfmem's own scripts must never mutate outward-facing or user-owned state *silently*, and never
destroy data without a deliberate, hard-to-fumble confirmation. Every state-changing op sits on a
three-rung ladder; write it to the rung that matches its blast radius, by default, instead of
rediscovering the right friction per-PR:

| Rung | Examples | Required friction |
|---|---|---|
| **Read-only / additive-safe** | `okfmem status`, `okfmem pull` (fail-open no-op), report-only warnings | none |
| **Outward or user-config mutating** | write `~/.claude/settings.json` (hook wiring), create symlinks/junctions/copies under `~/.claude`, add/set a git remote, push | **yes/no confirm** (`[y/N]`) |
| **Destructive / irreversible** | delete the store, wipe memory pages, remove wired hooks, `Remove-Item -Recurse` | **typed confirmation** (retype a word/path), never a bare `-Force` |

This is not new — it is the discipline the codebase already applies in its best spots, made
consistent. `detect_legacy_clone` is the gold standard: it **warns, never deletes, hands the user
the command.** `install.ps1::Set-StoreRemote` already gates the outward GitHub-remote step behind
`[y/N]`.

Two invariants keep the friction from becoming a regression:

- **Every prompt is skippable non-interactively.** A piped / CI install (no TTY) takes the
  documented default — skip the outward op — and **prints the exact manual command** to run it
  later (the `install.ps1::Write-ManualRemoteHint` pattern). A confirmation that can't be scripted
  around is a bug for automated installs.
- **Never prompt on rung-1 ops.** Friction on `status` / a fail-open `pull` is pure annoyance.

## Stack and commands

Pure Python 3.11+, standard library only for the core. Entry point is the `okfmem` dispatcher,
which routes to the `memory_*.py` modules.

```bash
okfmem status                 # store + per-project page/archive counts, sync + hook health
okfmem backfill  --dry-run    # stamp decay frontmatter on existing pages
okfmem init      --dry-run    # pointers + registry wiring
okfmem consolidate --dry-run  # decay + archive stale pages + push
okfmem sync [-m "<msg>"]      # commit + pull-rebase + push the store (prompts for the message if -m omitted)

python3 scripts/check-leaks.py   # leak gate (also runs first in CI)
ruff check .                     # lint (advisory in CI today)
ruff format --check .            # format check (advisory in CI today)
```

Opt-in commands (`index`, `search`) route lazily to `plugins/memory_search.py` and build a
rebuildable, git-ignored local SQLite FTS cache (`*.db`) — never committed.

## PRs

- **One commit per PR.** `main` is protected and merges through a queue that derives the squash
  message from the branch; a multi-commit PR would land `wip`/`fix lint` in `main` forever.
  Collapse the branch to a single commit before it reaches the queue.
- CI (`.github/workflows/ci.yml`) runs the leak gate (hard), then ruff + pytest (advisory /
  skip-if-absent). The leak gate must pass.
- `main` requires a green `verify` check **and 1 approving review from a Code Owner**
  (`.github/CODEOWNERS`). Solo owner can't self-approve, so merges own PRs via admin bypass
  until a co-maintainer joins; then the approval rule binds everyone.

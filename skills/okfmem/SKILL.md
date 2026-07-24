---
name: okfmem
allowed-tools: Read, Bash, Glob, Grep
description: High-level overview + status of the okfmem memory system — store location, per-project page/archive counts, decay/consolidation state, git sync status, hook + skill wiring health. `/okfmem` for the dashboard, `/okfmem usage` for the how-to.
---

# /okfmem — memory system overview

Read-only status + orientation for the okfmem memory system. It answers "what is
the state of my memory, and how do the pieces fit together" without touching
anything. Two modes:

- **`/okfmem`** (no arg) → the **status dashboard** (Steps 1–5 below).
- **`/okfmem usage`** → the **how-to** (the "Usage" section at the end): what each
  mechanism does, when it fires, and which skill/command to reach for.

Everything here is read-only. Never write, commit, archive, or curate from this
skill — point the user at `/okfmem-save` or `/okfmem-curate` for those.

## The system in one screen

Two git-backed repos + native memory auto-load:

| Repo | Role | Path / remote |
|---|---|---|
| `okfmem` | **engine** — skills + Python (`memory_*.py`, `okfmem` CLI) | `~/okfmem` → `github.com/s-annam/okfmem` (private) |
| `okfmem-store` | **data** — markdown pages, `MEMORY.md`, `STATE.md`, `decay_state.json` | `~/okfmem-store` → `github.com/s-annam/okfmem-store` (private) |

Two memory layers, both plain markdown the native memory system auto-loads:

- **Active state** — `projects/<name>/STATE.md`, bounded 6-section snapshot, **overwritten** each session by `/okfmem-save`.
- **Durable knowledge** — `projects/<name>/<slug>.md` pages indexed by one-line pointers in `MEMORY.md` (first 200 lines auto-load); pages read on demand.

Four moving parts:

| Part | Trigger | What it does |
|---|---|---|
| **capture + STATE + push** | `/okfmem-save` (manual, end of session) | model writes pages/STATE, then `okfmem sync` commits+pushes |
| **hygiene** (decay/archive/regen MEMORY.md) | Stop hook → `memory_consolidate.py` | deterministic; archives stale pages (reversible), never deletes |
| **sync in** | SessionStart hook → `git pull --rebase` | freshens the store before the session |
| **hard curation** | `/okfmem-curate` (rare, gated) | semantic merge / hard purge decay won't do |

`okfmem sync` (shared git helper) backs **both** `/okfmem-save` and the Stop-hook
consolidation, so pull-rebase + concurrency-lock behavior is identical on both.

## Status dashboard (`/okfmem`)

### Step 1: Engine status

```bash
python3 ~/okfmem/okfmem status
```

Relay its output: detected harnesses + pointer state, registry roots/overrides,
stale-reference count, and the skills-wiring line (`claude_code:N, codex:N,
antigravity:N` — with "not linked — run `okfmem init`" if any are pending).

Then probe **this repo's** own memory link — the per-repo wiring is separate
from the machine-wide install, and an unlinked repo fails invisibly:

```bash
python3 ~/okfmem/okfmem init --project-link-state   # read-only
```

`linked <name>` is healthy. On **`unlinked <name>`, lead the summary with it**:
this repo isn't wired, nothing said here will be remembered, and the fix is one
command run from the repo root — `okfmem init` (it seeds the store project dir
too, so a never-saved repo wires up in that single step). `not-a-repo` /
`no-claude` are informational, not problems.

### Step 2: Store inventory

```bash
STORE="${OKFMEM_STORE:-$HOME/okfmem-store}"
for d in "$STORE"/projects/*/; do
  name=$(basename "$d")
  pages=$(ls "$d"/*.md 2>/dev/null | grep -vcE '/(MEMORY|STATE)\.md$')
  mem=$( [ -f "$d/MEMORY.md" ] && wc -l < "$d/MEMORY.md" | tr -d ' ' || echo 0 )
  arch=$(ls "$d"/archive/*.md 2>/dev/null | wc -l | tr -d ' ')
  state=$( [ -f "$d/STATE.md" ] && echo yes || echo no )
  printf "  %-24s pages:%-4s MEMORY.md:%-4s archived:%-4s STATE:%s\n" "$name" "$pages" "$mem" "$arch" "$state"
done
```

Flag any project whose `MEMORY.md` exceeds ~200 lines (auto-load truncation
point) — candidate for `/okfmem-curate`.

### Step 3: Decay / consolidation state

```bash
STORE="${OKFMEM_STORE:-$HOME/okfmem-store}"
cat "$STORE/decay_state.json" 2>/dev/null || echo "  no decay_state.json (consolidation not yet run on this machine)"
```

`decay_state.json` holds the cold-start EPOCH guard the consolidation job uses so
a freshly-cloned store doesn't mass-archive on first run.

### Step 4: Git sync status (both repos)

```bash
for r in "$HOME/okfmem" "${OKFMEM_STORE:-$HOME/okfmem-store}"; do
  echo "== $r =="
  git -C "$r" status -sb | head -1
  git -C "$r" log -1 --format='  last: %h %s (%cr)'
done
```

Call out ahead/behind or a dirty tree — a dirty store means the next `okfmem
sync` will sweep those changes in.

### Step 5: Hook + skill wiring health

```bash
echo "== Stop/SessionStart hooks =="
grep -oE 'memory_consolidate\.py|okfmem-store. pull --rebase' ~/.claude/settings.json | sort -u
echo "== skill symlinks =="
for h in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.gemini/config/skills"; do
  [ -d "$h" ] || continue
  for s in okfmem okfmem-save okfmem-curate primer memory-curate; do
    [ -e "$h/$s" ] && printf "  %s/%s -> %s\n" "${h/#$HOME/~}" "$s" "$(readlink "$h/$s" 2>/dev/null || echo '(real)')"
  done
done
```

Confirm: Stop hook has `memory_consolidate.py`, SessionStart has the store
`pull --rebase`, and each harness has both the canonical `okfmem-*` skills and
the `primer`/`memory-curate` aliases. Missing canonical symlinks → tell the user
to run `okfmem init`.

### Summarize

Close with a 3–5 line health summary: how many projects, total pages, anything
over the MEMORY.md line cap, sync state of both repos, and any wiring gap with
the one command that fixes it (`okfmem init` for skills, a manual `git pull` for
drift).

## Usage (`/okfmem usage`)

Print this orientation instead of the dashboard:

**Daily flow**
- **Start of session** — nothing to do. SessionStart hook pulls the store;
  `STATE.md` + `MEMORY.md` auto-load. On a *new workstation*, `git -C
  ~/okfmem-store pull --rebase` once (cross-machine pull isn't auto yet).
- **During a session** — capture is a judgment call. To durably record an
  insight mid-session, write a `<slug>.md` page + a `MEMORY.md` pointer (same
  shape `/okfmem-save` uses).
- **End of session** — run **`/okfmem-save`** (alias `/primer`): cleans up
  worktrees, captures insights, overwrites `STATE.md`, optionally posts an
  impl-complete issue comment, then `okfmem sync` commits+pushes the store.

**When to reach for what**
- `/okfmem` — this overview / status.
- `/okfmem-save` (`/primer`) — session close-out (capture + STATE + push).
- `/okfmem-curate` (`/memory-curate`) — **rare**; judgment-driven purge/merge
  the automatic decay pass won't do. Routine hygiene is already automatic.
- `okfmem sync -m "…"` — commit+push the store by hand (pull-rebase + lock).
- `okfmem init` — run once **in each repo** you want memory for (the link is
  per-repo; the installer only wired the repo it ran in). Also (re)wires skills
  + pointers into each harness after a clone.
- `okfmem consolidate --dry-run` — preview what decay would archive.

**What's automatic vs manual**
- Automatic: decay/archive + `MEMORY.md` regen (Stop hook), store pull
  (SessionStart hook).
- Manual: capturing new insights and writing `STATE.md` — these need the session
  model reading the conversation, so they live in `/okfmem-save`, not a hook.

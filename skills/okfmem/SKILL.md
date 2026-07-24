---
name: okfmem
allowed-tools: Read, Bash, Glob, Grep
description: High-level overview + status of the okfmem memory system — store location, per-project page/archive counts, decay/consolidation state, git sync status, hook + skill wiring health. `/okfmem` for the dashboard, `/okfmem usage` for the how-to.
---

# /okfmem — memory system overview

Read-only status + orientation for the okfmem memory system. It answers "what is
the state of my memory, and how do the pieces fit together" without touching
anything. Two modes:

- **`/okfmem`** (no arg) → the **status dashboard** (Steps 1–3 below).
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

### Step 2: Store inventory + decay state

`okfmem status` (Step 1) already prints the per-project inventory and decay
epoch — it is a pure-Python part of the engine, so it renders identically on
macOS and Windows. **Do not re-derive this in shell.** Relay the section it
prints:

```
  projects (14):
  * okfmem              pages:27   MEMORY.md:33   archived:0    STATE:yes
    tools               pages:158  MEMORY.md:202  archived:0    STATE:yes   ! over 200-line auto-load limit
    + 11 more (okfmem status --all)
  decay: epoch 2026-07-16
```

- The `*` marks the project the current working directory maps to.
- Any project whose `MEMORY.md` exceeds the 200-line auto-load limit is flagged
  inline (`! over 200-line auto-load limit`) — a candidate for `/okfmem-curate`.
- The default view collapses to the current project plus any over-limit
  project. When the user wants the **full** list, re-run
  `python3 ~/okfmem/okfmem status --all`; for a single project,
  `python3 ~/okfmem/okfmem status --project <name>`.
- The `decay:` line reports the `decay_state.json` epoch (the cold-start guard
  the consolidation job uses so a freshly-cloned store doesn't mass-archive on
  first run), or `not yet run on this machine` when the file is absent.

### Step 3: Hook + skill wiring health

`okfmem status` also prints the wiring half cross-platform — relay it rather
than shelling out:

- `stop hook:` / `pull hook:` — `wired` is healthy; anything else means
  `okfmem init` hasn't run (or a legacy hook needs healing).
- `skills:` — per-harness canonical skill counts, with
  `(N not linked — run okfmem init)` when any are pending.
- `store sync:` — the store's git state (clean/in-sync, or dirty/unpushed/behind
  with the `okfmem sync` fix).
- An engine-update nudge appears at the end when a newer `okfmem` is available.

Missing canonical skills or an unwired hook → tell the user to run `okfmem init`.

### Summarize

Close with a 3–5 line health summary: how many projects, anything over the
`MEMORY.md` line cap (from the flagged rows), the store sync state, and any
wiring gap with the one command that fixes it (`okfmem init` for skills/hooks,
`okfmem sync` for a dirty store).

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

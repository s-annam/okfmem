---
name: okfmem-save
allowed-tools: Read, Write, Edit, Bash, Glob, Grep, Agent, mcp__claude_ai_Linear__get_issue, mcp__claude_ai_Linear__save_comment, mcp__claude_ai_Linear__list_comments, mcp__linear__get_issue, mcp__linear__save_comment, mcp__linear__list_comments
description: "Session close-out — clean up tool-created worktrees/branches, write active state to STATE.md, capture durable insights as memory pages, and commit + push okfmem-store via `okfmem sync`. Drafts an impl-complete comment on the resolved issue (Linear via MCP, GitHub via `gh issue comment`). Invoked as /okfmem-save (alias: /primer)."
---

# /okfmem-save — Write active state + capture insights + commit & push okfmem-store

> **Names.** Canonical `/okfmem-save`; `/primer` is a back-compat alias (a
> symlink at `~/tools/skills/primer` → this skill). Both invoke the same
> process. This skill and the memory engine live in the `okfmem` repo
> (`~/okfmem/skills/`); `okfmem init` symlinks it into each harness.

`/okfmem-save` closes out a work session in one move: overwrite the project's bounded active state (`STATE.md`), **capture durable insights as per-project markdown memory pages**, optionally post an impl-complete comment on the resolved issue (Linear or GitHub), and commit + push `~/okfmem-store` synchronously at the end.

**Everything is plain markdown under `~/okfmem-store` — no MCP, no daemon.** Active state and durable insights are files the native memory system auto-loads next session (`STATE.md`, `MEMORY.md`, and the `<topic>.md` pages). This skill writes them directly, in-session, on whatever model the session is on, then runs the final `git` commit/push via the shared `okfmem sync` helper.

## Two-layer architecture (for context)

Everything lives under `~/.claude/projects/<proj-dir>/memory/` (symlinked to `~/okfmem-store/projects/<name>/`):

- **Active state** lives in `STATE.md` — a bounded, single-session snapshot with a fixed 6-section shape (Summary / Left off / Next steps / Decisions / Blockers / Goal) plus OKF `type: state` frontmatter. It is **overwritten every session**, never appended. The native memory system auto-loads it next session.
- **Durable knowledge** lives in per-topic `<topic>.md` pages (OKF v0.1: markdown + YAML frontmatter with a top-level `type:` field ∈ `user` | `feedback` | `project` | `reference`), indexed by one-line pointers in `MEMORY.md`. This is the durable write path: create or update the page, then add/refresh its `MEMORY.md` pointer.
- `STATE.md` uses a separate `type: state` (see Step 5) — a different file with a different consumer (active-state snapshot, not the durable-page index), not a fifth value in the `type:` enum above.

## When to use

End-of-session. Also works mid-session for a checkpoint.

## Cost: delegate drafting to Haiku

`/okfmem-save` runs inside the current session, so its model is whatever your session is on (often Opus). The work splits cleanly:

| Phase | Character | Model |
|---|---|---|
| Triage (Step 2, 4, parts of 6) | judgment; needs full session context | session model |
| Insight capture (Step 3) | direct markdown page writes | session model — **direct, no Agent** |
| Drafting (Step 6 comment body) | mechanical formatting | **Haiku via Agent** |

Insight capture (Step 3) is a handful of small `Write`/`Edit` calls on the session model — one memory page plus its `MEMORY.md` pointer per insight. It is cheap; do **not** spawn a Haiku Agent per memory file.

The remaining delegation candidate is the Step 6 impl-complete comment body: spawn an `Agent(subagent_type="claude", model="haiku", ...)` briefed with the *decided* content (criteria → met/unmet + notes), have it return the body text; the main session writes the file and posts. Triage decisions stay on the main model.

## Process

### Step 1: Identify the active project

First, recover from a deleted cwd. `commit-all.sh` often runs immediately before `/okfmem-save` and cleans up the worktree the session was implementing in (`<repo>/.claude/worktrees/<issue-id>/`). When that happens, `$PWD` still holds the path string but the directory is gone — every `git`/`pwd`-based lookup below will fail until we cd back to a live checkout.

```bash
if [ ! -d "$PWD" ]; then
  case "$PWD" in
    */.claude/worktrees/*)
      MAIN_CHECKOUT="${PWD%%/.claude/worktrees/*}"
      if [ -d "$MAIN_CHECKOUT" ]; then
        cd "$MAIN_CHECKOUT"
        echo "okfmem-save: cwd was a worktree that's been cleaned up; switched to $MAIN_CHECKOUT"
      else
        echo "okfmem-save: cwd $PWD is gone and fallback $MAIN_CHECKOUT also missing — cd to the project root and re-run" >&2
        exit 1
      fi
      ;;
    *)
      echo "okfmem-save: cwd $PWD no longer exists and is not a recognized worktree path — cd to the project root and re-run" >&2
      exit 1
      ;;
  esac
fi

PROJECT_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
PROJECT_NAME="$(basename "$PROJECT_ROOT")"
MEMORY_DIR="$HOME/.claude/projects/$(echo "$PROJECT_ROOT" | sed 's|:|-|g; s|/|-|g')/memory"
```

If `$MEMORY_DIR` does not exist or is not a symlink into `~/okfmem-store/projects/`, the project isn't set up yet — tell the user and offer to scaffold (create `~/okfmem-store/projects/<name>/` with a `MEMORY.md` + `STATE.md`, then symlink it into `$MEMORY_DIR`).

### Step 1c: Clean up tool-created worktrees and branches

Before any memory write, reclaim the worktrees and branches that tooling left behind this session. `/implement-issue`, `/implement-epic`, `commit-all.sh`, and the worktree isolation used by Stop hooks (fallow audits and friends) all create `<repo>/.claude/worktrees/<slug>/` dirs and local feature branches (`feat/…`, `gh-<N>`, lowercase Linear IDs); after a ff-merge into `main` they are dead weight, and `git worktree remove`'s partial-success failure mode (TCC, Docker pins) leaves orphan dirs behind. The `fallow audit --changed-since` Stop hook also leaves **detached, clean temp worktrees** in `$TMPDIR` (basename `fallow-audit-base-cache-*`) — the sweep now matches those too. Run the sweep:

```bash
bash "$PROJECT_ROOT/scripts/cleanup-worktrees.sh"   # ~/tools — adjust if PROJECT_ROOT differs
```

It is **safe by default and non-interactive**: it removes only *clean* worktrees and branches *fully merged into `main`*, and **keeps** (reporting, never deleting) anything with uncommitted changes or unmerged commits. It releases Docker pins first (fail-open), handles git's three worktree-removal outcomes, and prints a TCC remediation hint for any orphan dir it can't `rm`. It always exits 0, so this step never aborts the skill.

- The script lives in `~/tools/scripts/cleanup-worktrees.sh`. If `$PROJECT_ROOT` is a different repo that has no copy, call it by absolute path: `bash ~/tools/scripts/cleanup-worktrees.sh` (it operates on the *current* repo via `git rev-parse`; Step 1 already put you in a live checkout of `$PROJECT_ROOT`).
- **Invoke it as a single, non-compound command** — just `bash "$PROJECT_ROOT/scripts/cleanup-worktrees.sh"`. Do **not** wrap it as `cd … && bash …`: a compound command defeats the `Bash(bash:*)` allowlist prefix and falls to the auto-mode classifier, which gates it — that's the recurring "can't clean up worktrees" failure. If you need a different cwd, `cd` in a **prior, separate** Bash call.
- Direct `git worktree remove` / `git worktree prune` are also allowlisted now, so a targeted manual cleanup won't prompt either — but prefer the script; it is the safe-by-default path.
- Do **not** pass `--force` — that would delete unmerged branches and dirty worktrees. Only a human should opt into that.
- Relay the script's one-line summary (removed N worktrees / M branches; kept list) in Step 8.

### Step 2: Triage this session's content

For each durable item worth preserving across sessions, pick the right home:

| Content | Goes to |
| -- | -- |
| What's in progress, next step, active blocker | **`STATE.md`** (see Step 5) — bounded active state |
| Decision, engineering insight, reusable pattern, "lesson learned" | **Memory page** — a `<topic>.md` page + `MEMORY.md` pointer (see Step 3) |
| Bug discovered, follow-up work not yet started | **Linear issue** — file it |
| What shipped in this session | **`STATE.md`** (`Summary` + `Decisions` if significant) |
| What shipped in prior sessions | **Skip** — `git log` + Linear are authoritative |

### Step 3: Capture durable insights as memory pages

For each durable insight from this session, write (or update) one markdown page under `$MEMORY_DIR` and add/refresh its pointer in that project's `MEMORY.md` index. Emit these `Write`/`Edit` calls directly on the session model — **do not spawn a Haiku Agent per file.** This mirrors the persistent file-based memory convention in `~/.claude/CLAUDE.md`.

The capture bar: decisions, engineering insights, reusable patterns, and lessons learned that will matter across sessions. Skip routine/derivable activity (what shipped, one-off chores) — `git log` and the issue tracker are authoritative for those.

**Never capture what a tool already records.** Before writing a memory page, ask *"could a future session get this from `git log`, `gh pr view`, or the issue tracker in one command?"* If yes, it is not a memory:

- ❌ "PR #447 merged, squash `abc1234`" / "epic #85 closed" / "CI went red because the branch was behind main" / "branch `gh-469-probe` has a worktree at …"
- ❌ issue status, assignee, milestone, review state, a plan for work that is now done
- ✅ the **invariant the PR established** — e.g. "`collectAnchors` rejects a header when the two-column flatten interleaves the date cell" — which survives the PR, the branch, and the issue number

The test is *durability under `git log`*, not importance-at-the-time. Capture the fact **stripped of its status wrapper** — the gotcha, not the changelog. Evidence: a 2026-07-13 curation pass on `~/resumelint` deleted 73 of 445 memory entries (16% of the store), almost all status wrappers; age was a dead signal (nothing over 90 days).

Each page is OKF v0.1: markdown with YAML frontmatter carrying a **top-level `type:`** field (`user` | `feedback` | `project` | `reference`).

- **Filename** — a short kebab-case slug, e.g. `linear-mcp-save-partial-failure.md`. Reuse the *exact* existing filename when extending a topic already captured — `Edit` (or re-`Write`) that page so it merges instead of duplicating.
- **`type`** — the frontmatter kind above.

**New topic** → `Write` the page:

```markdown
---
type: <user|feedback|project|reference>
---

# <Title>

<the insight, one paragraph per point; for feedback/project, include the why and how>
```

**Existing topic** (slug already a page in `$MEMORY_DIR`) → `Edit` that page to add the new fact, or supersede stale content in place (no manual `SUPERSEDED` markers needed — just rewrite the page to current truth).

**Index pointer** — for every new page, add a one-line pointer to that project's `MEMORY.md`; for an updated page, refresh the existing pointer if its hook changed:

```
- [<Title>](<slug>.md) — <one-line hook>
```

Do this step before Step 5 so the `STATE.md` summary can mention what was captured.

### Step 4: File Linear issues for pending work

Follow-ups, deferred scope, discovered bugs → Linear issue. Do not list them in `STATE.md` — `STATE.md` is active state only.

### Step 5: Write active state to `STATE.md`

Overwrite `$MEMORY_DIR/STATE.md` with the current session's active state. **`STATE.md` is bounded and OVERWRITTEN each session — never appended.** It always uses this exact 6-section shape:

```markdown
---
type: state
project: <PROJECT_NAME>
---

# STATE — <PROJECT_NAME>

## Summary
<one-session summary — what was accomplished this session>

## Left off
<specific file/feature/bug actively being worked on>

## Next steps
<bulleted concrete next actions>

## Decisions
<bulleted: - what — why>

## Blockers
<bulleted blockers, or (none)>

## Goal
<the standing goal>
```

Fill each section from the session. Show a draft summary first: `"Session: '<summary>' — save this? (yes / edit)"`. After confirmation, `Write` the file (full overwrite — do not `Edit`/append; the whole file is replaced each session).

Keep `## Goal` as the project's standing goal — carry forward the prior value unless the goal shifted this session. Use `(none)` for empty `## Blockers`.

### Step 6: Post impl-complete comment for completed issues

If code committed this session resolves an issue, post an implementation-complete comment on it. The body shape is identical regardless of backend — only the read + write surface differs.

**Pick the backend the same way `/implement-issue` and `/create-issue` do** — by checking the project's `.env` for `LINEAR_TEAM` (walk up from `$PROJECT_ROOT`, same as `load_env`):

- **`LINEAR_TEAM` set** → Linear backend. Fetch via Linear MCP `get_issue` for acceptance criteria; post via `save_comment`; verify round-trip via `get_issue`. If the issue has a GitHub attachment (`attachments`/`url` field on the Linear issue), prefer posting via `gh issue comment <N> --repo <owner>/<repo> --body-file <tempfile>` — the sync propagates to Linear and avoids the Linear MCP markdown-drop bug for richer bodies (see `~/.claude/CLAUDE.md` → "Writing Linear issue bodies"). Verify via `get_issue` either way.
- **`LINEAR_TEAM=UNSET` AND `gh repo view` succeeds** → GitHub backend. Fetch via `gh issue view <N> --repo <owner>/<repo> --json number,title,body,labels,state` for acceptance criteria; post via `gh issue comment <N> --repo <owner>/<repo> --body-file <tempfile>`; verify with `gh issue view <N> --repo <owner>/<repo> --comments`.
- **Neither available** → skip this step and tell the user one line: "no issue tracker detected — skipping impl-complete comment".

Resolve the issue identifier in priority order: (1) the active worktree slug (`.claude/worktrees/<slug>/` — `gh-<N>` for GH, lowercase Linear ID for Linear), (2) the commit trailer (`Resolves #<N>` / `Refs #<N>`) from `git log -1`, (3) `STATE.md`'s `## Left off` / `## Summary`. If still ambiguous, ask the user.

Steps:

1. Commit hash: `git log --oneline -1`
2. Fetch issue (per backend, as above) — grab acceptance criteria from the body
3. Post the comment with this body:

```markdown
## Implementation Complete — `<commit-hash>`

### Acceptance Criteria

- [x] **Criterion 1** — brief implementation note
- [x] **Criterion 2** — brief implementation note

### Additional Changes

- **Change 1**: description
- **Change 2**: description
```

Rules: check `[x]` with a short note per criterion; use `[ ]` + reason if not met; list additional changes made beyond scope; keep it factual; round-trip verify via the backend's read command (`get_issue` for Linear, `gh issue view --comments` for GitHub).

**Never write the body and post it in one compound bash command.** `Bash(gh:*)` is allow-listed, but a `cat > /tmp/body.md <<EOF … EOF` heredoc chained with `gh issue comment` does *not* match that prefix rule — the compound falls through to the auto-mode classifier, which gates the embedded external write and blocks the whole invocation. Worse, the tempfile write lived inside the blocked compound, so the retry fails with "no such file." Do it as two discrete steps:

1. **Write the body with the Write tool** (not a bash heredoc) to a tempfile, e.g. `/tmp/<issue>-complete.md`. The Write tool is not subject to the bash-command classifier, so the body always lands.
2. **Post it as a standalone command** so it matches `Bash(gh:*)` and sails through without a gate:
   ```bash
   gh issue comment <N> --repo <owner>/<repo> --body-file /tmp/<issue>-complete.md
   ```
   Keep this on its own — no `cd …;`, no `&&`, no heredoc on the same line. (The Linear-MCP `save_comment` path is unaffected; this gotcha is GitHub-only.)

**Delegation:** main session decides which criteria are met and what additional changes shipped. Hand that decided mapping (criterion → "met, note: X" or "unmet, reason: Y", plus the additional-changes list) to an `Agent(model="haiku")` that formats the comment body. The Agent returns the body text; the main session writes it to the tempfile **with the Write tool** and posts via the standalone `gh issue comment` / `save_comment` call above, then verifies round-trip.

### Step 7: Commit and push `~/okfmem-store` via `okfmem sync`

Commit + push the memory repo synchronously at the end of the session — no daemon, no queue. This runs **inside Claude Code**, so it works the same on Mac and Windows (no LaunchAgent / launchd / cron). The commit+push logic lives in one shared place — the `okfmem sync` engine command (`~/okfmem/memory_sync.py`), which the P3 consolidation Stop-hook job also calls — so the pull-rebase and concurrency-lock behavior is identical on both paths:

```bash
okfmem sync -m "<PROJECT_NAME>: <session summary line>"
# or, if `okfmem` is not on PATH:  python3 ~/okfmem/okfmem sync -m "…"
```

`okfmem sync` does: `add -A` → (if anything staged) `pull --rebase` → `commit` → `push`, serialized by an `flock` lockfile at the store root so two windows can't race. It is a clean **no-op when nothing changed** (no empty commits). On a `pull --rebase` conflict it aborts the half-done rebase, leaves `~/okfmem-store` clean, refuses to push, and exits non-zero — surface the conflict to the user rather than forcing. It prints one status line (`committed <sha>: <msg>` + `pushed.`, or the reason it did nothing).

### Step 8: Confirm and show

Show the user:
- The worktree/branch cleanup result (Step 1c): removed N worktrees / M branches, and the kept list if non-empty
- The session summary written to `STATE.md` (one line)
- Any insights captured as memory pages (slugs + one-line hooks; note new vs. updated-existing)
- Any issues filed or commented on (Linear or GitHub)
- The memory push result — the commit SHA that was pushed (from `okfmem sync`'s status line), or `"no memory changes to push"` if the working tree was clean.

## Rules

- **Active state goes in `STATE.md`**, not in `MEMORY.md` or the memory pages
- **`STATE.md` is bounded and overwritten every session** — full replace, never append; keep it to the 6-section template
- **Durable knowledge is captured as `<topic>.md` memory pages + a `MEMORY.md` pointer**, not stored in `STATE.md`
- **Reuse an existing page slug to update a topic** (dedup) — rewrite the page to current truth instead of leaving stale duplicates
- **Pending work goes in the issue tracker** (Linear or GitHub, whichever this project uses), not in `STATE.md` `## Blockers` (reserve blockers for "can't progress" not "haven't started")
- **Capture memory pages BEFORE writing `STATE.md`** so the session summary can reference what was captured
- **Skip reconstructable content** — `git log` + the issue tracker are authoritative for history; don't capture "what shipped"
- **Commit + push `~/okfmem-store` via `okfmem sync`** (Step 7), synchronously, no-op when clean — no daemon, cross-platform; the same helper backs the Stop-hook consolidation job
- **Worktree cleanup (Step 1c) runs before any memory write and is safe-by-default** — `cleanup-worktrees.sh` removes only clean worktrees + branches merged into `main`, keeps anything with uncommitted/unmerged work, and exits 0 always; never pass `--force`

## First-time setup on a new machine

Active state and insights are plain markdown under `~/okfmem-store` — no MCP server, no daemon. Setup is the two repo clones plus `okfmem init`:

```bash
git clone git@github.com:s-annam/okfmem.git       ~/okfmem         # engine (this skill + scripts)
git clone git@github.com:s-annam/okfmem-store.git ~/okfmem-store   # data (markdown pages)
python3 ~/okfmem/okfmem init   # symlinks skills into each harness + wires memory pointers/registry
# Then, for each project with a memory dir in the store, symlink it into the project's memory dir:
ln -sfn ~/okfmem-store/projects/<name> ~/.claude/projects/-Users-<user>-<name>/memory
```

The native memory system auto-loads `STATE.md` / `MEMORY.md` on session start, so no load hook is required. If sessions may run on multiple workstations, add a `git -C ~/okfmem-store pull --rebase` SessionStart hook so each session starts from the latest memory.

---
name: okfmem-curate
description: "Judgment-driven memory curation (rare; routine hygiene is automatic via the okfmem consolidation Stop hook). Inventory, flag stale entries, propose deletions/compressions/semantic merges, rewrite MEMORY.md as tight one-line hooks. Hard approval gate before any deletion. Invoked as /okfmem-curate (alias: /memory-curate)."
origin: user
---

# okfmem Curate

> **Names.** Canonical `/okfmem-curate`; `/memory-curate` is a back-compat
> alias (a symlink at `~/tools/skills/memory-curate` → this skill). This skill
> lives in the `okfmem` repo (`~/okfmem/skills/`); `okfmem init` symlinks it
> into each harness.

> **Fallback tool (as of 2026-07-16).** Routine memory hygiene is now automatic:
> the okfmem P3 consolidation job (`~/okfmem/memory_consolidate.py`, wired to the
> Claude Code session-end Stop hook) decay-scores every page and gracefully
> archives stale ones (`projects/<proj>/archive/`, never deleted), regenerating
> `MEMORY.md`, every session — no hand-running required. See issue #1.
> Reach for `/okfmem-curate` only for **judgment-driven** curation the automated
> decay pass does not do: merging duplicate-with-CLAUDE.md entries, semantic
> consolidation, or a hard purge the archive gate is too conservative to make.
> Note the layers differ: consolidation archives (reversible); this skill can
> delete (hard, gated). Prefer letting decay do the routine work.

Curate the per-project auto-memory store under `~/.claude/projects/<project-slug>/memory/`. Detects stale, superseded, or duplicate-with-CLAUDE.md entries; proposes a deletion/compression plan; on approval, executes and rewrites `MEMORY.md` as tight one-line hooks per the user's auto-memory convention.

Applies the "deterministic collection + LLM judgment" principle: a script collects facts, then an LLM cross-reads each candidate and produces verdicts. **Hard rule:** no file is deleted and no index is rewritten until the user has explicitly approved the plan.

## When to use

- The user says "clean up memory", "prune memory", "memory hygiene", "tighten MEMORY.md", "context is bloated", or similar.
- Periodic curation (monthly, or after a project reaches a milestone where many "X landed" memories accumulate).
- After noticing MEMORY.md exceeds ~200 lines (the auto-load truncation point) or its size has grown well beyond ~10KB.
- **Audit-only check-in**: when the user wants the report without committing to deletions yet — invoke with `audit` argument.

## Modes

| Mode | Trigger | What it does |
|------|---------|--------------|
| Default | "clean up memory" / "prune memory" / no arg | All five phases; halts at approval gate; executes on approval |
| Audit | `audit` arg, or "memory audit" / "what's stale" | Phases 1–3 only; produces the report; no approval gate, no execution |

## How it works

Five phases. Phases 4 and 5 are gated on explicit user approval — never execute deletions or rewrite MEMORY.md without it. The lesson behind this skill: an unguarded "cleanup pass" feels efficient until the index points at deleted files.

### Phase 1: Resolve target directory

Compute the memory dir from the current working directory:

```bash
# project slug = absolute cwd with / replaced by -
SLUG=$(pwd -P | sed 's|/|-|g')
MEM_DIR="$HOME/.claude/projects/$SLUG/memory"
```

If `$MEM_DIR` doesn't exist, tell the user and stop. If the user passed an explicit path argument, use it instead.

Detect whether the dir is symlinked into `~/okfmem-store` (so deletions are git-recoverable). Run:

```bash
readlink "$MEM_DIR" 2>/dev/null
```

If the link points into a git-backed location, surface that to the user as the recovery mechanism. If not, *say so explicitly* — recovery is harder.

### Phase 2: Inventory (deterministic)

Run the inventory script:

```bash
python3 ~/okfmem/skills/okfmem-curate/scripts/inventory.py "$MEM_DIR"
```

The script emits a markdown report with three sections:

1. **Summary**: file count, total bytes, MEMORY.md size + line count, ratio.
2. **Per-file table**: name, size, age (days since mtime), frontmatter `type` if present, frontmatter `name`, MEMORY.md link status (linked / orphan / dangling).
3. **Heuristic flags**: per file, comma-separated flags drawn from filename + content patterns:
   - `ck_snapshot` — filename matches `ck_YYYY-MM-DD_*.md` (CK session-end saves; ephemeral by nature)
   - `landed_doc` — filename or frontmatter `name` contains `landed` / `_operational` (project-state, prone to age out)
   - `superseded_marker` — body contains `SUPERSEDED`, `superseded by`, `replaced by`, or `(Note:` markers indicating self-deprecation
   - `orphan` — exists but not linked from MEMORY.md
   - `dangling` — linked from MEMORY.md but file missing
   - `old_45d` — mtime older than 45 days
   - `old_90d` — mtime older than 90 days

These flags are *signals*, not verdicts. Phase 3 turns them into recommendations.

### Phase 3: LLM judgment (cross-read)

For every flagged file (any flag set), read its frontmatter + first ~30 lines and classify as one of:

- **keep** — pattern memories, user feedback, current operational state. Default for `feedback_*`, named patterns, and the most recent `*_landed` doc per project surface.
- **delete** — superseded markers, CK snapshots older than the most recent two, files whose `name`/`description` is fully covered by the project's `CLAUDE.md`, "X landed" docs older than 7 days where the design rules survive in pattern memories.
- **compress** — files with unique content but verbose framing; output a one-paragraph rewrite.
- **unsure** — content needs user judgment. Surface verbatim with a short question.

**Cross-check against project CLAUDE.md:**

```bash
grep -F -i "$NAME_FROM_FRONTMATTER" "$CWD/CLAUDE.md" 2>/dev/null
```

If the memory's `name` or first-line content appears in CLAUDE.md, lean toward `delete` (with rationale: "duplicate with CLAUDE.md → drift risk").

**Recency rule for `*_landed` docs:** group by surface (e.g., `agnt29_*`, `agnt32_*` are AGNT-related landed docs). Keep the most recent in each group; older ones are candidates for delete unless they encode unique content not covered elsewhere.

### Phase 4: Approval gate (HARD STOP)

Present the plan as a single markdown response with three buckets:

```markdown
## Cleanup plan for review

### Bucket A — high-confidence delete (N files)
| File | Why |
|---|---|
| ... | ... |

### Bucket B — duplicates with CLAUDE.md (N files)
| File | Where it lives in CLAUDE.md |
|---|---|
| ... | ... |

### Bucket C — superseded / aged out (N files)
| File | Reason |
|---|---|
| ... | ... |

### Bucket D — compress (N files)
| File | Proposed one-paragraph rewrite |
|---|---|
| ... | ... |

### Unsure / ask user (N files)
| File | Question |
|---|---|
| ... | ... |

**Net effect:** -N files, MEMORY.md tightens from XKB to ~YKB, ~Z fewer tokens auto-loaded per session.

→ Approve as-is, or call out files to keep, before I touch anything.
```

**STOP HERE.** Do not invoke any `rm`, `Edit`, or `Write` tool until the user has explicitly approved. If the user replies with corrections ("keep file X", "delete Y too", "actually compress instead"), update the plan and present again — still no execution. Only proceed to Phase 5 on a clear "approve" / "proceed" / "yes go ahead" signal.

In **audit mode** the skill stops here regardless and reports completion — no approval question, no execution.

### Phase 5: Execute and verify

Once approved:

1. Delete the approved files using `rm` (single batched command if possible).
2. For `compress` decisions: rewrite the file in place with the agreed paragraph.
3. **Read MEMORY.md once** (required before Write).
4. Rewrite MEMORY.md as the tightened index:
   - Group entries by frontmatter `type` (or by topical sections if types aren't consistent): "Current operational state", "Design patterns", "Gotchas / references", "User feedback (preferences)", "Cross-references".
   - Each entry: `- [filename](filename) — one-line hook ≤150 chars`. Strip embedded summaries; the hook is what the user sees in the index, not the memory's full content.
   - Preserve any cross-reference section pointing into other projects' memory dirs.
5. Run the verification block:

```bash
cd "$MEM_DIR"
echo "=== file count ==="; ls *.md | wc -l
echo "=== MEMORY.md size + lines ==="; wc -c -l MEMORY.md
echo "=== link integrity ==="
grep -oE '\]\([A-Za-z][A-Za-z0-9_-]*\.md\)' MEMORY.md | sed 's/[)(]//g; s/^]//' | while read f; do
  [ -f "$f" ] || echo "MISSING: $f"
done
echo "=== orphans (existing files not linked from MEMORY.md) ==="
comm -23 <(ls *.md | sort) <({ echo MEMORY.md; grep -oE '\]\([A-Za-z][A-Za-z0-9_-]*\.md\)' MEMORY.md | sed 's/[)(]//g; s/^]//'; } | sort)
```

If any `MISSING:` lines appear, the new MEMORY.md is broken — fix immediately. If orphans appear, decide per file: add the link back, or delete the orphan (with user confirmation).

6. Report the final numbers: files before/after, MEMORY.md bytes before/after, estimated tokens saved per session (~bytes/3.5).

## Recovery

If the user disagrees with deletions:

```bash
git -C ~/okfmem-store restore .
```

Restores everything in the symlinked memory dir. Only works if the memory dir is git-backed (which the user's `~/okfmem-store` setup ensures). Skill must check this in Phase 1 and surface it; if memory dir is *not* git-backed, the approval gate requires extra confirmation language ("this is not recoverable via git").

## Style guarantees

- One-line MEMORY.md hooks ≤150 chars (the user's stated convention).
- Never delete a file that has a frontmatter `type: feedback` without explicit user override — feedback memories encode user preferences and are durable by design.
- Never delete a file whose frontmatter `name` is referenced verbatim by another *kept* file's body (cross-reference cluster).
- The most recent `_landed` / `_operational` doc per surface always survives unless explicitly listed for deletion.

## Anti-patterns

- **Executing before approval.** Phase 4 is a hard stop. The skill exists because manual cleanup loses files when the index isn't updated atomically; the same risk applies even when an LLM is driving.
- **Deleting frontmatter `type: feedback` files** without explicit user instruction. These are user preferences — durable by design.
- **Updating MEMORY.md before deletions complete**, or vice versa. The two operations are atomic from the user's perspective; do them as the last two steps of Phase 5.
- **Treating the inventory script's flags as verdicts.** Flags are signals; the LLM judgment in Phase 3 is the verdict.
- **Inventing file deletions outside the approved plan.** If the user approves Bucket A but Phase 5 reveals extra junk, surface it as a follow-up — don't fold it in silently.

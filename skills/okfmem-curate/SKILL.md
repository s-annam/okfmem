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

**Page deletion and archival are store hygiene and recall precision — not a context optimization (#52).** Only `MEMORY.md` and `STATE.md` are auto-loaded at session start; every other page costs zero context regardless of how many exist. A curate pass that deletes or archives 100 pages and touches no pointer saves **0** tokens. The number that moves the needle is auto-loaded bytes — see Phase 2 and Phase 4.

Applies the "deterministic collection + LLM judgment" principle: a script collects facts, then an LLM cross-reads each candidate and produces verdicts. **Hard rule:** no file is deleted and no index is rewritten until the user has explicitly approved the plan.

## When to use

- The user says "clean up memory", "prune memory", "memory hygiene", "tighten MEMORY.md", "context is bloated", or similar.
- Periodic curation (monthly, or after a project reaches a milestone where many "X landed" memories accumulate).
- **Not** the byte-ceiling trigger — when `okfmem status` or `okfmem reindex --report` flags `MEMORY.md` as over the auto-load byte ceiling (`memory_reindex.MEMORY_BUDGET_BYTES`, 8192 bytes), that is `/okfmem-reindex`'s trigger, not this skill's: the remedy is a lane split (a pointer move), not a deletion. Phase 2 below still surfaces the same numbers, because a curate pass benefits from knowing them too, but this skill does not execute the split.
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

**Auto-loaded bytes — the headline numbers.** Run the reindex engine first.
Its `auto_loaded` table is the only thing that costs context at session start
(`MEMORY.md` + `STATE.md` against their ceiling), and its per-section
breakdown of `MEMORY.md` shows **which section holds the bytes** — a single
dominant block is the finding that decides whether the remedy is "tighten a
few hooks" or "split a lane"; total file size alone never shows it:

```bash
python3 ~/okfmem/okfmem reindex --report "$MEM_DIR"
```

**If the `MEMORY.md` row reads `OVER`, the restructure trigger has fired
(#53) — but that trigger is `/okfmem-reindex`'s to act on, not this
skill's.** The recommended remedy is a lane split, never "tighten hooks":
moving a lane's pointers out of `MEMORY.md` into a new or existing
`MEMORY-<lane>.md` index recovers far more bytes than shortening a handful
of hooks, and once a pointer moves to a lane index it is never re-flattened
back to the root. `/okfmem-reindex` clusters the split, proposes it for
approval, and executes it — see that skill for the full process. Report
the `OVER` finding here (it belongs in this skill's numbers too, since a
curate pass often runs on the same store), but hand the split itself to
`/okfmem-reindex` rather than doing it inline.

Also count pointer lines over the per-line budget (#52 — `okfmem-save`
enforces ≤150 chars at write time; this is the check that catches what
slipped through):

```bash
python3 ~/okfmem/okfmem reindex --budget-check "$MEM_DIR"
```

It walks **every** index, not just the root — after a lane split (or once
#53's write-time routing is in effect) most pointers live in lane indexes,
so checking `MEMORY.md` alone would miss almost all of them — and it accepts
**both** pointer syntaxes, so the bare `- slug.md — hook` form a lane index
uses is counted too. It reports each over-budget line with its index file,
line number, and character count, and it is **advisory**: always exit 0,
never a gate (the budget rule itself is advisory — see `okfmem-save` Step 3).

> Do not hand-roll this as a shell one-liner. The `awk` version this replaced
> was wrong in both directions at once: a `/^- \[/` guard is blind to every
> bare pointer (exactly the ones a lane index holds), and BSD `awk`'s
> `length($0)` counts **bytes**, so the convention's em-dash over-reported
> every line carrying one. Same fail-open-on-macOS class as the old `sed`
> verifier below. The budget constant lives in one place —
> `memory_reindex.POINTER_BUDGET_CHARS`.

Lead the curation report (Phase 4) with these numbers: `MEMORY.md` /
`STATE.md` bytes vs. ceiling, and pointer overage count.

**Page-level detail — store hygiene, not context.** Run the inventory script
for per-file age, link status, and heuristic flags:

```bash
python3 ~/okfmem/skills/okfmem-curate/scripts/inventory.py "$MEM_DIR"
```

Page count and on-disk total bytes are **not auto-loaded and cost zero
context at session start** — demote them to context for the plan, not a
finding. Deleting or archiving a page changes none of the headline numbers
above unless it also removes or shortens a `MEMORY.md` pointer.

The inventory script emits a markdown report with three sections:

1. **Summary**: page count, total bytes, MEMORY.md size + line count, per-index pointer counts.
2. **Per-file table**: name, size, age (days since mtime), frontmatter `type` if present, frontmatter `name`, link status (linked / orphan). Link status is computed across **every** `MEMORY*.md` in both pointer syntaxes — a page carried by a lane index is linked, not an orphan.
3. **Heuristic flags**: per file, comma-separated flags drawn from filename + content patterns:
   - `ck_snapshot` — filename starts `ck_` (retired CK session-end saves; ephemeral by nature). These are *never* counted as orphans — they were never indexed by design, and every engine pass (`consolidate`, `backfill`, `reindex --verify`) skips them for the same reason. This skill is the one pass that still surfaces them, so a plan can propose deleting them.
   - `landed_doc` — filename or frontmatter `name` contains `landed` / `_operational` (project-state, prone to age out)
   - `superseded_marker` — body contains `SUPERSEDED`, `superseded by`, `replaced by`, or `(Note:` markers indicating self-deprecation
   - `orphan` — a durable page that exists but is linked from no index (`MEMORY.md` *or* any `MEMORY-*.md` lane index); `ck_*.md` snapshots are excluded
   - `dangling` — linked from an index but file missing (reported with the index it came from)
   - `old_45d` — mtime older than 45 days
   - `old_90d` — mtime older than 90 days

These flags are *signals*, not verdicts. Phase 3 turns them into recommendations.

### Phase 3: LLM judgment (cross-read)

For every flagged file (any flag set), read its frontmatter + first ~30 lines and classify as one of:

- **keep** — pattern memories, user feedback, current operational state. Default for `feedback_*`, named patterns, and the most recent `*_landed` doc per project surface.
- **delete** — superseded markers, CK snapshots older than the most recent two, pure-noise files whose `name`/`description` is fully covered by the project's `CLAUDE.md` (no unique content worth keeping), "X landed" docs older than 7 days where the design rules survive in pattern memories.
- **graduate** — a *still-valuable* page whose rule belongs in the always-loaded `CLAUDE.md` (not recall-on-demand memory): promote it into `CLAUDE.md`/`AGENTS.md` and archive the source with provenance, rather than deleting it. This is the forward, lossless alternative to `delete` for the duplicate-with-CLAUDE.md case — run `okfmem graduate <slug> [--to <dir>/CLAUDE.md]` (it distills + places, mirrors a real-file `AGENTS.md`, archives the page stamped `graduated_to`, and drops its `MEMORY.md` pointer). Reserve `delete` for duplicates with no unique content.
- **compress** — files with unique content but verbose framing; output a one-paragraph rewrite.
- **unsure** — content needs user judgment. Surface verbatim with a short question.

**Cross-check against project CLAUDE.md:**

```bash
grep -F -i "$NAME_FROM_FRONTMATTER" "$CWD/CLAUDE.md" 2>/dev/null
```

If the memory's `name` or first-line content appears in CLAUDE.md, the page is a drift risk. Split by value: a page with unique, still-valuable rules → **graduate** (promote + archive, lossless, keeps provenance); a pure-noise duplicate with nothing worth keeping → **delete**. Prefer `graduate` whenever the content still carries a rule worth having in `CLAUDE.md` — deleting it loses both the content and where it came from.

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
Recommended action per file: **graduate** a still-valuable page (promote into `CLAUDE.md`/`AGENTS.md` + archive with provenance via `okfmem graduate <slug>`), **delete** only a pure-noise duplicate. Graduate is lossless and keeps provenance; prefer it whenever the rule is worth keeping.

| File | Where it lives in CLAUDE.md | Action (graduate / delete) |
|---|---|---|
| ... | ... | ... |

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

**Net effect:** ~Z fewer tokens auto-loaded per session (MEMORY.md XKB → ~YKB; STATE.md unchanged unless also rewritten) — reads **0** if no auto-loaded file changes size. -N files archived/deleted is a store-hygiene count, reported separately; it is not a context saving by itself.

→ Approve as-is, or call out files to keep, before I touch anything.
```

**STOP HERE.** Do not invoke any `rm`, `Edit`, or `Write` tool until the user has explicitly approved. If the user replies with corrections ("keep file X", "delete Y too", "actually compress instead"), update the plan and present again — still no execution. Only proceed to Phase 5 on a clear "approve" / "proceed" / "yes go ahead" signal.

In **audit mode** the skill stops here regardless and reports completion — no approval question, no execution.

### Phase 5: Execute and verify

Once approved:

1. Delete the approved files using `rm` (single batched command if possible).
2. For `compress` decisions: rewrite the file in place with the agreed paragraph.
3. For every file the approved plan marked **graduate**, run `okfmem graduate <slug> --yes` (add `--to <dir>/CLAUDE.md` for a lane-scoped target). Phase 4 **is** the human approval gate, so pass `--yes` to skip graduate's own rung-2 `[y/N]` — without it, invoked non-interactively with no tty the prompt silently skips and prints only a manual hint, so nothing lands while Phase 5 would wrongly report success. Each run distills the rule into `CLAUDE.md` (mirroring a real-file `AGENTS.md`), archives the source page stamped `graduated_to`, and drops its `MEMORY.md` pointer — so run these **before** the MEMORY.md rewrite below.
4. **Read MEMORY.md once** (required before Write).
5. Rewrite MEMORY.md as the tightened index:
   - Group entries by frontmatter `type` (or by topical sections if types aren't consistent): "Current operational state", "Design patterns", "Gotchas / references", "User feedback (preferences)", "Cross-references".
   - Each entry: `- [filename](filename) — one-line hook ≤150 chars`. Strip embedded summaries; the hook is what the user sees in the index, not the memory's full content.
   - Preserve any cross-reference section pointing into other projects' memory dirs.
6. Run the verification block:

```bash
python3 ~/okfmem/okfmem reindex --verify "$MEM_DIR"; echo "verify exit: $?"
python3 ~/okfmem/okfmem reindex --report "$MEM_DIR"    # after-numbers for step 8
```

`--verify` walks **every** `MEMORY*.md` (not just the root index) and accepts
**both** pointer syntaxes — `[title](slug.md)` and the bare `- slug.md — hook`
a lane index uses. It exits **0** when the index is intact and **1** on any
dangling pointer or orphan, so this line is a real gate: a non-zero exit means
the rewrite broke the index — fix it before reporting success. Each dangling
pointer is named **with the index file it came from**, which is what makes it
fixable.

> This used to be a `grep | sed | comm` pipeline, and it was wrong in the
> unsafe direction: it read only `MEMORY.md`, only the rich link form, and its
> BRE `\?` is a GNU extension that BSD `sed` (macOS) does not support — so on
> the primary platform it silently failed to strip the prefix and reported
> nothing. A verifier that fails open is worse than no verifier. The logic
> lives in Python now (issue #54) precisely so it cannot diverge per platform.
> If you ever hand-roll this again: anchor on the **link target**, never a
> loose `- \[\?` prefix, or a pointer whose *title* starts with a filename
> (`- [CLAUDE.md subdir lanes](real-slug.md)`) reports a phantom `CLAUDE.md`.

If any dangling pointers appear, the new MEMORY.md is broken — fix immediately.
If orphans appear, decide per file: add the link back, or delete the orphan
(with user confirmation).

7. Verify each **graduate** with the same rigor as delete/compress — confirm the rule actually landed and the source actually moved:

```bash
for slug in $GRADUATED_SLUGS; do
  echo "=== $slug: rule landed in CLAUDE.md ==="
  git -C "$REPO_ROOT" diff -- CLAUDE.md AGENTS.md | grep -q '^+' && echo "OK: CLAUDE.md/AGENTS.md gained the rule" || echo "MISSING: no CLAUDE.md diff for $slug"
  echo "=== $slug: source archived, not left live ==="
  [ ! -f "$MEM_DIR/$slug.md" ] && [ -f "$MEM_DIR/archive/$slug.md" ] \
    && grep -q 'graduated_to:' "$MEM_DIR/archive/$slug.md" \
    && echo "OK: moved to archive/ with graduated_to stamp" \
    || echo "BROKEN: $slug not cleanly archived (still live, missing, or unstamped)"
done
```

A `MISSING:`/`BROKEN:` line here means the graduate did not apply (commonly: `--yes` was omitted so the non-interactive `[y/N]` silently skipped) — re-run `okfmem graduate <slug> --yes` and re-verify before rewriting MEMORY.md's counts.

8. Report the final numbers from the step-6 `okfmem reindex --report` run, in this order: (1) `MEMORY.md` + `STATE.md` bytes before/after against the ceiling and the resulting tokens saved per session (~bytes delta / 3.5) — the number that matters; (2) pages before/after, labelled non-context — store hygiene, not a context saving. "We deleted N files" must never be presented as a context saving on its own; say so only alongside a `MEMORY.md`/`STATE.md` byte drop.

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

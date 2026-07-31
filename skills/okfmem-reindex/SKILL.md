---
name: okfmem-reindex
description: "Topology restructuring for a bloated MEMORY.md — cluster flat pointers into lane indexes, rewrite MEMORY.md as a routing table with 'covers' clauses, verify link integrity. Not deletion (see okfmem-curate); nothing is lost, pointers only move. Hard approval gate before any write. Invoked as /okfmem-reindex."
origin: user
---

# okfmem Reindex

> **Names.** Canonical `/okfmem-reindex`. This skill lives in the `okfmem`
> repo (`~/okfmem/skills/`); `okfmem init` symlinks it into each harness
> alongside `/okfmem`, `/okfmem-save`, and `/okfmem-curate`.

Restructure a flat `MEMORY.md` that has grown past the auto-load byte ceiling
into a **two-tier index**: a small root `MEMORY.md` that reads as a routing
table, plus one or more `MEMORY-<lane>.md` lane indexes holding the actual
pointer bulk. Nothing is deleted, archived, or rewritten in content — every
pointer that moves still points at the same page. This is a topology fix, not
a hygiene pass.

## Why this is a separate skill from `okfmem-curate`

`okfmem-curate` answers "is this content still worth keeping" — it deletes,
archives, and compresses. This skill answers "is this content filed in the
right place" — it only moves pointers between index files. The two operations
have opposite risk profiles:

| | `okfmem-curate` | `/okfmem-reindex` |
|---|---|---|
| Operation | **deletes** pages | **moves** a pointer between index files |
| Reversibility | hard; git-backed store is the only recovery | nothing is lost; no page is touched |
| Gate | hard approval stop, deliberately rare | propose → approve → execute; can be routine |
| Framing | rot | topology |

A store can come back from a full curate audit completely clean — zero
superseded markers, zero dangling pointers, every page recent — and still
have a `MEMORY.md` several times over budget. That is not a curation problem;
curate has no vocabulary for it. Folding a non-destructive move behind
curate's hard stop would also make it rarer than it deserves to be, since a
reversible operation does not need the friction a destructive one does.

**Never re-flatten. The remedy for an over-budget `MEMORY.md` is a lane
split, never tighter hooks** — see `skills/okfmem-save/SKILL.md` Step 3
("Lane routing") and `skills/okfmem-curate/SKILL.md` Phase 2 for the full
statement of this rule and the byte-savings ratio between the two remedies.
This skill exists to execute that split, not to restate the rule.

**Pages on disk cost nothing.** Only `MEMORY.md` and `STATE.md` are
auto-loaded at session start; every other page — including every lane
index's pointer targets — costs zero context regardless of how many exist.
This skill never proposes deleting or consolidating pages as a context
optimization; for context, that is a no-op by construction. Consolidation is
`okfmem-curate`'s domain, not this skill's, even when a lane happens to
surface duplicate-looking content along the way.

**`type: feedback` pointers stay at the root.** User preferences are durable
and high-hit-rate — they are not a lane, regardless of how a byte-count
argument might read.

## When to use

- `okfmem status` or `okfmem reindex --report` flags a project's `MEMORY.md`
  as `OVER` the auto-load byte ceiling (`memory_reindex.MEMORY_BUDGET_BYTES`,
  8192 bytes).
- The user says "reindex memory", "split MEMORY.md", "the index is too big",
  or similar.
- **Audit-only check-in**: when the user wants the cluster proposal without
  committing to execution yet — invoke with the `audit` argument.

## Modes

| Mode | Trigger | What it does |
|------|---------|--------------|
| Default | "reindex memory" / "split the index" / no arg | All five phases; halts at approval gate; executes on approval |
| Audit | `audit` arg, or "memory reindex audit" / "what would you split" | Phases 1–3 only; presents the cluster proposal and stops; no approval question, no execution |

## How it works

Five phases, mirroring `okfmem-curate`'s shape so the two feel like
siblings. Phase 4 is the only phase that writes to the store, and it is
gated on explicit user approval in Phase 3 — never create or rewrite an
index file before that approval.

### Phase 1: Measure

Resolve the target memory dir (same convention as `okfmem-curate` Phase 1 —
current project by default, or an explicit path argument) and call the
engine:

```bash
python3 ~/okfmem/okfmem reindex --report "$MEM_DIR" --json
```

Read the `auto_loaded` row for `MEMORY.md`: bytes, ceiling, `over`. Read the
`sections` array — the per-section byte breakdown of `MEMORY.md` — to see
**which block holds the bytes**, not just the total. A single dominant
section is the finding that makes a lane split worthwhile; several small,
evenly-sized sections may not be.

**If `MEMORY.md` is under budget and no section dominates, report a no-op
and stop.** Do not propose a split for its own sake, and do not pad the
report into busywork to justify having run — "this store is fine" is a
complete and correct answer. Re-running this skill on a store it already
reindexed should reach this branch and report no changes.

### Phase 2: Cluster

Read every pointer currently in `MEMORY.md` (and any existing lane indexes,
if this is a re-run adding to a prior split) and propose a set of lanes.

**Explicitly consider a cross-cutting lane, not only subsystem-shaped
ones.** The natural first pass groups pointers by directory/subsystem/slug
prefix — that heuristic finds real lanes, but it can also miss the largest
one. A theme that cuts across every subsystem (recurring workflow
conventions, a class of gotcha that shows up in every area, a review or
verification discipline) has no shared prefix or directory to key on, so a
subsystem-first pass silently shreds it across the lanes it should have
been its own. Read the pointer hooks themselves for a recurring *kind* of
fact, not just a recurring *area* of the codebase, before finalizing the
lane list.

For each proposed lane, write the **"covers" clause** it will carry in the
root routing table — the one or two sentences that let a session model
decide whether to open that lane index, without opening it. If you cannot
write a tight covers clause for a lane, its boundary is probably wrong; fold
it into a neighboring lane or reconsider the split.

Decide, per pointer, whether it stays at the root instead of moving to a
lane:

- Genuinely cross-lane facts (project identity, deploy topology, a product
  invariant that every lane's work touches) — root.
- `type: feedback` pages — root, always (see rule above).
- Everything else — its lane.

### Phase 3: Propose (HARD STOP)

Present the plan as a single markdown response:

```markdown
## Reindex plan for review

### Proposed lanes

| Lane index | Covers | Pointers moving in |
|---|---|---|
| MEMORY-<lane>.md | <the covers clause> | N |

### Staying at root

| Pointer | Why |
|---|---|
| ... | cross-lane / type: feedback |

### Projected bytes

| File | Before | After |
|---|---|---|
| MEMORY.md | X | Y |
| MEMORY-<lane>.md (new/extended) | — | Z |

→ Approve as-is, adjust lane boundaries, or call out pointers to keep at
the root, before anything is written.
```

**STOP HERE.** Do not invoke `Write` or `Edit` on any index file until the
user has explicitly approved. This gate is lighter in *stakes* than
`okfmem-curate`'s — nothing is destroyed, and a bad split is recoverable by
re-running this skill — but it is not lighter in *presence*: the carve is
the entire product of this skill, and an unreviewed one is tedious to
unwind by hand across every affected index. If the user proposes
corrections ("merge lane X and Y", "keep pointer N at root"), update the
plan and present again — still no execution.

In **audit mode** the skill stops here regardless and reports the proposal
as-is — no approval question, no execution. Audit therefore runs Phases 1–3
(the proposal table above is a Phase 3 artifact) and writes nothing: every
write in this skill lives in Phase 4.

### Phase 4: Execute

Once approved:

1. For each new lane, `Write` `MEMORY-<lane>.md` using the pointer-line
   format already in use (rich `- [Title](slug.md) — hook` or bare
   `- slug.md — hook`, matching what the pointer already used at the root).
   Head the file with a one- or two-line note stating **when and why** it
   was split out (date + the byte/dominant-block finding from Phase 1) —
   this is what lets a future session (or a future run of this skill)
   understand the lane's origin without re-deriving it.
2. For each extended (pre-existing) lane, `Edit` it to append the moving
   pointers, same format.
3. **Read `MEMORY.md` once** (required before `Write`), then rewrite it as
   the routing table: a row per lane index with its "covers" clause, the
   cross-lane facts, and the `type: feedback` pointers — nothing else. This
   is the file every session auto-loads, so it should read as a map, not a
   page list.
4. **Every lane index must be linked from the root routing table.** An
   index with **no inbound link from any other index** is an *orphan index*
   and Phase 5's `--verify` will fail on it — the mechanism that catches a
   lane created and then never referenced. Note the rule here is stricter
   than the check: `--verify` only asks whether *some* index links the lane,
   so a chain (`MEMORY.md → MEMORY-a.md → MEMORY-b.md`) passes with zero
   orphan indexes even though `MEMORY-b.md` has no routing-table row. Route
   every lane from the root anyway — the root is the only file a session
   auto-loads, and a lane reachable only two hops in is a lane nobody opens.
5. **Never re-flatten.** A pointer that already lives in a lane index is
   never moved back to `MEMORY.md` by this skill, including when a lane
   turns out smaller than expected after the move — resize the lane
   structure (merge two small lanes, rename one), don't undo the split.

### Phase 5: Verify

```bash
python3 ~/okfmem/okfmem reindex --verify "$MEM_DIR"; echo "verify exit: $?"
python3 ~/okfmem/okfmem reindex --report "$MEM_DIR"    # after-numbers
```

`--verify` exits **0** when every `MEMORY*.md` in the dir is intact and
**1** on any dangling pointer, orphan page, or orphan index. Treat non-zero
as failure — the reindex is broken, not merely imperfect — and fix it
before reporting success. A dangling pointer is reported with the index
file it came from. An *orphan index* is one with **no inbound link from any
other index** — a lane created in step 1/2 above and then referenced by
nothing at all; if `--verify` reports one, the fix is adding its
routing-table row to `MEMORY.md`, not deleting the lane. Because the check
accepts an inbound link from *any* index, it will not catch a lane linked
only from another lane — step 4's root-routing rule is what covers that, and
it is on you, not on `--verify`.

Report, in this order: (1) `MEMORY.md` bytes before → after against the
ceiling, and the lane index(es) created/extended with their byte totals;
(2) the verify result (dangling / orphan / orphan-index counts, ideally all
zero). Do not report a page or archive count here — that is
`okfmem-curate`'s number, not this skill's; a reindex that moves pointers
without deleting anything changes zero pages on disk.

## Anti-patterns

- **Executing before Phase 3 approval.** The carve is the whole product;
  writing it unreviewed defeats the point of proposing it.
- **Re-flattening a lane pointer back to `MEMORY.md`** on a later run,
  including "just to clean up" or "it's a small lane now" — resize lane
  structure instead (see Phase 4, step 5).
- **Proposing page deletion or consolidation as part of a reindex plan.**
  That is `okfmem-curate`'s domain; for context cost it is a no-op (pages on
  disk cost nothing). If a reindex pass surfaces genuinely stale content,
  name it as a follow-up for `/okfmem-curate`, don't fold it into this
  skill's plan.
- **A subsystem-only clustering pass.** Always check for a cross-cutting
  lane before finalizing Phase 2 — see the rationale above.
- **Creating a lane index without a routing-table row in `MEMORY.md`.**
  Phase 5's `--verify` catches the blatant case (a lane no index links at
  all) as an orphan index, but it does not catch a lane linked only from
  another lane — so this one is on the Phase 4 checklist, not on the tool.
- **Padding a no-op into a plan.** If Phase 1 finds `MEMORY.md` under
  budget with no dominant section, say so and stop — a proposed split with
  no real justification behind it is worse than no action.

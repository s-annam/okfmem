# okfmem — self-maintaining OKF markdown memory engine

The **engine + CLI** for a plain-markdown, git-portable memory system for AI
coding agents. Storage is [Google's Open Knowledge Format (OKF) v0.1][okf] —
one markdown page per topic, YAML frontmatter, plain-markdown links, no DB, no
server. Any harness reads it via `grep`.

**Engine ⇄ store split** (like `chezmoi` vs your dotfiles — tool shared, data
yours):

| Repo | Is | Contents |
|---|---|---|
| **`okfmem`** (this repo) | the engine + CLI | `memory_backfill.py`, `memory_init.py`, `okfmem` CLI |
| **`<user>/okfmem-store`** | the data (private) | `projects/*/`, `archive/`, `MEMORY.md` indexes, `registry.json`, per-project `STATE.md` |

The engine locates the store via, in order: `--store PATH`, `$OKFMEM_STORE`,
then `~/okfmem-store`.

## Commands

```bash
okfmem backfill    [--dry-run]  # P1 — stamp decay frontmatter on every page
okfmem init        [--dry-run]  # P2 — write harness pointers + build registry
okfmem consolidate [--dry-run]  # P3 — decay-score, archive stale, regen, push
okfmem status                   # what's wired + drift
```

(`okfmem <cmd>` is a thin wrapper over `python3 memory_<cmd>.py` — the modules
run standalone too.)

## What each component does

### `memory_backfill.py` (P1) — decay frontmatter

One-shot, idempotent stamp of maintenance metadata onto every durable page:

```yaml
type: project          # unchanged: user | feedback | project | reference
importance: 6          # deterministic by type: user/feedback=10, project=6, reference=3
pinned: false          # user/feedback (and unknown types) => true, decay-exempt
created: 2026-04-29     # git first-commit date (bulk-resolved), else file mtime
last_accessed: 2026-04-29
access_count: 0
status: active
```

Byte-preserving (no PyYAML dependency). Skips `ck_*` (retired snapshots),
`MEMORY.md`/`STATE.md`/`CONTEXT.md`, and pages without frontmatter or a
top-level `type:`. Unknown (non-4-enum) types like `person` are stamped
`pinned: true` so nothing unclassified is ever archived. Re-running is a no-op.

### `memory_init.py` (P2) — harness init + registry

One-time (re-runnable to repair), cross-platform:

1. Detects harnesses: Claude Code (`~/.claude/`), Antigravity (`~/.gemini/` /
   `agy`).
2. Writes a managed `<!-- MEMORY-POINTER v1 -->…<!-- /MEMORY-POINTER -->` block
   into each harness's global slot (`~/.claude/CLAUDE.md`,
   `~/.gemini/config/AGENTS.md`), edited in place — grep-on-demand protocol.
3. Builds `<store>/registry.json` mapping absolute git-root → project (default
   `basename(git-root)`; deviations recorded as overrides). Source of truth is
   the `~/.claude/projects/<encoded>/memory` symlink set; the encoded dir is
   decoded by filesystem probe (dir names may contain `-`).
4. Scans registered project roots' `CLAUDE.md`/`AGENTS.md`/`CLAUDE.local.md`
   for retired-system references (memgraph, ck, projector, …) — **detection +
   report only**; rewriting is gated behind `--apply-cleanup` (deferred to
   hand-review).

`--status` / `okfmem status` prints wiring + drift.

### `memory_consolidate.py` (P3) — sleep-time consolidation

Runs at Claude Code session end (Stop hook) or by hand. Per run:

1. **Access tracking** — parse the session transcript (`--transcript PATH`, or
   `--stdin-hook` to pull `transcript_path` from the Stop-hook JSON on stdin)
   for reads/greps of any `projects/*/` page, matching both the store path and
   the `~/.claude/projects/<enc>/memory/…` symlink spelling. Touched pages get
   `last_accessed = today`, `access_count += 1`. Best-effort: no transcript ⇒
   pure age decay.
2. **Decay scoring** — `R = exp(-t_days / S)`, `S = access_count + 1`.
3. **Graceful archival (never delete)** — move page → `projects/<proj>/archive/`,
   set `status: archived` + `archived_on`, drop its `MEMORY.md` line, iff
   `not pinned AND type∉{user,feedback} AND t_days>30 AND R<0.40 AND age>14d`,
   capped at `--cap` (default 20)/run, lowest-R first.
4. **Commit + push** the store (skip with `--no-commit` / `--no-push`).

**Cold-start guard (decay epoch).** Backfill seeded `last_accessed = created`
(git-creation date), so at go-live every never-re-read page would look
maximally decayed and drain into archive. The archival timer therefore measures
against `max(last_accessed, epoch)`, where `epoch` is the day tracking went live
(persisted in `<store>/decay_state.json`, written on first apply run). Every
page gets a fair 30-day post-go-live window to prove usage.

Guardrails: `--dry-run` writes nothing; never `rm` (archive/ + git = backstop);
never touches `type: user|feedback` or `pinned: true`; refuses to run in apply
mode if the store tree is already dirty (unless `--force`/`--no-commit`).

## Decay math

| Quantity | Formula | Default |
|---|---|---|
| Importance | deterministic by `type` | user/feedback=10, project=6, reference=3 |
| Retention | `R = exp(-t_days / S)`, `S = access_count + 1` | archive gate `R < 0.40` |
| Half-life | `≈ 0.693 · S` days | S=1 → ~0.5 mo; S=4 → ~2.8 mo |
| Age gate | `t_days > 30` (vs `max(last_accessed, epoch)`) AND `age > 14d` | plain-language rule |
| Archive cap | ≤ N pages/run | 20 |

## Status

- **P1 — decay frontmatter + backfill** ✅
- **P2 — init wrapper (pointers + registry + stale-ref detection)** ✅
- **P3 — consolidation job** (`memory_consolidate.py`) ✅ — dry-run tuned; Stop hook wiring below
- **P4 — retire manual `/memory-curate`; rewrite continuity blocks** — not yet built

### Wiring the Stop hook (P3 go-live)

Add to `~/.claude/settings.json` `hooks.Stop` (runs after each session ends):

```json
{ "hooks": { "Stop": [ { "hooks": [ {
  "type": "command",
  "command": "python3 ~/okfmem/memory_consolidate.py --stdin-hook"
} ] } ] } }
```

Dry-run across the whole store first (`okfmem consolidate --dry-run`) — at
go-live it should report 0 archive candidates (epoch guard).

Design + research: `okfmem-store/design/memory-v2-self-maintaining-design.md`
and [s-annam/tools#19].

[okf]: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[s-annam/tools#19]: https://github.com/s-annam/tools/issues/19

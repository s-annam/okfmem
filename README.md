# okfmem — self-maintaining OKF markdown memory engine

**The Problem:** There is a fundamental difference between an *append-only log* and a true *memory system*. While agents like Claude Code have native memory features (e.g., auto-loading a `MEMORY.md` index), they lack lifecycle management. Over time, they act as endless scratchpads that accumulate clutter, ultimately degrading reasoning ("Lost in the Middle"). Conversely, advanced frameworks like Hermes solve this with dedicated tiered memory, but they rely on local databases (like SQLite) and are tightly coupled to their own ecosystems.
**The Solution:** `okfmem` brings a Hermes-style, self-maintaining memory architecture to your existing CLI agents. By sitting on top of native features (like Claude Code's file reading), it adds mathematical decay, Open Knowledge Format (OKF) metadata, and automatic archival to keep your agent's context window lean. Like Hermes, it also provides an opt-in SQLite full-text search index over your past sessions—but safely isolated as a rebuildable, git-ignored local cache.

Storage uses [Google's Open Knowledge Format (OKF) v0.1][okf] — one markdown page per topic, YAML frontmatter, plain-markdown links. No database, no server.

## Architecture: Engine ⇄ Store Split

Just like `chezmoi` separates the tool from your dotfiles, `okfmem` separates the engine from your private data.

| Repo | Role | Contents |
|---|---|---|
| **`okfmem`** (this repo) | The **Engine** (Public) | The scripts and CLI (`memory_*.py`, `okfmem`) |
| **`<user>/okfmem-store`** | The **Store** (Private) | Your data: `projects/*/`, `archive/`, `MEMORY.md`, `STATE.md` |

By keeping them separate, your data never leaves your machine unless you push it to a private repo.

## Quickstart

Requirements: Python 3 (stdlib only — no dependencies) and `git`. Runs natively on macOS and Linux. For Windows, run these commands inside **WSL (Windows Subsystem for Linux)** or **Git Bash**.

```bash
# 1. Clone the engine (this repo)
git clone https://github.com/s-annam/okfmem.git ~/okfmem
cd ~/okfmem

# 2. Run the automated installer
./install.sh
```

The installer will:
1. Symlink the `okfmem` CLI to `~/.local/bin/okfmem`.
2. Create a local git-backed store at `~/okfmem-store` (if it doesn't exist).
3. Wire the memory system into your AI coding agents (Claude Code, Antigravity, etc.).

Make sure `~/.local/bin` is in your `$PATH`. (e.g., `export PATH="$HOME/.local/bin:$PATH"`).

## How the AI Uses It (Daily Flow)

Once installed, the memory system works transparently with your AI agent.

### 1. Auto-Loading Context (Start of Session)
When the AI starts, it automatically reads two files per project:
*   **`STATE.md` (Active State):** A bounded snapshot of current work, priorities, and context. Overwritten every session.
*   **`MEMORY.md` (Durable Knowledge):** A 200-line index of one-line pointers to deeper knowledge.

### 2. On-Demand Retrieval (During Session)
If the AI needs more context, it `grep`s the durable `<slug>.md` pages referenced in `MEMORY.md`.

### 3. Capture & Sync (End of Session)
The AI is instructed to capture insights into new `<slug>.md` pages and update `STATE.md` before the session ends. 

## How the Engine Maintains It

The `okfmem` CLI handles maintenance so your AI doesn't have to. 

### 1. Decay & Graceful Archival (`okfmem consolidate`)
To prevent context bloat, the system automatically tracks page accesses. If a page isn't read, it decays.
*   **Never Delete:** Stale pages are moved to `archive/`. They are never permanently deleted, ensuring zero data loss.
*   **Math:** Retention `R = exp(-t_days / S)` where `S = access_count + 1`. Pages with `R < 0.40` and age `> 14d` are safely archived.

**Wiring the Stop hook (Automated Archival):**
To run consolidation automatically when your agent finishes a session, add this to your agent's configuration (e.g., `~/.claude/settings.json`):
```json
{ "hooks": { "Stop": [ { "hooks": [ {
  "type": "command",
  "command": "python3 ~/okfmem/memory_consolidate.py --stdin-hook"
} ] } ] } }
```

### 2. Initialization & Wiring (`okfmem init`)
Scans your system for supported harnesses (Claude Code, Antigravity) and writes a managed `<!-- MEMORY-POINTER v1 -->` block into their global prompts so the AI knows where to find the memory. (The `install.sh` script runs this automatically).

### 3. Backfill Metadata (`okfmem backfill`)
An idempotent tool that stamps required YAML frontmatter (like `importance`, `pinned`, `created`) onto all durable pages. (The `install.sh` script runs this automatically).

### 4. Status Check (`okfmem status`)
Run this anytime to view the wiring status, detected harnesses, and if your store has any uncommitted changes.

### 5. Session Search (`okfmem search`)
An opt-in plugin that builds a local SQLite FTS5 index over your agent's past conversation transcripts (e.g., Claude Code or Antigravity logs). Just like Hermes' database layer, this allows your agent to perform deep full-text searches across historical sessions to recover details not currently in `MEMORY.md`. The `.db` is purely a derived local cache—gitignored and rebuildable anytime via `okfmem index`.

```mermaid
flowchart TD
    subgraph Harness [AI Coding Agent]
        A[Agent starts] --> B{Reads \nSTATE.md & MEMORY.md}
        B --> C[Works on task]
        C -->|Greps on demand| D[(okfmem-store)]
        C -->|Writes new learnings| D
    end

    subgraph okfmem [okfmem Engine]
        E[Stop Hook] -->|Triggers| F(okfmem consolidate)
        F -->|Archives stale docs| D
    end
```

## Store Location Override

By default, the store is created at `~/okfmem-store`. To put it elsewhere, set `$OKFMEM_STORE` in your shell profile or pass `--store PATH` to any command.

---
Design & research: `okfmem-store/design/memory-v2-self-maintaining-design.md`.

[okf]: https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md
[memgpt]: https://arxiv.org/abs/2310.08560
[lost-in-the-middle]: https://arxiv.org/abs/2307.03172
[generative-agents]: https://arxiv.org/abs/2304.03442

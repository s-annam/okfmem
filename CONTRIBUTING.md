# Contributing to okfmem

Thanks for your interest. okfmem is a small, dependency-free memory engine for
CLI coding agents — contributions that keep it that way are especially welcome.

## Ground rules

- **Stdlib only.** The core engine (`memory_*.py`, `okfmem`) must run on Python 3
  with no third-party dependencies. Optional plugins may pull extras, but never
  the core.
- **Markdown is the source of truth.** No database or server in the core path.
  Any index (FTS5, vectors) is a derived, gitignored, rebuildable local cache —
  never committed.
- **Never delete durable data.** Decay archives pages (`projects/<proj>/archive/`);
  it must never hard-delete. Keep the supersede-don't-delete invariant.
- **Harness-neutral.** Don't couple the core to any one agent. Harness specifics
  live behind adapters (`plugins/adapters/`).

## Development

```bash
git clone https://github.com/s-annam/okfmem.git ~/okfmem
cd ~/okfmem
./install.sh        # symlinks the CLI, creates a local store, wires harnesses
okfmem status       # verify wiring
```

Try changes against a throwaway store: `OKFMEM_STORE=/tmp/okfmem-test okfmem <cmd>`.

## Pull requests

1. Open (or comment on) an issue first for anything non-trivial — the roadmap
   lives in the issue tracker.
2. Keep PRs focused; one concern per PR.
3. Note how you verified the change (commands run, dry-run output).
4. Never include real transcripts, secrets, or personal paths — see the
   sanitization constraints in issue #2.

## Roadmap

Open issues track the direction:
- **#3** — public release readiness + launch.
- **#4 / #5** — session-search replication and full Antigravity extraction.
- **#6 / #7** — conditional retrieval and consolidation upgrades (gated on real
  need; not built preemptively).

"""Archiving a page must drop its pointer from EVERY index, not just the root.

The composition bug this guards: #53's write-time lane routing sends a new
page's pointer straight into a `MEMORY-<lane>.md`, while the consolidation pass
only ever dropped lines from the root `MEMORY.md`. Archiving such a page left a
dangling pointer behind — and consolidation runs unattended from the Stop hook,
so every store that adopted the routing rule would accumulate dangling pointers
until `okfmem reindex --verify` (the gate the reindex/curate skills depend on)
started failing with no user action.

Driven end-to-end through the module's `main()` in a subprocess, because the
defect was in main's wiring, not in `drop_memory_lines` — a unit test of the
helper passes either way. `OKFMEM_NO_STATUS=1` keeps the badge writer off the
real home dir; `--no-commit` keeps git out of it. Fixtures are synthetic.
"""
import os
import subprocess
import sys

import memory_reindex as mr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PAGE = (
    "---\n"
    "name: old-page\n"
    "description: an aged-out page\n"
    "type: project\n"
    "created: 2026-01-01\n"
    "last_accessed: 2026-01-01\n"
    "access_count: 0\n"
    "---\n\n"
    "# Old page\n\nBody.\n"
)


def _store(tmp_path, root_index, lane_index=None):
    store = tmp_path / "store"
    pdir = store / "projects" / "demo"
    pdir.mkdir(parents=True)
    (pdir / "old-page.md").write_text(PAGE, encoding="utf-8")
    (pdir / "MEMORY.md").write_text(root_index, encoding="utf-8")
    if lane_index is not None:
        (pdir / "MEMORY-lane.md").write_text(lane_index, encoding="utf-8")
    (store / "decay_state.json").write_text(
        '{"epoch": "2026-01-01"}\n', encoding="utf-8")
    return store, pdir


def _consolidate(store):
    env = dict(os.environ, OKFMEM_NO_STATUS="1")
    return subprocess.run(
        [sys.executable, os.path.join(ROOT, "memory_consolidate.py"),
         "--store", str(store), "--no-commit", "--today", "2026-07-31"],
        capture_output=True, text=True, env=env, cwd=ROOT, check=True)


def test_pointer_in_a_lane_index_is_dropped_when_the_page_is_archived(tmp_path):
    store, pdir = _store(
        tmp_path,
        root_index="# MEMORY\n\n- MEMORY-lane.md - covers the lane\n",
        lane_index="# Lane\n\n- [Old page](old-page.md) - hook\n")

    before = mr.Scan(str(pdir))
    assert before.ok, "fixture must start clean, or the test proves nothing"

    out = _consolidate(store).stdout
    # The relative path is built with os.path.join by the code under test, so
    # it renders with backslashes on Windows -- a hand-typed POSIX literal here
    # passes on macOS/Linux and fails only on the Windows matrix leg.
    assert "archive  " + os.path.join("projects", "demo", "old-page.md") in out
    assert "index lines dropped: 1" in out

    assert (pdir / "archive" / "old-page.md").is_file()
    assert "old-page.md" not in (pdir / "MEMORY-lane.md").read_text(
        encoding="utf-8")
    # the routing-table row is not collateral damage
    assert "MEMORY-lane.md" in (pdir / "MEMORY.md").read_text(encoding="utf-8")
    assert mr.Scan(str(pdir)).ok


def test_root_index_drop_still_works(tmp_path):
    """The pre-existing root-only path must keep working unchanged."""
    store, pdir = _store(
        tmp_path,
        root_index="# MEMORY\n\n- [Old page](old-page.md) - hook\n"
                   "- [Kept](kept.md) - unrelated\n")

    out = _consolidate(store).stdout
    assert "index lines dropped: 1" in out
    root = (pdir / "MEMORY.md").read_text(encoding="utf-8")
    assert "old-page.md" not in root
    assert "kept.md" in root          # unrelated pointer survives


def test_a_pointer_duplicated_across_two_indexes_is_dropped_from_both(tmp_path):
    """The half-finished-move shape: dropping only one copy would still leave a
    dangling pointer, so the count must be 2 and both files must be clean."""
    store, pdir = _store(
        tmp_path,
        root_index="# MEMORY\n\n- MEMORY-lane.md - covers the lane\n"
                   "- [Old page](old-page.md) - hook\n",
        lane_index="# Lane\n\n- old-page.md - bare-syntax copy\n")

    out = _consolidate(store).stdout
    assert "index lines dropped: 2" in out
    assert "old-page.md" not in (pdir / "MEMORY.md").read_text(encoding="utf-8")
    assert "old-page.md" not in (pdir / "MEMORY-lane.md").read_text(
        encoding="utf-8")
    assert mr.Scan(str(pdir)).ok


def test_dry_run_still_writes_no_index(tmp_path):
    store, pdir = _store(
        tmp_path,
        root_index="# MEMORY\n\n- MEMORY-lane.md - covers the lane\n",
        lane_index="# Lane\n\n- [Old page](old-page.md) - hook\n")
    lane_before = (pdir / "MEMORY-lane.md").read_bytes()

    env = dict(os.environ, OKFMEM_NO_STATUS="1")
    subprocess.run(
        [sys.executable, os.path.join(ROOT, "memory_consolidate.py"),
         "--store", str(store), "--dry-run", "--today", "2026-07-31"],
        capture_output=True, text=True, env=env, cwd=ROOT, check=True)

    assert (pdir / "MEMORY-lane.md").read_bytes() == lane_before
    assert (pdir / "old-page.md").is_file()

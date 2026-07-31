import os
import types

import pytest

import memory_init as mi
from memory_reindex import page_files


# ---------------------------------------------------------------------------
# Fixtures — a fake store, built the tmp_path way (no real home paths, so the
# leak gate stays green). We deliberately do NOT fake a Claude project dir
# here: project_inventory only reads <store>/projects, and project_for_cwd
# takes a registry dict directly, so there is nothing to encode. (When a test
# *does* fake a Claude project dir it must use mi.encode_root(str(repo)), never
# str(repo).replace("/", "-") — the naive form breaks on Windows CI where the
# drive colon becomes an absolute token and the path collapses; this has bitten
# #14/#16/#17/#41.)
# ---------------------------------------------------------------------------

def make_project(store, name, *, pages=0, archived=None, memory_lines=None,
                 memory_bytes=None, state=False, extra_files=()):
    """Create <store>/projects/<name>/ with the requested contents.

    ``archived`` = None -> no archive/ dir; an int -> archive/ dir with that
    many .md files. ``memory_lines`` = None -> no MEMORY.md; an int -> MEMORY.md
    with exactly that many lines. ``memory_bytes`` (mutually exclusive with
    ``memory_lines``) writes a MEMORY.md padded to exactly that many bytes —
    used for the byte-ceiling tests, where the exact line count doesn't matter.
    """
    d = store / "projects" / name
    d.mkdir(parents=True)
    for i in range(pages):
        (d / f"page{i}.md").write_text(f"# page {i}\n", encoding="utf-8")
    if memory_lines is not None:
        # newline="\n" pins the on-disk bytes to LF on every platform: without
        # it Path.write_text translates to CRLF on Windows and any byte-count
        # expectation computed from this same string is off by one per line.
        (d / "MEMORY.md").write_text("\n".join(f"line {i}"
                                     for i in range(memory_lines)) + "\n",
                                     encoding="utf-8", newline="\n")
    elif memory_bytes is not None:
        (d / "MEMORY.md").write_bytes(b"x" * memory_bytes)
    if state:
        (d / "STATE.md").write_text("state\n", encoding="utf-8")
    if archived is not None:
        adir = d / "archive"
        adir.mkdir()
        for i in range(archived):
            (adir / f"old{i}.md").write_text("archived\n", encoding="utf-8")
    for fn in extra_files:
        (d / fn).write_text("x\n", encoding="utf-8")
    return d


@pytest.fixture
def store(tmp_path):
    (tmp_path / "projects").mkdir()
    return tmp_path


def row_for(inv, name):
    for row in inv:
        if row[0] == name:
            return row
    raise AssertionError(f"{name} not in inventory")


# ---------------------------------------------------------------------------
# project_inventory — counting rules
# ---------------------------------------------------------------------------

def test_memory_and_state_excluded_from_pages(store):
    make_project(store, "proj", pages=3, memory_lines=10, state=True)
    name, pages, archived, mem_bytes, has_state, has_arch = row_for(
        mi.project_inventory(str(store)), "proj")
    # 3 real pages; MEMORY.md and STATE.md are NOT pages.
    assert pages == 3
    assert has_state is True
    assert mem_bytes == len("\n".join(f"line {i}" for i in range(10)) + "\n")


def test_lane_indexes_are_not_counted_as_pages(store):
    """#53's lane routing creates MEMORY-<lane>.md index files. A root-only
    "not MEMORY.md/STATE.md" filter counts every one of them as a durable page,
    so the reported page total inflates by one per lane on exactly the stores
    the lane-routing rule tells users to build. Pages come from the engine's
    page_files(), which knows an index from a page."""
    make_project(store, "proj", pages=2, memory_lines=3, state=True,
                 extra_files=("MEMORY-parser.md", "MEMORY-tooling.md",
                              "CONTEXT.md", "ck_2026-01-01_snap.md"))
    _, pages, _, _, _, _ = row_for(mi.project_inventory(str(store)), "proj")
    assert pages == 2
    # The engine's own enumerator is the single source of truth for this rule.
    assert pages == len(page_files(str(store / "projects" / "proj")))


def test_archive_counted(store):
    make_project(store, "proj", pages=1, archived=4)
    _, _, archived, _, _, has_arch = row_for(
        mi.project_inventory(str(store)), "proj")
    assert archived == 4
    assert has_arch is True


def test_missing_archive_dir_reports_zero_not_crash(store):
    make_project(store, "proj", pages=2, archived=None)
    _, _, archived, _, _, has_arch = row_for(
        mi.project_inventory(str(store)), "proj")
    assert archived == 0        # 0, distinguishable from an empty archive dir
    assert has_arch is False    # ... via has_archive_dir


def test_empty_archive_dir_distinguishable_from_missing(store):
    make_project(store, "proj", pages=1, archived=0)  # dir exists, 0 files
    _, _, archived, _, _, has_arch = row_for(
        mi.project_inventory(str(store)), "proj")
    assert archived == 0
    assert has_arch is True     # present but empty != absent


# ---------------------------------------------------------------------------
# MEMORY.md byte count + the auto-load byte ceiling (#53 — supersedes the old
# 200-line trigger: a store can sit well under 200 lines while over the byte
# ceiling once pointers run long, since bytes and lines diverge whenever
# pointer length isn't uniform).
# ---------------------------------------------------------------------------

def test_memory_byte_count_exact(store):
    make_project(store, "proj", memory_bytes=500)
    _, _, _, mem_bytes, _, _ = row_for(
        mi.project_inventory(str(store)), "proj")
    assert mem_bytes == 500


def test_no_memory_file_is_zero_bytes(store):
    make_project(store, "proj", pages=1, memory_lines=None)
    _, _, _, mem_bytes, _, _ = row_for(
        mi.project_inventory(str(store)), "proj")
    assert mem_bytes == 0


def test_byte_ceiling_shared_with_reindex_engine():
    # The ceiling has exactly one home (memory_reindex.MEMORY_BUDGET_BYTES);
    # memory_init imports it rather than restating the number (#53).
    assert mi.MEMORY_BUDGET_BYTES == 8192


def test_autoload_boundary_at_ceiling_no_warn_over_warns(store):
    make_project(store, "at_limit", memory_bytes=mi.MEMORY_BUDGET_BYTES)
    make_project(store, "over_limit", memory_bytes=mi.MEMORY_BUDGET_BYTES + 1)
    inv = mi.project_inventory(str(store))
    at = row_for(inv, "at_limit")[3]
    over = row_for(inv, "over_limit")[3]
    assert at == mi.MEMORY_BUDGET_BYTES
    assert over == mi.MEMORY_BUDGET_BYTES + 1
    # The warning fires on strictly-greater-than the ceiling: at-budget is
    # clean, one byte over trips. This is the exact predicate cmd_status
    # renders.
    assert (at > mi.MEMORY_BUDGET_BYTES) is False
    assert (over > mi.MEMORY_BUDGET_BYTES) is True


def test_lines_can_stay_low_while_bytes_trip_the_ceiling(store):
    # The scenario #53 exists to fix: few lines, long pointers -> bytes blow
    # the ceiling while the old line-count trigger would never fire.
    long_pointer = "- [" + ("x" * 200) + "](slug.md) -- hook\n"
    n_lines = 40
    text = long_pointer * n_lines
    (store / "projects" / "proj").mkdir(parents=True)
    (store / "projects" / "proj" / "MEMORY.md").write_text(
        text, encoding="utf-8")
    _, _, _, mem_bytes, _, _ = row_for(
        mi.project_inventory(str(store)), "proj")
    assert n_lines < 200                       # old trigger: would not fire
    assert mem_bytes > mi.MEMORY_BUDGET_BYTES   # byte trigger: fires


# ---------------------------------------------------------------------------
# Directory-shape edge cases
# ---------------------------------------------------------------------------

def test_empty_projects_dir_returns_empty_list(store):
    assert mi.project_inventory(str(store)) == []


def test_missing_projects_dir_returns_empty_list(tmp_path):
    # No projects/ subdir at all — must not crash.
    assert mi.project_inventory(str(tmp_path / "nope")) == []


def test_non_directory_file_in_projects_is_skipped(store):
    make_project(store, "realproj", pages=1)
    (store / "projects" / "loose.txt").write_text("junk\n", encoding="utf-8")
    (store / "projects" / "README.md").write_text("# store\n", encoding="utf-8")
    inv = mi.project_inventory(str(store))
    assert [r[0] for r in inv] == ["realproj"]  # files skipped, dirs sorted


def test_inventory_sorted_by_name(store):
    make_project(store, "zeta", pages=1)
    make_project(store, "alpha", pages=1)
    make_project(store, "mid", pages=1)
    assert [r[0] for r in mi.project_inventory(str(store))] == \
        ["alpha", "mid", "zeta"]


# ---------------------------------------------------------------------------
# project_for_cwd — cwd -> project via the built registry
# ---------------------------------------------------------------------------

def _fake_git(returncode, stdout=""):
    def _run(*args, **kwargs):
        return types.SimpleNamespace(returncode=returncode, stdout=stdout)
    return _run


def test_project_for_cwd_outside_repo_returns_none(monkeypatch):
    # git rev-parse fails (non-zero) outside a repo.
    monkeypatch.setattr(mi.subprocess, "run", _fake_git(128, ""))
    assert mi.project_for_cwd({"map": {"/anything": "x"}}) is None


def test_project_for_cwd_unregistered_root_returns_none(monkeypatch):
    root = os.path.normpath("/repos/unknown")
    monkeypatch.setattr(mi.subprocess, "run", _fake_git(0, root + "\n"))
    reg = {"map": {os.path.normpath("/repos/known"): "known"}}
    assert mi.project_for_cwd(reg) is None


def test_project_for_cwd_registered_root_returns_name(monkeypatch):
    root = os.path.normpath("/repos/myproj")
    monkeypatch.setattr(mi.subprocess, "run", _fake_git(0, root + "\n"))
    reg = {"map": {root: "myproj"}}
    assert mi.project_for_cwd(reg) == "myproj"


def test_project_for_cwd_missing_map_key_is_safe(monkeypatch):
    root = os.path.normpath("/repos/myproj")
    monkeypatch.setattr(mi.subprocess, "run", _fake_git(0, root + "\n"))
    assert mi.project_for_cwd({}) is None  # no "map" key at all


def test_project_for_cwd_git_not_installed_returns_none(monkeypatch):
    def _boom(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(mi.subprocess, "run", _boom)
    assert mi.project_for_cwd({"map": {}}) is None

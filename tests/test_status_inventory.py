import os
import types

import pytest

import memory_init as mi


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
                 state=False, extra_files=()):
    """Create <store>/projects/<name>/ with the requested contents.

    ``archived`` = None -> no archive/ dir; an int -> archive/ dir with that
    many .md files. ``memory_lines`` = None -> no MEMORY.md; an int -> MEMORY.md
    with exactly that many lines.
    """
    d = store / "projects" / name
    d.mkdir(parents=True)
    for i in range(pages):
        (d / f"page{i}.md").write_text(f"# page {i}\n", encoding="utf-8")
    if memory_lines is not None:
        (d / "MEMORY.md").write_text("\n".join(f"line {i}"
                                     for i in range(memory_lines)) + "\n",
                                     encoding="utf-8")
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
    name, pages, archived, mem_lines, has_state, has_arch = row_for(
        mi.project_inventory(str(store)), "proj")
    # 3 real pages; MEMORY.md and STATE.md are NOT pages.
    assert pages == 3
    assert has_state is True
    assert mem_lines == 10


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
# MEMORY.md line count + the 200-line auto-load boundary
# ---------------------------------------------------------------------------

def test_memory_line_count_exact(store):
    make_project(store, "proj", memory_lines=137)
    _, _, _, mem_lines, _, _ = row_for(
        mi.project_inventory(str(store)), "proj")
    assert mem_lines == 137


def test_no_memory_file_is_zero_lines(store):
    make_project(store, "proj", pages=1, memory_lines=None)
    _, _, _, mem_lines, _, _ = row_for(
        mi.project_inventory(str(store)), "proj")
    assert mem_lines == 0


def test_autoload_boundary_200_no_warn_201_warn(store):
    make_project(store, "at_limit", memory_lines=200)
    make_project(store, "over_limit", memory_lines=201)
    inv = mi.project_inventory(str(store))
    at = row_for(inv, "at_limit")[3]
    over = row_for(inv, "over_limit")[3]
    assert at == 200
    assert over == 201
    # The warning fires on strictly-greater-than the constant: 200 is clean,
    # 201 trips. This is the exact predicate cmd_status renders.
    assert mi.MEMORY_AUTOLOAD_LINES == 200
    assert (at > mi.MEMORY_AUTOLOAD_LINES) is False
    assert (over > mi.MEMORY_AUTOLOAD_LINES) is True


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

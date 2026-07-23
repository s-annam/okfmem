"""Coverage for #27: the store's managed `.gitignore` block (memory_init.py)
and the sync lock's relocation outside the worktree (memory_sync.py).
"""
import os
import shutil
import subprocess

import pytest

import memory_init as mi
import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={r.returncode}): "
                             f"{r.stderr}")
    return r


def _init_repo(path):
    os.makedirs(path, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "config", "user.email", "t@e.st")
    _git(path, "config", "user.name", "tester")
    _git(path, "config", "commit.gpgsign", "false")
    return path


# ---------------------------------------------------------------------------
# ensure_store_gitignore
# ---------------------------------------------------------------------------
def test_creates_gitignore_when_absent(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    gi = os.path.join(store, ".gitignore")

    action = mi.ensure_store_gitignore(gi, dry_run=False)

    assert action == "created"
    with open(gi, encoding="utf-8") as f:
        text = f.read()
    for line in mi.GITIGNORE_LINES:
        assert line in text
    assert mi.GITIGNORE_MARKER_OPEN in text
    assert mi.GITIGNORE_MARKER_CLOSE in text


def test_rerun_on_managed_block_is_idempotent_noop(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    gi = os.path.join(store, ".gitignore")

    first = mi.ensure_store_gitignore(gi, dry_run=False)
    assert first == "created"
    with open(gi, encoding="utf-8") as f:
        after_first = f.read()

    second = mi.ensure_store_gitignore(gi, dry_run=False)
    assert second == "unchanged"
    with open(gi, encoding="utf-8") as f:
        after_second = f.read()
    assert after_first == after_second


def test_hand_authored_rules_are_preserved_and_okfmem_lines_appended(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    gi = os.path.join(store, ".gitignore")
    with open(gi, "w", encoding="utf-8") as f:
        f.write("# my own rules\n.obsidian/\nck-backup/\n")

    action = mi.ensure_store_gitignore(gi, dry_run=False)

    assert action == "appended"
    with open(gi, encoding="utf-8") as f:
        text = f.read()
    # Unrelated hand-authored rules kept, in place, untouched.
    assert "# my own rules" in text
    assert ".obsidian/" in text
    assert "ck-backup/" in text
    # Missing okfmem lines gained.
    for line in mi.GITIGNORE_LINES:
        assert line in text


def test_dry_run_never_writes(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    gi = os.path.join(store, ".gitignore")

    action = mi.ensure_store_gitignore(gi, dry_run=True)

    assert action == "created"
    assert not os.path.exists(gi)


def test_rerun_after_dry_run_still_creates(tmp_path):
    store = str(tmp_path / "store")
    os.makedirs(store)
    gi = os.path.join(store, ".gitignore")

    mi.ensure_store_gitignore(gi, dry_run=True)
    assert not os.path.exists(gi)

    action = mi.ensure_store_gitignore(gi, dry_run=False)
    assert action == "created"
    assert os.path.exists(gi)


# ---------------------------------------------------------------------------
# Lock relocation outside the worktree
# ---------------------------------------------------------------------------
def test_lock_path_lives_under_git_dir_for_a_real_repo(tmp_path):
    store = str(tmp_path / "store")
    _init_repo(store)

    lock_path = ms._lock_path(store)

    git_dir = os.path.join(store, ".git")
    assert lock_path == os.path.join(git_dir, "okfmem-sync.lock")


def test_lock_path_falls_back_to_store_root_for_non_repo(tmp_path):
    # A plain directory (no .git) can't nest the lock anywhere real --
    # fall back to the pre-#27 store-root path instead of a nonexistent one.
    store = str(tmp_path / "plain")
    os.makedirs(store)

    lock_path = ms._lock_path(store)

    assert lock_path == os.path.join(store, ".okfmem-sync.lock")


def test_sync_never_stages_or_commits_the_lockfile(tmp_path):
    """End-to-end: acquire the lock inside a real store repo, `add -A` +
    inspect status exactly like sync_store does, and confirm the lockfile
    never shows up as a tracked/stageable path -- even with NO .gitignore
    entry for it at all (the belt-and-suspenders the lock relocation buys)."""
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = str(tmp_path / "store")
    _git(str(tmp_path), "clone", "-q", origin, store)
    _init_repo(store)  # re-apply user.email/name (clone doesn't inherit)
    with open(os.path.join(store, "a.txt"), "w", encoding="utf-8") as f:
        f.write("1")
    _git(store, "add", "-A")
    _git(store, "commit", "-qm", "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")

    fd = ms._acquire_lock(store, timeout=5)
    try:
        lock_path = ms._lock_path(store)
        assert os.path.isfile(lock_path)
        # Note: NO .gitignore entry for the lock exists in this store at all.
        st = _git(store, "status", "--porcelain")
        assert ".okfmem-sync.lock" not in st.stdout
        assert "okfmem-sync.lock" not in st.stdout

        _git(store, "add", "-A")
        staged = _git(store, "diff", "--cached", "--name-only")
        assert "okfmem-sync.lock" not in staged.stdout
    finally:
        ms._release_lock(store, fd)

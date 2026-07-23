"""Unit coverage for the #26 pull-before-commit reorder in memory_sync.sync_store.

Mirrors tests/test_pull.py's real-repo-no-network approach: throwaway bare
"remotes" plus real git clones, so the fail-open + no-op + pre-commit-rebase
contract is locked in rather than only smoke-verified by hand.
"""
import os
import shutil
import subprocess

import pytest

import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed (rc={r.returncode}): "
                             f"{r.stderr}")
    return r


def _config(repo):
    _git(repo, "config", "user.email", "t@e.st")
    _git(repo, "config", "user.name", "tester")
    _git(repo, "config", "commit.gpgsign", "false")


def _commit(repo, name, content):
    with open(os.path.join(repo, name), "w", encoding="utf-8") as f:
        f.write(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", f"c-{name}")


def _bare(tmp_path, name="origin.git"):
    origin = str(tmp_path / name)
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    return origin


def _clone(origin, dest):
    _git(os.path.dirname(dest) or ".", "clone", "-q", origin, dest)
    _config(dest)
    return dest


def _seed(tmp_path):
    """Bare origin + a `store` clone that tracks it and is up to date.

    Ships a `.gitignore` for `.okfmem-sync.lock` — a real okfmem-store repo
    ignores its own sync lockfile; without this, `add -A` inside sync_store
    would pick up the lockfile that's still open mid-sync as a spurious
    tracked change."""
    origin = _bare(tmp_path)
    store = _clone(origin, str(tmp_path / "store"))
    _commit(store, ".gitignore", ".okfmem-sync.lock\n")
    _commit(store, "a.txt", "1")
    _git(store, "push", "-q", "-u", "origin", "HEAD")  # -u sets @{u}
    return origin, store


def _advance_remote(tmp_path, origin, name, content, fname="a.txt"):
    """Push a new commit to `origin` from a throwaway clone."""
    w = str(tmp_path / f"w-{name}")
    _clone(origin, w)
    _commit(w, fname, content)
    _git(w, "push", "-q", "origin", "HEAD")


# --------------------------------------------------------------------------
# clean no-op: nothing local changed, nothing remote-ahead
# --------------------------------------------------------------------------

def test_noop_when_nothing_changed_and_not_remote_ahead(tmp_path):
    _origin, store = _seed(tmp_path)
    res = ms.sync_store(store, "msg")
    assert res["committed"] is False
    assert res["pushed"] is False
    assert res["conflict"] is False
    assert res["reason"] == "no changes to commit."
    # No empty commit was created, no rebase happened.
    log = _git(store, "log", "--oneline")
    assert len(log.stdout.strip().splitlines()) == 2  # gitignore, a.txt seed


# --------------------------------------------------------------------------
# pull-before-commit: remote changes are folded in before the local commit
# --------------------------------------------------------------------------

def test_remote_changes_integrated_before_local_commit(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "adv", "2")  # origin moves ahead

    with open(os.path.join(store, "b.txt"), "w", encoding="utf-8") as f:
        f.write("new local file")

    res = ms.sync_store(store, "local change")
    assert res["conflict"] is False
    assert res["committed"] is True
    assert res["pushed"] is True

    # Remote's change landed (rebase folded it in before our commit).
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "2"
    # Our local addition also made it through.
    assert os.path.exists(os.path.join(store, "b.txt"))

    # The local commit is a normal descendant of origin's advance — not a
    # replay-on-top produced by a POST-commit rebase.
    log = _git(store, "log", "--oneline")
    assert len(log.stdout.strip().splitlines()) == 4  # gitignore, seed, remote advance, ours


def test_pre_commit_rebase_conflict_aborts_and_commits_nothing(tmp_path):
    origin, store = _seed(tmp_path)
    # Remote changes a.txt; local also stages a conflicting edit to a.txt
    # (uncommitted — this is the realistic okfmem-save shape: new/edited
    # pages sitting in the working tree when sync runs).
    _advance_remote(tmp_path, origin, "remote", "REMOTE")
    with open(os.path.join(store, "a.txt"), "w", encoding="utf-8") as f:
        f.write("LOCAL")

    res = ms.sync_store(store, "conflicting change")
    assert res["conflict"] is True
    assert res["committed"] is False
    assert res["pushed"] is False
    # Rebase aborted -> not left mid-rebase.
    assert not ms._rebase_in_progress(store)
    # Local edit survived the aborted autostash (nothing lost).
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "LOCAL"


def test_preexisting_rebase_in_progress_bails_without_committing(tmp_path):
    """A human (or another session) is mid hand-resolve — `.git/rebase-merge`
    present, conflict markers in the tree. sync_store must bail BEFORE any
    add/commit/push, fail-open, and never stage marker text as resolved.
    Regression: the `_rebase_in_progress` guard used to gate only the pull
    sub-step, so add -A/commit ran anyway and committed markers on a detached
    HEAD reporting success."""
    origin, store = _seed(tmp_path)
    # Local COMMIT that conflicts with an incoming remote commit, so a manual
    # rebase stops mid-flight with conflict markers left in the tree.
    _commit(store, "a.txt", "LOCAL")
    _advance_remote(tmp_path, origin, "remote", "REMOTE")
    _git(store, "fetch", "-q")
    _git(store, "rebase", "@{u}", check=False)  # conflicts, leaves rebase-merge
    assert ms._rebase_in_progress(store)  # precondition
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert "<<<<<<<" in f.read()  # markers present before the call

    res = ms.sync_store(store, "auto save")

    # Bailed fail-open: nothing committed, not flagged as this call's conflict.
    assert res["committed"] is False
    assert res["sha"] is None
    assert res["pushed"] is False
    assert "rebase is already in progress" in res["reason"]
    # The human's in-progress rebase is left exactly as it was.
    assert ms._rebase_in_progress(store)
    # Marker text was neither resolved-away nor committed.
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert "<<<<<<<" in f.read()
    assert "auto save" not in _git(store, "log", "--oneline", "--all").stdout


# --------------------------------------------------------------------------
# fail-open: offline / no-upstream never blocks the commit
# --------------------------------------------------------------------------

def test_offline_fetch_failure_still_commits_locally(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))

    with open(os.path.join(repo, "b.txt"), "w", encoding="utf-8") as f:
        f.write("wip")

    res = ms.sync_store(repo, "offline commit", do_push=True)
    assert res["conflict"] is False
    assert res["committed"] is True
    # push will fail (dead remote) but that must not be reported as a conflict.
    assert res["pushed"] is False
    assert res["push_error"] is not None


def test_no_upstream_configured_still_commits(tmp_path):
    origin = _bare(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", origin)  # fetchable, no @{u} set

    with open(os.path.join(repo, "b.txt"), "w", encoding="utf-8") as f:
        f.write("wip")

    res = ms.sync_store(repo, "no-upstream commit", do_push=False)
    assert res["conflict"] is False
    assert res["committed"] is True


# --------------------------------------------------------------------------
# do_pull=False / do_push=False must still behave correctly
# --------------------------------------------------------------------------

def test_do_pull_false_skips_rebase_but_still_pushes(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "adv", "2")

    with open(os.path.join(store, "b.txt"), "w", encoding="utf-8") as f:
        f.write("wip")

    res = ms.sync_store(store, "no pull", do_pull=False)
    # No pre-commit rebase happened, so origin's advance is NOT in our tree...
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "1"
    assert res["committed"] is True
    # ...and push is rejected (non-fast-forward) rather than silently forced.
    assert res["pushed"] is False
    assert res["push_error"] is not None


def test_do_push_false_is_fully_offline_local_commit_only(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "adv", "2")

    with open(os.path.join(store, "b.txt"), "w", encoding="utf-8") as f:
        f.write("wip")

    res = ms.sync_store(store, "local only", do_push=False)
    assert res["committed"] is True
    assert res["pushed"] is False
    assert res["push_error"] is None
    assert res["conflict"] is False
    # No network touched -> origin's advance still not integrated.
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "1"

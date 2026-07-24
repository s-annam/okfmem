"""Unit coverage for memory_pull.pull_store's fail-open branches.

pull_store is what the #16 SessionStart pull-hook rests on: it must NEVER raise
and must exit non-zero (via main) ONLY on a rebase conflict. These tests drive
each branch with throwaway git repos + local bare "remotes" so the fail-open
contract is locked in rather than only smoke-verified by hand.
"""
import os
import shutil
import subprocess
import sys

import pytest

import memory_pull as mp

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")


# --------------------------------------------------------------------------
# git helpers — real repos, no network
# --------------------------------------------------------------------------

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
    """Bare origin + a `store` clone that tracks it and is up to date."""
    origin = _bare(tmp_path)
    store = _clone(origin, str(tmp_path / "store"))
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
# pull_store branches
# --------------------------------------------------------------------------

def test_not_a_git_repo(tmp_path):
    plain = str(tmp_path / "plain")
    os.makedirs(plain)
    res = mp.pull_store(plain)
    assert res == {"pulled": False, "up_to_date": False, "conflict": False,
                   "offline": False, "reason": res["reason"]}
    assert "not a git repo" in res["reason"]


def test_no_upstream_configured(tmp_path):
    # Valid remote so `git fetch` SUCCEEDS, but no branch tracking so @{u}
    # fails -> the "no upstream configured" no-op (distinct from offline).
    origin = _bare(tmp_path)
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", origin)  # fetchable, but no @{u}
    res = mp.pull_store(repo)
    assert res["up_to_date"] is False
    assert res["pulled"] is False
    assert res["conflict"] is False
    assert res["offline"] is False
    assert "no upstream" in res["reason"]


def test_offline_dead_remote(tmp_path):
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q")
    _config(repo)
    _commit(repo, "a.txt", "1")
    _git(repo, "remote", "add", "origin", str(tmp_path / "does-not-exist.git"))
    res = mp.pull_store(repo, timeout=10)
    assert res["offline"] is True
    assert res["conflict"] is False


def test_up_to_date(tmp_path):
    _origin, store = _seed(tmp_path)
    res = mp.pull_store(store)
    assert res["up_to_date"] is True
    assert res["pulled"] is False
    assert res["conflict"] is False


def test_fast_forward_when_clean(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "ff", "2")  # remote moves ahead
    res = mp.pull_store(store)
    assert res["pulled"] is True
    assert res["conflict"] is False
    with open(os.path.join(store, "a.txt"), encoding="utf-8") as f:
        assert f.read() == "2"


def test_dirty_tree_autostash_never_raises(tmp_path):
    origin, store = _seed(tmp_path)
    _advance_remote(tmp_path, origin, "adv", "2", fname="a.txt")
    # Local uncommitted edit to a DIFFERENT file -> dirty tree forces the
    # rebase --autostash path; the edit must survive and nothing must raise.
    with open(os.path.join(store, "local.txt"), "w", encoding="utf-8") as f:
        f.write("wip")
    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert os.path.exists(os.path.join(store, "local.txt"))  # autostash restored
    with open(os.path.join(store, "local.txt"), encoding="utf-8") as f:
        assert f.read() == "wip"


def test_rebase_conflict_exits_clean(tmp_path):
    origin, store = _seed(tmp_path)
    # Remote and local both change a.txt divergently -> rebase conflicts.
    _advance_remote(tmp_path, origin, "remote", "REMOTE", fname="a.txt")
    _commit(store, "a.txt", "LOCAL")
    res = mp.pull_store(store)
    assert res["conflict"] is True
    assert res["pulled"] is False
    # Rebase aborted -> tree left clean, not mid-rebase.
    assert not mp._rebase_in_progress(store)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=store,
                            capture_output=True, text=True)
    assert status.stdout.strip() == ""


def test_never_raises_on_garbage_store(tmp_path):
    # A .git that is a file pointing nowhere is malformed; pull_store must
    # still return a dict, never raise.
    repo = str(tmp_path / "repo")
    os.makedirs(repo)
    with open(os.path.join(repo, ".git"), "w", encoding="utf-8") as f:
        f.write("gitdir: /nonexistent\n")
    # .git is a file, not a dir -> treated as "not a git repo", clean no-op.
    res = mp.pull_store(repo)
    assert isinstance(res, dict)
    assert res["conflict"] is False


# --------------------------------------------------------------------------
# main() exit-code mapping — conflict is the ONLY non-zero exit
# --------------------------------------------------------------------------

def _run_main(monkeypatch, canned):
    monkeypatch.setattr(mp, "pull_store", lambda *a, **k: canned)
    monkeypatch.setattr(sys, "argv",
                        ["okfmem", "--quiet", "--store", "/whatever"])
    with pytest.raises(SystemExit) as e:
        mp.main()
    return e.value.code


def test_main_exit_zero_on_offline(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": False,
                                   "conflict": False, "offline": True,
                                   "reason": "offline"})
    assert code == 0


def test_main_exit_zero_on_up_to_date(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": True,
                                   "conflict": False, "offline": False,
                                   "reason": "up to date"})
    assert code == 0


def test_main_exit_one_only_on_conflict(monkeypatch):
    code = _run_main(monkeypatch, {"pulled": False, "up_to_date": False,
                                   "conflict": True, "offline": False,
                                   "reason": "conflict"})
    assert code == 1


# --------------------------------------------------------------------------
# unlinked_repo_notice — the SessionStart reminder to run `okfmem init`
# --------------------------------------------------------------------------

def test_notice_fires_only_when_unlinked(monkeypatch):
    import memory_init as mi

    monkeypatch.setattr(mi, "project_link_state", lambda store: ("unlinked", "proj"))
    msg = mp.unlinked_repo_notice("/any/store")
    assert msg and "okfmem init" in msg and "proj" in msg

    for state in ("linked", "not-a-repo", "no-claude"):
        monkeypatch.setattr(mi, "project_link_state",
                            lambda store, _s=state: (_s, "proj"))
        assert mp.unlinked_repo_notice("/any/store") is None


def test_notice_never_raises(monkeypatch):
    # Fail-open: this runs on the SessionStart path, so a broken probe must
    # degrade to "no reminder", never a traceback that fails the hook.
    import memory_init as mi

    monkeypatch.setattr(mi, "project_link_state",
                        lambda store: (_ for _ in ()).throw(RuntimeError("boom")))
    assert mp.unlinked_repo_notice("/any/store") is None

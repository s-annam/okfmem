"""#25 — auto-resolve cross-machine STATE.md conflicts (last-write-wins).

Real git repos, no network (bare origin + clones), same approach as
tests/test_sync_store.py. Covers BOTH directions (sync prefers local on a
tie, pull prefers incoming), the newer-`modified:`-wins rule either way, the
git-commit-time fallback that engages when a side's frontmatter has no
parseable `modified:` (legacy pages / not-yet-re-saved pages), the
both-sources-unavailable case falling back to the human path, BOTH conflict
points (a genuine rebase conflict on a local commit, and the autostash-pop
conflict on a dirty tree), and the abort-safety guard: any unmerged path that
is not a per-project STATE.md — alone or alongside STATE.md — must abort
exactly as before #25.
"""
import os
import shutil
import subprocess
from datetime import datetime, timezone

import pytest

import memory_pull as mp
import memory_sync as ms

pytestmark = pytest.mark.skipif(shutil.which("git") is None,
                                reason="git binary not available")

STATE_REL = "projects/demo/STATE.md"

T_OLD = "2026-07-20T10:00:00.000Z"
T_NEW = "2026-07-22T10:00:00.000Z"


def state_md(modified, body):
    """A realistic STATE.md matching the schema `skills/okfmem-save/SKILL.md`
    Step 5 writes today: `type`/`project`/`modified` at the TOP level of the
    frontmatter (top-level `modified:` is the shape the template stamps fresh
    on every save). Body differs enough to guarantee a content conflict on
    every line."""
    return ("---\n"
            "type: state\n"
            "project: demo\n"
            f"modified: {modified}\n"
            "---\n\n"
            "# STATE — demo\n\n"
            f"## Summary\n{body}\n")


def state_md_no_modified(body):
    """A STATE.md whose frontmatter has NO `modified:` key — a legacy page, or
    one not yet re-saved under the `modified:`-stamping template. Forces the
    git-commit-time fallback in `_auto_resolve_state_conflicts`."""
    return ("---\n"
            "type: state\n"
            "project: demo\n"
            "---\n\n"
            "# STATE — demo\n\n"
            f"## Summary\n{body}\n")


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


def _write(repo, rel, content):
    path = os.path.join(repo, *rel.split("/"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _read(repo, rel):
    with open(os.path.join(repo, *rel.split("/")), encoding="utf-8") as f:
        return f.read()


def _commit_all(repo, msg):
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", msg)


def _commit_all_at(repo, msg, when):
    """Commit with committer (and author) date pinned to `when` (ISO-8601), so
    the git-commit-time fallback in `_auto_resolve_state_conflicts` has a
    deterministic, controllable timestamp to compare on."""
    _git(repo, "add", "-A")
    r = subprocess.run(
        ["git", "commit", "-qm", msg], cwd=repo, capture_output=True, text=True,
        env={**os.environ, "GIT_COMMITTER_DATE": when, "GIT_AUTHOR_DATE": when})
    if r.returncode != 0:
        raise AssertionError(f"dated commit failed (rc={r.returncode}): {r.stderr}")


def _seed(tmp_path):
    """Bare origin + a tracking `store` clone seeded with a base STATE.md,
    a durable topic page, and a MEMORY.md index (plus the lockfile ignore
    a real store carries)."""
    origin = str(tmp_path / "origin.git")
    _git(str(tmp_path), "init", "--bare", "-q", origin)
    store = str(tmp_path / "store")
    _git(str(tmp_path), "clone", "-q", origin, store)
    _config(store)
    _write(store, ".gitignore", ".okfmem-sync.lock\n")
    _write(store, STATE_REL, state_md("2026-07-01T00:00:00.000Z", "base"))
    _write(store, "projects/demo/topic.md", "base topic\n")
    _write(store, "projects/demo/MEMORY.md", "# MEMORY\nbase\n")
    _commit_all(store, "seed")
    _git(store, "push", "-q", "-u", "origin", "HEAD")
    return origin, store


def _remote_writes(tmp_path, origin, files, name="remote", when=None):
    """Push a commit to origin (the 'other machine') touching `files`
    ({rel: content}). `when` pins the commit date for git-time fallback tests."""
    w = str(tmp_path / f"w-{name}")
    _git(str(tmp_path), "clone", "-q", origin, w)
    _config(w)
    for rel, content in files.items():
        _write(w, rel, content)
    if when is None:
        _commit_all(w, f"remote-{name}")
    else:
        _commit_all_at(w, f"remote-{name}", when)
    _git(w, "push", "-q", "origin", "HEAD")


def _origin_head_file(tmp_path, origin, rel, name="check"):
    """Content of `rel` at origin's HEAD (via a throwaway clone)."""
    c = str(tmp_path / f"chk-{name}")
    _git(str(tmp_path), "clone", "-q", origin, c)
    return _read(c, rel)


def _clean(store):
    """No mid-rebase state, no unmerged paths, no leftover stash."""
    assert not ms._rebase_in_progress(store)
    assert not ms._has_unmerged_paths(store)
    assert _git(store, "stash", "list").stdout.strip() == ""


# --------------------------------------------------------------------------
# timestamp parsing
# --------------------------------------------------------------------------

def test_modified_ts_parses_real_frontmatter():
    ts = ms._state_modified_ts(state_md(T_NEW, "x"))
    assert ts == datetime(2026, 7, 22, 10, 0, tzinfo=timezone.utc)


def test_modified_ts_parses_top_level_zulu():
    """The exact field shape the /okfmem-save template stamps: a top-level
    `modified:` with a `Z` (UTC) suffix — the parser must accept it."""
    blob = ("---\ntype: state\nproject: demo\n"
            "modified: 2026-07-23T18:04:00Z\n---\n\n# STATE — demo\n")
    ts = ms._state_modified_ts(blob)
    assert ts == datetime(2026, 7, 23, 18, 4, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("blob", [
    "no frontmatter at all\n",
    "---\nname: x\n---\nbody mentions modified: 2026-01-01T00:00:00Z\n",
    "---\n  modified: not-a-date\n---\n",
    "---\n  modified: null\n---\n",
    "---\nunclosed frontmatter\n",
    "",
])
def test_modified_ts_malformed_returns_none(blob):
    assert ms._state_modified_ts(blob) is None


def test_modified_ts_naive_assumed_utc_comparable():
    naive = ms._state_modified_ts("---\nmodified: 2026-07-22T10:00:00\n---\n")
    aware = ms._state_modified_ts(state_md(T_OLD, "x"))
    assert naive > aware  # would raise TypeError if naive stayed naive


# --------------------------------------------------------------------------
# sync — autostash-pop point (dirty local STATE.md, no local commit)
# --------------------------------------------------------------------------

def test_sync_pop_conflict_local_newer_wins_and_pushes(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_OLD, "REMOTE")})
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is False
    assert res["committed"] is True
    assert res["pushed"] is True
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _origin_head_file(tmp_path, origin, STATE_REL, "a")
    _clean(store)


def test_sync_pop_conflict_incoming_newer_wins(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})
    _write(store, STATE_REL, state_md(T_OLD, "LOCAL"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is False
    # Incoming snapshot won; the stale local snapshot left nothing to commit.
    assert res["committed"] is False
    assert "REMOTE" in _read(store, STATE_REL)
    _clean(store)


def test_sync_tie_prefers_local(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is False
    assert res["committed"] is True
    assert res["pushed"] is True
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _origin_head_file(tmp_path, origin, STATE_REL, "tie")
    _clean(store)


# --------------------------------------------------------------------------
# sync — genuine rebase-conflict point (local COMMIT, e.g. prior offline sync)
# --------------------------------------------------------------------------

def test_sync_rebase_conflict_local_commit_newer_wins(tmp_path):
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))
    ms.sync_store(store, "offline save", do_push=False)  # committed, unpushed
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_OLD, "REMOTE")})

    res = ms.sync_store(store, "next save")
    assert res["conflict"] is False
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _origin_head_file(tmp_path, origin, STATE_REL, "rb")
    _clean(store)


def test_sync_rebase_conflict_incoming_newer_local_commit_dropped(tmp_path):
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md(T_OLD, "LOCAL"))
    ms.sync_store(store, "offline save", do_push=False)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})

    res = ms.sync_store(store, "next save")
    assert res["conflict"] is False
    # Incoming snapshot won; the superseded local snapshot commit emptied out.
    assert "REMOTE" in _read(store, STATE_REL)
    assert "REMOTE" in _origin_head_file(tmp_path, origin, STATE_REL, "rb2")
    _clean(store)


# --------------------------------------------------------------------------
# the abort-safety guard: any non-STATE unmerged path must abort as before
# --------------------------------------------------------------------------

def test_sync_topic_page_conflict_alone_still_aborts(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {"projects/demo/topic.md": "REMOTE topic\n"})
    _write(store, "projects/demo/topic.md", "LOCAL topic\n")

    res = ms.sync_store(store, "save")
    assert res["conflict"] is True
    assert res["committed"] is False
    assert res["pushed"] is False
    # Original dirty tree restored, nothing lost, nothing left mid-flight.
    assert _read(store, "projects/demo/topic.md") == "LOCAL topic\n"
    _clean(store)


def test_sync_state_plus_memory_md_conflict_still_aborts(tmp_path):
    """The critical guard: STATE.md AND MEMORY.md both in dispute — the
    STATE part must NOT be auto-resolved; everything reaches the human."""
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {
        STATE_REL: state_md(T_OLD, "REMOTE"),
        "projects/demo/MEMORY.md": "# MEMORY\nREMOTE\n",
    })
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))
    _write(store, "projects/demo/MEMORY.md", "# MEMORY\nLOCAL\n")

    res = ms.sync_store(store, "save")
    assert res["conflict"] is True
    assert res["committed"] is False
    assert res["pushed"] is False
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _read(store, "projects/demo/MEMORY.md")
    _clean(store)


def test_sync_state_plus_topic_conflict_in_local_commit_still_aborts(tmp_path):
    """Guard at the rebase-conflict point too: a local COMMIT touching both
    STATE.md and a topic page conflicts on both — abort, keep the commit."""
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))
    _write(store, "projects/demo/topic.md", "LOCAL topic\n")
    ms.sync_store(store, "offline save", do_push=False)
    local_head = _git(store, "rev-parse", "HEAD").stdout.strip()
    _remote_writes(tmp_path, origin, {
        STATE_REL: state_md(T_OLD, "REMOTE"),
        "projects/demo/topic.md": "REMOTE topic\n",
    })

    res = ms.sync_store(store, "next save")
    assert res["conflict"] is True
    assert res["pushed"] is False
    # Rebase aborted: local commit still intact and un-replayed.
    assert _git(store, "rev-parse", "HEAD").stdout.strip() == local_head
    assert "LOCAL" in _read(store, STATE_REL)
    _clean(store)


# --------------------------------------------------------------------------
# no-`modified:` frontmatter → git-commit-time fallback engages (#25/#26)
#
# Legacy pages (or pages not yet re-saved under the `modified:`-stamping
# template) carry no parseable `modified:`; auto-resolve must still engage via
# each side's git commit time instead of silently deferring the whole common
# transition case to a hand-merge.
# --------------------------------------------------------------------------

# Controlled git commit dates (the fallback timestamp source). `Z`/`+0000`
# forms both accepted by git; %cI normalizes to `...Z`.
D_OLD = "2026-07-20T10:00:00 +0000"
D_NEW = "2026-07-22T10:00:00 +0000"


def test_sync_pop_no_modified_worktree_local_wins_via_git_time(tmp_path):
    """Pop-conflict point, local side has NO frontmatter timestamp: its git
    time is the autostash (≈now), which beats an old committed incoming
    snapshot → local wins and is pushed. Exercises the stash@{0} fallback ref."""
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")}, when=D_OLD)
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is False
    assert res["committed"] is True
    assert res["pushed"] is True
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _origin_head_file(tmp_path, origin, STATE_REL, "gt")
    _clean(store)


def test_sync_rebase_no_modified_local_commit_newer_wins_via_git_time(tmp_path):
    """Rebase-conflict point, BOTH sides lack `modified:` — decided purely by
    git commit time. Local commit is newer (D_NEW) than the incoming remote
    (D_OLD) → local wins. Exercises the REBASE_HEAD fallback ref."""
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))
    _commit_all_at(store, "offline save", D_NEW)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")}, when=D_OLD)

    res = ms.sync_store(store, "next save")
    assert res["conflict"] is False
    assert "LOCAL" in _read(store, STATE_REL)
    assert "LOCAL" in _origin_head_file(tmp_path, origin, STATE_REL, "gt2")
    _clean(store)


def test_sync_rebase_no_modified_incoming_newer_wins_via_git_time(tmp_path):
    """Reverse of the above: incoming commit is newer (D_NEW) than the local
    commit (D_OLD) → incoming wins, the superseded local commit empties out."""
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))
    _commit_all_at(store, "offline save", D_OLD)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")}, when=D_NEW)

    res = ms.sync_store(store, "next save")
    assert res["conflict"] is False
    assert "REMOTE" in _read(store, STATE_REL)
    assert "REMOTE" in _origin_head_file(tmp_path, origin, STATE_REL, "gt3")
    _clean(store)


def test_pull_rebase_no_modified_incoming_newer_wins_via_git_time(tmp_path):
    """Pull direction (prefer_local=False), both sides lack `modified:` at the
    rebase-conflict point: newer incoming git time wins."""
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))
    _commit_all_at(store, "offline save", D_OLD)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")}, when=D_NEW)

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert "REMOTE" in _read(store, STATE_REL)
    _clean(store)


def test_no_timestamp_source_at_all_defers_to_human(tmp_path, monkeypatch):
    """The preserved abort case: frontmatter has no parseable `modified:` AND
    the git-commit-time fallback is unavailable (simulated) — auto-resolve
    must defer to the human path, restoring the original tree, losing nothing."""
    monkeypatch.setattr(ms, "_side_commit_ts", lambda *a, **k: None)
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")})
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is True
    assert res["committed"] is False
    assert "LOCAL" in _read(store, STATE_REL)  # nothing lost
    _clean(store)


# --------------------------------------------------------------------------
# pull — read side: newer `modified:` wins, tie prefers INCOMING
# --------------------------------------------------------------------------

def test_pull_pop_conflict_incoming_newer_wins(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})
    _write(store, STATE_REL, state_md(T_OLD, "LOCAL"))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert "REMOTE" in _read(store, STATE_REL)
    _clean(store)
    # Read-only pull leaves nothing staged behind.
    assert _git(store, "diff", "--cached", "--name-only").stdout.strip() == ""


def test_pull_pop_conflict_local_newer_survives_as_worktree_edit(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_OLD, "REMOTE")})
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    # Branch moved to remote, but the newer local snapshot survives as an
    # ordinary unstaged working-tree edit (as a clean autostash-pop would
    # leave it) for the next `okfmem sync` to commit.
    assert "LOCAL" in _read(store, STATE_REL)
    _clean(store)
    assert _git(store, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert STATE_REL in _git(store, "diff", "--name-only").stdout


def test_pull_tie_prefers_incoming(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL"))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert "REMOTE" in _read(store, STATE_REL)
    _clean(store)


def test_pull_rebase_conflict_on_local_commit_incoming_newer(tmp_path):
    origin, store = _seed(tmp_path)
    _write(store, STATE_REL, state_md(T_OLD, "LOCAL"))
    ms.sync_store(store, "offline save", do_push=False)
    _remote_writes(tmp_path, origin, {STATE_REL: state_md(T_NEW, "REMOTE")})

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert "REMOTE" in _read(store, STATE_REL)
    _clean(store)


def test_pull_topic_conflict_still_aborts_tree_restored(tmp_path):
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin, {
        STATE_REL: state_md(T_NEW, "REMOTE"),
        "projects/demo/topic.md": "REMOTE topic\n",
    })
    _write(store, STATE_REL, state_md(T_OLD, "LOCAL"))
    _write(store, "projects/demo/topic.md", "LOCAL topic\n")

    res = mp.pull_store(store)
    assert res["conflict"] is True
    assert res["pulled"] is False
    # Original dirty tree restored exactly.
    assert "LOCAL" in _read(store, STATE_REL)
    assert _read(store, "projects/demo/topic.md") == "LOCAL topic\n"
    _clean(store)


def test_pull_pop_no_modified_worktree_local_newer_survives_via_git_time(tmp_path):
    """Pull side, pop-conflict point: local worktree edit has no `modified:`
    frontmatter, so its git time is the autostash (≈now), newer than the old
    committed incoming snapshot → the local edit survives as an unstaged
    working-tree change (mirrors the clean-pop `local-newer-survives` case)."""
    origin, store = _seed(tmp_path)
    _remote_writes(tmp_path, origin,
                   {STATE_REL: state_md_no_modified("REMOTE")}, when=D_OLD)
    _write(store, STATE_REL, state_md_no_modified("LOCAL"))

    res = mp.pull_store(store)
    assert res["conflict"] is False
    assert res["pulled"] is True
    assert "LOCAL" in _read(store, STATE_REL)
    _clean(store)
    assert _git(store, "diff", "--cached", "--name-only").stdout.strip() == ""
    assert STATE_REL in _git(store, "diff", "--name-only").stdout


# --------------------------------------------------------------------------
# multiple projects' STATE.md conflicting at once — all resolved per-file
# --------------------------------------------------------------------------

def test_sync_two_projects_state_conflicts_each_resolved_independently(tmp_path):
    origin, store = _seed(tmp_path)
    other = "projects/other/STATE.md"
    _write(store, other, state_md("2026-07-01T00:00:00.000Z", "base"))
    _commit_all(store, "add other project")
    _git(store, "push", "-q", "origin", "HEAD")

    _remote_writes(tmp_path, origin, {
        STATE_REL: state_md(T_OLD, "REMOTE-demo"),
        other: state_md(T_NEW, "REMOTE-other"),
    })
    _write(store, STATE_REL, state_md(T_NEW, "LOCAL-demo"))
    _write(store, other, state_md(T_OLD, "LOCAL-other"))

    res = ms.sync_store(store, "save")
    assert res["conflict"] is False
    assert "LOCAL-demo" in _read(store, STATE_REL)     # local newer here
    assert "REMOTE-other" in _read(store, other)       # incoming newer here
    _clean(store)

#!/usr/bin/env python3
"""okfmem sync — the single git commit+push path for the store.

Both callers share this one code path so the pull-rebase + lock logic lives in
exactly one place instead of being duplicated (and drifting) between them:

  - `memory_consolidate.py` (P3 Stop-hook job) imports `sync_store` in-process.
  - the `/okfmem-save` skill shells `okfmem sync -m "<summary>"`.

Sequence:  fetch + rebase --autostash onto origin (if do_push and do_pull)
           →  add -A  →  commit (if staged)  →  push

The pull moved BEFORE the commit (#26): folding in remote changes first means
this machine's commit lands on top of the latest shared history, instead of
being replayed over it during a post-commit rebase where it can collide with
a concurrent edit from another machine. See `memory_pull.py`'s `pull_store`
(the standalone `okfmem pull` primitive, #17) for the fetch/rebase/autostash
pattern this mirrors — that logic isn't imported here (it acquires its own
copy of this same lock, which would deadlock a caller that already holds it),
so the fetch+rebase steps are re-implemented locally against the lock this
function already holds.

`do_pull` only takes effect when `do_push` is also true — matching the prior
behavior where `do_push=False` meant "fully offline, no network calls at
all" (used by callers that want a local-only commit).

Guards:
  - A lockfile (`<store>/.git/okfmem-sync.lock`, #27 -- outside the worktree,
    so it is never staged regardless of `.gitignore` state) serializes
    concurrent windows so two sessions never race on the same working tree.
    It's a single pure-
    stdlib path on every platform (POSIX and Windows alike): `os.open` with
    `O_CREAT | O_EXCL` is atomic everywhere, so there is no fcntl/msvcrt
    branching. A lock older than `LOCK_STALE_SECONDS` is assumed to be left
    over from a process that died without releasing it and is broken rather
    than deadlocking forever.
  - A pre-commit rebase conflict confined to per-project STATE.md snapshots
    is auto-resolved (#25): STATE.md is a whole-file last-write-wins snapshot,
    so the newer `modified:` frontmatter timestamp wins (tie → the local,
    actively-saving side). Any conflict touching a durable page or MEMORY.md
    aborts the half-done rebase, commits nothing, and refuses to push (never
    force) — the caller surfaces it to the user. A rebase already in progress
    at call time (a human hand-resolving one) makes the whole sync bail early
    before any add/commit/push — fail-open, tree left untouched — so marker
    text is never staged as "resolved" and committed onto a detached HEAD.
  - Fetch failure (offline) is swallowed — falls through to add/commit/push
    as if `do_pull` were false, matching `pull_store`'s fail-open contract.
  - No-op + clean return when nothing is staged (no empty commits) and
    nothing was remote-ahead (no spurious rebase).

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

# A lock older than this is treated as abandoned (owning process died without
# releasing it) and is broken rather than left to deadlock future syncs.
LOCK_STALE_SECONDS = 600  # 10 minutes


def _git(store, *args, timeout=120):
    return subprocess.run(
        ["git", "-C", store, *args], capture_output=True, text=True, timeout=timeout
    )


def _git_dir(store):
    """Resolve the real git dir for `store` so the lockfile can live outside
    the worktree (never stageable, regardless of `.gitignore` state). `.git`
    is a directory for a normal clone but a FILE (a gitdir pointer) for a
    worktree/submodule, so this asks git rather than assuming -- the store is
    always a top-level clone in practice (never a worktree), but resolving it
    properly costs nothing and removes the assumption. Falls back to
    `<store>/.git` if git can't answer (e.g. `store` isn't a repo yet)."""
    gd = _git(store, "rev-parse", "--git-dir")
    if gd.returncode == 0 and gd.stdout.strip():
        git_dir = gd.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(store, git_dir)
        return git_dir
    return os.path.join(store, ".git")


def _lock_path(store):
    # #27: lives inside `.git/` (never staged by `add -A`, never visible to
    # `git status`), so a missing/incomplete store `.gitignore` can no longer
    # cause a per-machine lockfile (this machine's PID + wall-clock) to be
    # committed into shared history. Belt-and-suspenders alongside the
    # `.gitignore` managed block `memory_init.py` now maintains (which still
    # covers `*.db` and future artifacts).
    #
    # `_git_dir` only resolves to something real when `store` is an actual
    # git repo; a plain directory (e.g. a bare `tmp_path` in a unit test, or
    # a caller that hasn't `git init`-ed yet) has no `.git` to nest under, so
    # fall back to the pre-#27 store-root path rather than a nonexistent
    # nested one.
    git_dir = _git_dir(store)
    if not os.path.isdir(git_dir):
        return os.path.join(store, ".okfmem-sync.lock")
    return os.path.join(git_dir, "okfmem-sync.lock")


def _lock_age(path):
    """Seconds since the lockfile was created, or None if it's gone already."""
    try:
        return time.time() - os.path.getmtime(path)
    except OSError:
        return None


def _lock_owner_pid(path):
    """Best-effort read of the PID recorded in the lockfile body. Returns the
    int PID, or None if the file is missing, empty, or its first token isn't a
    parseable PID (a partial/corrupt write) — callers then judge by age alone."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            tokens = f.readline().split()
        if tokens:
            return int(tokens[0])
    except (OSError, ValueError):
        pass
    return None


def _pid_alive(pid):
    """Best-effort liveness probe for a lockfile's owner. Returns True if the
    process looks alive, False if it looks gone, None if we can't tell (the
    caller then falls back to the age heuristic alone). Never raises.

    POSIX: `os.kill(pid, 0)` delivers no signal — it only probes existence and
    permission. On Windows `os.kill` maps signal 0 to CTRL_C_EVENT (it would
    signal the process group, not probe it), so we deliberately don't use it
    there and return None, deferring to the age check."""
    if pid is None or pid <= 0 or os.name == "nt":
        return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False  # no such process — genuinely gone
    except PermissionError:
        return True  # exists but owned by another user
    except OSError:
        return None  # can't determine — defer to age


def _acquire_lock(store, timeout=60):
    """Acquire the per-store lockfile. `os.O_CREAT | os.O_EXCL` atomically
    creates-or-fails on POSIX and Windows alike, so this one code path
    serializes concurrent syncs on every platform — no conditional imports.
    The file body (PID + timestamp) is diagnostic only; staleness is judged by
    the file's own mtime (so clock skew between writer and reader doesn't
    matter) plus a best-effort liveness check on the recorded PID. Returns the
    open fd (keep it alive until the sync finishes) or None if the lock can't
    be taken within `timeout` seconds."""
    path = _lock_path(store)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, f"{os.getpid()} {time.time()}\n".encode())
            except OSError:
                # Write failed after we won the create race — don't leak the fd
                # or leave an unbreakable-until-stale lockfile behind.
                os.close(fd)
                os.remove(path)
                raise
            return fd
        except FileExistsError:
            # Held by someone. Break it only if it's genuinely stale: older
            # than the threshold AND not owned by a still-running process (a
            # long sync is old but alive, not dead). A corrupt/empty lockfile
            # yields no PID, so age alone decides.
            age = _lock_age(path)
            owner_alive = _pid_alive(_lock_owner_pid(path))
            if age is not None and age > LOCK_STALE_SECONDS and owner_alive is not True:
                try:
                    os.remove(path)
                    continue  # broke it — retry acquisition immediately
                except OSError:
                    pass  # can't remove (raced, or Windows holds it open) —
                    # fall through to the shared timeout/backoff tail below
        # Shared tail for every non-acquire branch (fresh lock held, OR a
        # stale lock we couldn't break): honor the overall timeout, else back
        # off and retry. This is the ONLY sleep/deadline site, so a failed
        # stale-break can never busy-loop.
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.5)


def _rebase_in_progress(store):
    """True if `store` is mid-rebase (a human hand-resolving a conflict).
    Resolves the real git dir so a worktree `.git` *file* is handled too,
    falling back to `<store>/.git` for a normal clone. Mirrors
    `memory_pull._rebase_in_progress` — duplicated rather than imported to
    avoid a circular import (`memory_pull` imports this module for the
    shared `_git`/lock helpers)."""
    git_dir = None
    gd = _git(store, "rev-parse", "--git-dir")
    if gd.returncode == 0 and gd.stdout.strip():
        git_dir = gd.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(store, git_dir)
    if git_dir is None:
        git_dir = os.path.join(store, ".git")
    return os.path.isdir(os.path.join(git_dir, "rebase-merge")) or os.path.isdir(
        os.path.join(git_dir, "rebase-apply")
    )


_UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _has_unmerged_paths(store):
    """True if `git status --porcelain` shows any unmerged path. Used to catch
    the case where `git rebase --autostash` reports success (returncode 0 —
    the rebase itself completed) but the trailing autostash-pop it runs
    internally left conflict markers behind: git does NOT fold that failure
    into the rebase's exit code, so a bare `returncode != 0` check misses it
    and would let conflict markers get silently `add -A`'d and committed."""
    st = _git(store, "status", "--porcelain")
    if st.returncode != 0:
        return False
    return any(line[:2] in _UNMERGED_CODES for line in st.stdout.splitlines())


def _unmerged_paths(store):
    """The current unmerged paths (repo-relative, '/'-separated as git prints
    them), or [] if there are none / they can't be enumerated. Callers treat
    an empty answer as "nothing safely auto-resolvable" — enumeration failure
    fails closed, never open."""
    r = _git(store, "diff", "--name-only", "--diff-filter=U")
    if r.returncode != 0:
        return []
    return [p for p in r.stdout.splitlines() if p.strip()]


# `modified:` inside YAML frontmatter. The STATE.md template stamps it at the
# top level (`modified: <ISO-8601 UTC>`); the `^\s*` also tolerates an indented
# form (e.g. nested under `metadata:` on older pages). Optionally quoted. Value
# must start with a digit so non-timestamps (`modified: null`) fall through to
# the git-time fallback / abort path.
_FRONTMATTER_MODIFIED = re.compile(r"^\s*modified:\s*[\"']?(\d[^\s\"']*)", re.MULTILINE)


def _state_modified_ts(text):
    """Parse the `modified:` timestamp out of a STATE.md blob's YAML
    frontmatter. Returns an aware datetime (naive values are assumed UTC so
    cross-side comparison never raises), or None when the frontmatter block
    or the timestamp is missing/malformed — callers then refuse to
    auto-resolve and fall back to the human path."""
    text = text.lstrip("\ufeff")
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    m = _FRONTMATTER_MODIFIED.search(text[:end])
    if m is None:
        return None
    try:
        ts = datetime.fromisoformat(m.group(1))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _side_commit_ts(store, path, ref):
    """Git commit time (last commit touching `path` reachable from `ref`) as an
    aware datetime, or None when `ref` can't be resolved / doesn't touch `path`.
    The fallback timestamp for a STATE.md side whose frontmatter has no
    parseable `modified:` (a legacy page, or one not yet re-saved under the
    `modified:`-stamping template). `%cI` is strict ISO-8601 with a timezone
    offset (e.g. `2026-07-23T13:43:47-07:00`), so `fromisoformat` (3.11+)
    always yields an aware datetime; the naive-guard is belt-and-suspenders."""
    r = _git(store, "log", "-1", "--format=%cI", ref, "--", path)
    if r.returncode != 0:
        return None
    s = r.stdout.strip()
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _auto_resolve_state_conflicts(store, prefer_local):
    """#25: resolve the CURRENT unmerged set iff it is confined to per-project
    STATE.md snapshots. Returns True with every path resolved + staged, or
    False having decided nothing is safe to touch (callers then unwind
    exactly as before #25 — abort the rebase / reset + stash pop).

    STATE.md is a whole-file last-write-wins snapshot, so "which side wins"
    is decided by the newer `modified:` frontmatter timestamp; `prefer_local`
    only breaks an exact tie (sync — this machine is actively saving — passes
    True; read-side pull passes False).

    Stage mapping (verified empirically; identical at both conflict points
    this hooks — a rebase content conflict and a post-rebase autostash-pop
    conflict): stage 2 (`--ours`) is the incoming/upstream side, stage 3
    (`--theirs`) is this machine's side (the replayed local commit, or the
    stashed local working-tree edit).

    `modified:`-less fallback (#25/#26): a STATE.md whose frontmatter carries
    no parseable `modified:` (a legacy page, or one not yet re-saved under the
    `modified:`-stamping template) falls back to that side's git commit time
    so auto-resolve still engages instead of silently deferring the whole
    common-transition case to the human path. Ref → side (verified empirically
    at both points): the incoming/ours side (stage 2) is always the current
    branch tip, so `HEAD -- <path>` dates it (`@{u}` is unusable mid-rebase —
    HEAD is detached). The local/theirs side (stage 3) is `REBASE_HEAD` while a
    rebase is in progress (the replayed local commit) and `stash@{0}` once the
    rebase has finished (the retained autostash holding the working-tree edit).

    Abort-safety guard: this function returns False — resolving NOTHING —
    unless every one of these holds for the ENTIRE unmerged set:
      - the set is non-empty and enumerable;
      - every path's basename is exactly `STATE.md` (a durable `<topic>.md`
        or `MEMORY.md` in the set fails this and reaches the human);
      - both sides' blobs exist (no add/delete conflicts);
      - both sides yield a timestamp — a parseable `modified:` OR, failing
        that, a git commit time. Only when BOTH sources are unavailable for a
        side does it defer to the human.
    The git-time fallback only ever supplies a timestamp for a path that has
    already passed the STATE.md-only gate above, so it never weakens the
    abort-safety guard: a conflict touching any `<topic>.md`/`MEMORY.md` still
    aborts. All decisions are made before the first write, so a refusal never
    leaves a half-resolved tree behind."""
    paths = _unmerged_paths(store)
    if not paths:
        return False
    if any(p.rsplit("/", 1)[-1] != "STATE.md" for p in paths):
        return False  # durable memory content is in dispute — a human's job
    # Local/theirs side (stage 3) git-time ref depends on the conflict point:
    # a rebase still in progress means we're at a content conflict on the
    # replayed local commit (REBASE_HEAD); otherwise the rebase finished and
    # this is the autostash-pop conflict, whose local edit lives in the
    # retained stash (stash@{0}).
    local_ref = "REBASE_HEAD" if _rebase_in_progress(store) else "stash@{0}"
    picks = []
    for p in paths:
        incoming = _git(store, "show", f":2:{p}")
        local = _git(store, "show", f":3:{p}")
        if incoming.returncode != 0 or local.returncode != 0:
            return False  # add/delete conflict — no both-sides snapshot
        t_inc = _state_modified_ts(incoming.stdout)
        if t_inc is None:
            t_inc = _side_commit_ts(store, p, "HEAD")
        t_loc = _state_modified_ts(local.stdout)
        if t_loc is None:
            t_loc = _side_commit_ts(store, p, local_ref)
        if t_inc is None or t_loc is None:
            return False  # neither frontmatter nor git time — human path
        if t_loc != t_inc:
            local_wins = t_loc > t_inc
        else:
            local_wins = prefer_local
        picks.append((p, "--theirs" if local_wins else "--ours"))
    for p, side in picks:
        if _git(store, "checkout", side, "--", p).returncode != 0:
            return False
        if _git(store, "add", "--", p).returncode != 0:
            return False
    return True


def _finish_rebase_with_state_autoresolve(store, prefer_local):
    """We just saw `rebase` fail mid-flight. If (and only if) every stop is a
    STATE.md-only conflict, resolve each and drive the rebase to completion.
    Returns True when the rebase fully finished; False when anything wasn't
    safely auto-resolvable — the caller then runs the same `rebase --abort`
    unwind as before #25 (which also re-applies the autostash, so a partial
    walk loses nothing).

    A rebase replays commits one at a time, so this loops: resolve the
    current stop, `--continue`, and handle the next stop if a later commit
    conflicts too. A commit whose STATE.md change is superseded by the
    incoming side can become empty; modern git drops it on `--continue`,
    older git stops and asks for `--skip` — both are handled."""
    if not _rebase_in_progress(store):
        return False  # rebase failed without leaving a conflict to resolve
    for _ in range(100):  # hard bound: a rebase replays finitely many commits
        if not _rebase_in_progress(store):
            return True
        if _unmerged_paths(store):
            if not _auto_resolve_state_conflicts(store, prefer_local):
                return False
        cont = _git(store, "-c", "core.editor=true", "rebase", "--continue")
        if cont.returncode != 0:
            blob = ((cont.stdout or "") + (cont.stderr or "")).lower()
            if "--skip" in blob and not _unmerged_paths(store):
                # The resolved pick became empty (incoming side won and the
                # commit carried nothing else) — old-git stops; drop it.
                if _git(
                    store, "rebase", "--skip"
                ).returncode != 0 and not _unmerged_paths(store):
                    return False
            elif not _unmerged_paths(store):
                return False  # failed for some non-conflict reason — unwind
            # else: --continue/--skip surfaced the NEXT commit's conflict —
            # loop back and try to resolve that one too.
    return False


def _release_lock(store, fd):
    """Close the fd and remove the lockfile. Quietly tolerates the file
    already being gone (e.g. a racing stale-lock break) — release must never
    raise, since it runs in a `finally`."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(_lock_path(store))
    except OSError:
        pass


def sync_store(store, message, do_push=True, do_pull=True):
    """Stage everything, and if anything changed commit (+optionally pull-rebase
    + push). Returns a dict describing what happened:

        {committed: bool, sha: str|None, pushed: bool, reason: str,
         push_error: str|None, conflict: bool}

    Never raises on the git operations themselves; the caller decides how loud
    to be. A rebase conflict returns conflict=True, pushed=False so the caller
    can tell the user to resolve `~/okfmem-store` by hand.
    """
    store = os.path.abspath(os.path.expanduser(store))
    res = {
        "committed": False,
        "sha": None,
        "pushed": False,
        "reason": "",
        "push_error": None,
        "conflict": False,
    }

    lock = _acquire_lock(store)
    if lock is None:
        res["reason"] = "another okfmem sync holds the lock; skipped."
        return res
    try:
        # A human (or another session) is mid hand-resolve of a store conflict
        # — `.git/rebase-merge` is present and the working tree may hold literal
        # conflict markers. Bail BEFORE any `add -A`/commit/push, fail-open, so
        # we never stage marker text as "resolved" and commit corrupted memory
        # onto a detached HEAD. This mirrors `pull_store`'s early return
        # (memory_pull.py) and must sit above every git mutation, not just the
        # pull sub-step below — the pull block is skipped on do_push=False /
        # offline / already-current, but the stage+commit path is not.
        if _rebase_in_progress(store):
            res["reason"] = (
                "a rebase is already in progress in the store; "
                "skipped (resolve ~/okfmem-store by hand)."
            )
            return res

        # Pull-before-commit (#26): fold in remote changes BEFORE staging/
        # committing local work, so this machine's commit is built on top of
        # the latest shared history rather than replayed over it afterward.
        # Only runs when do_push is also true — do_push=False means "fully
        # offline, no network calls at all," matching the prior behavior.
        if do_push and do_pull:
            fetch = _git(store, "fetch")
            if fetch.returncode == 0:
                upstream = _git(store, "rev-parse", "@{u}")
                if upstream.returncode == 0:
                    local = _git(store, "rev-parse", "HEAD")
                    if (
                        local.returncode == 0
                        and local.stdout.strip() != upstream.stdout.strip()
                    ):
                        # A pre-existing rebase-in-progress was already caught
                        # by the early return at the top of the try, so the
                        # tree is clean here and this pull's own rebase is safe.
                        rb = _git(store, "rebase", "--autostash", "@{u}")
                        if rb.returncode != 0:
                            # Rebase failed mid-flight. #25: a conflict
                            # confined to STATE.md snapshots is decided
                            # by the newer `modified:` frontmatter
                            # (prefer_local=True — this machine is the
                            # one actively saving); anything touching a
                            # durable page or MEMORY.md falls through to
                            # the normal abort so a human reconciles it.
                            if not _finish_rebase_with_state_autoresolve(
                                store, prefer_local=True
                            ):
                                _git(store, "rebase", "--abort")
                                res["conflict"] = True
                                res["reason"] = (
                                    "pull --rebase conflict before commit — "
                                    "resolve ~/okfmem-store by hand; "
                                    "NOT committed, NOT pushed."
                                )
                                return res
                        if _has_unmerged_paths(store):
                            # The rebase completed (branch moved), but
                            # the trailing autostash-pop conflicted —
                            # git doesn't reflect that in the rebase's
                            # exit code. #25: if the pop conflict is
                            # STATE.md-only, resolve it in place and
                            # drop the retained autostash; otherwise
                            # undo the rebase and restore the original
                            # dirty tree exactly: ORIG_HEAD is the
                            # pre-rebase commit, and the stash (never
                            # dropped because the pop conflicted)
                            # re-applies cleanly there.
                            if _auto_resolve_state_conflicts(store, prefer_local=True):
                                _git(store, "stash", "drop")
                            else:
                                _git(store, "reset", "--hard", "ORIG_HEAD")
                                _git(store, "stash", "pop")
                                res["conflict"] = True
                                res["reason"] = (
                                    "pull --rebase conflict before commit — "
                                    "resolve ~/okfmem-store by hand; "
                                    "NOT committed, NOT pushed."
                                )
                                return res
                # no upstream configured — nothing to integrate, fall through.
            # fetch failure (offline) is swallowed — fall through and commit
            # locally; the push attempt below will surface as push_error.

        _git(store, "add", "-A")
        staged = _git(store, "diff", "--cached", "--quiet")
        has_changes = staged.returncode != 0

        if has_changes:
            c = _git(store, "commit", "-m", message)
            if c.returncode != 0:
                res["reason"] = c.stdout.strip() or c.stderr.strip() or "commit failed."
                return res
            res["committed"] = True
            sha = _git(store, "rev-parse", "--short", "HEAD")
            res["sha"] = sha.stdout.strip() if sha.returncode == 0 else None
            res["reason"] = f"committed {res['sha']}: {message}"
        else:
            res["reason"] = "no changes to commit."

        if not do_push:
            return res

        if not has_changes:
            # Nothing new was committed this call — usually the pre-commit
            # pull above already brought us current, so skip the no-op `git
            # push` round-trip. But NOT always: an earlier offline sync may
            # have left a committed-but-unpushed local commit that the
            # rebase just replayed (e.g. its STATE.md conflict was
            # auto-resolved, #25) — HEAD is then ahead of @{u} with nothing
            # newly staged, and returning here would strand it locally.
            head = _git(store, "rev-parse", "HEAD")
            upstream = _git(store, "rev-parse", "@{u}")
            if (
                head.returncode != 0
                or upstream.returncode != 0
                or head.stdout.strip() == upstream.stdout.strip()
            ):
                return res
            res["reason"] = "no new changes; pushing existing unpushed local commit(s)."

        p = _git(store, "push")
        if p.returncode == 0:
            res["pushed"] = True
        else:
            res["push_error"] = p.stderr.strip() or "push failed."
        return res
    finally:
        _release_lock(store, lock)


def main():
    ap = argparse.ArgumentParser(description="Commit + push the okfmem store.")
    ap.add_argument(
        "--store",
        default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")),
    )
    ap.add_argument("-m", "--message", help="commit subject (prompted for if omitted)")
    ap.add_argument("--no-push", action="store_true", help="commit but don't push")
    ap.add_argument(
        "--no-pull",
        action="store_true",
        help="don't pull --rebase before push (single-machine)",
    )
    args = ap.parse_args()

    # A bare `okfmem sync` shouldn't be a usage error -- ask for the subject,
    # offering a timestamped default so Enter alone works. input() is the
    # ground truth for interactivity (not a pre-detected isatty guess): on a
    # non-interactive stdin it raises EOFError immediately, and the documented
    # default is taken so piped/unattended callers keep working. Ctrl+C aborts
    # before anything is committed. In-process callers (memory_consolidate)
    # pass `message` to sync_store directly and never reach this.
    message = args.message
    if not message:
        default = "okfmem sync " + datetime.now().strftime("%Y-%m-%d %H:%M")
        try:
            answer = input(f"Commit message [{default}]: ").strip()
        except EOFError:
            answer = ""  # no interactive stdin -- take the default
        except KeyboardInterrupt:
            print("\nokfmem sync: aborted -- nothing committed.")
            sys.exit(130)
        message = answer or default

    res = sync_store(
        args.store, message, do_push=not args.no_push, do_pull=not args.no_pull
    )
    print(f"okfmem sync: {res['reason']}")
    if res["pushed"]:
        print("  pushed.")
    elif res["push_error"]:
        print(f"  push failed: {res['push_error']}")
    # Non-zero exit only on a conflict the user must resolve.
    sys.exit(1 if res["conflict"] else 0)


if __name__ == "__main__":
    main()

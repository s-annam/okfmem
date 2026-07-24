#!/usr/bin/env python3
"""okfmem pull — the read-side sync primitive.

`okfmem sync` (memory_sync.py) only reaches its `pull --rebase` step because
something local got staged first — a machine that only *reads* memory
(loads STATE.md/MEMORY.md, never edits) has nothing to stage, so `sync` is a
clean no-op and never pulls. `okfmem pull` is the standalone counterpart:
fetch + integrate the resolved store regardless of local changes.

Sequence:  fetch  →  no-op if already up to date  →
           (clean tree) pull --ff-only, else pull --rebase --autostash

Fail-open contract (this is the part #16's SessionStart hook depends on):
  - Offline / fetch failure: swallowed. Never raises, never exits non-zero.
  - Rebase conflict confined to per-project STATE.md snapshots: auto-resolved
    (#25) — STATE.md is a whole-file last-write-wins snapshot, so the newer
    `modified:` frontmatter timestamp wins (tie → the incoming side; a
    read-side pull is consuming, not saving). Any conflict touching a durable
    page or MEMORY.md: the half-done rebase is aborted (tree left clean) and
    reported; `okfmem pull` run manually exits 1 so a human notices and
    resolves the store by hand. This is the ONLY non-zero exit case.
  - No upstream configured, or already up to date: clean no-op, exit 0.
A caller that must never block (a SessionStart hook) can invoke this command
as-is and either ignore its exit code entirely or treat 1 as "needs a human,
but don't fail the session" — it never hangs and never leaves a half-done
rebase behind.

Shares its lockfile (`<store>/.git/okfmem-sync.lock`, #27) and git helpers with
memory_sync.py so a concurrent `okfmem sync` and `okfmem pull` on the same
store serialize instead of racing.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import os
import subprocess
import sys

import memory_sync as ms  # reuse _git / lock helpers — one lock, one lock file

# Keep network calls short: this may run on a SessionStart hook path and must
# never make a session wait meaningfully long for a dead network.
DEFAULT_TIMEOUT = 30


def _safe_git(store, *args, timeout=DEFAULT_TIMEOUT):
    """Like memory_sync._git but never raises. Offline networks, a missing
    git binary, or a hung remote all collapse into a synthetic non-zero
    CompletedProcess so every caller here can fail open uniformly instead of
    each needing its own try/except."""
    try:
        return ms._git(store, *args, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, 1, stdout="",
                                           stderr="git timed out (offline?)")
    except OSError as e:
        return subprocess.CompletedProcess(args, 1, stdout="", stderr=str(e))


def _rebase_in_progress(store):
    """True if `store` is mid-rebase (an interactive/manual rebase the user is
    hand-resolving). Resolves the real git dir so a worktree `.git` *file* is
    handled too, but falls back to `<store>/.git` for a normal clone."""
    git_dir = None
    gd = _safe_git(store, "rev-parse", "--git-dir")
    if gd.returncode == 0 and gd.stdout.strip():
        git_dir = gd.stdout.strip()
        if not os.path.isabs(git_dir):
            git_dir = os.path.join(store, git_dir)
    if git_dir is None:
        git_dir = os.path.join(store, ".git")
    return (os.path.isdir(os.path.join(git_dir, "rebase-merge"))
            or os.path.isdir(os.path.join(git_dir, "rebase-apply")))


def pull_store(store, timeout=DEFAULT_TIMEOUT):
    """Fetch + integrate remote changes into `store`. Returns a dict:

        {pulled: bool, up_to_date: bool, conflict: bool, offline: bool,
         reason: str}

    Never raises. Exactly one of pulled/up_to_date/conflict/offline is the
    "headline" outcome; reason is always a short human-readable string.
    """
    store = os.path.abspath(os.path.expanduser(store))
    res = {"pulled": False, "up_to_date": False, "conflict": False,
           "offline": False, "reason": ""}

    if not os.path.isdir(os.path.join(store, ".git")):
        res["reason"] = f"{store} is not a git repo — nothing to pull."
        return res

    lock = None
    try:
        # Short lock timeout: this may run on the SessionStart path and must
        # never make a session wait meaningfully long. A concurrent okfmem
        # sync/pull (a just-ended session's Stop hook, or a second window)
        # holding the lock is a clean no-op skip, not a 60s stall.
        lock = ms._acquire_lock(store, timeout=2)
        if lock is None:
            res["reason"] = "another okfmem sync/pull holds the lock; skipped."
            return res

        fetch = _safe_git(store, "fetch", timeout=timeout)
        if fetch.returncode != 0:
            res["offline"] = True
            res["reason"] = fetch.stderr.strip() or "fetch failed (offline?)."
            return res

        upstream = _safe_git(store, "rev-parse", "@{u}")
        if upstream.returncode != 0:
            res["reason"] = "no upstream configured — nothing to pull."
            return res

        local = _safe_git(store, "rev-parse", "HEAD")
        if local.returncode == 0 and local.stdout.strip() == upstream.stdout.strip():
            res["up_to_date"] = True
            res["reason"] = "already up to date."
            return res

        # A clean tree can take the cheap fast-forward path. A dirty tree (or
        # a history that isn't a fast-forward) falls back to rebase, stashing
        # any local edits around it so they survive the rebase.
        status = _safe_git(store, "status", "--porcelain")
        clean = status.returncode == 0 and not status.stdout.strip()

        if clean:
            ff = _safe_git(store, "pull", "--ff-only", timeout=timeout)
            if ff.returncode == 0:
                res["pulled"] = True
                res["reason"] = "fast-forwarded."
                return res
            # Not a fast-forward (diverged history) — fall through to rebase.

        # If the store is ALREADY mid-rebase, a user is hand-resolving a
        # conflict in it. `pull --rebase` would fail instantly ("a rebase is
        # in progress") and the self-conflict `rebase --abort` below would
        # then discard their partial resolution. Bail out fail-open instead:
        # don't pull, don't abort.
        if _rebase_in_progress(store):
            res["reason"] = ("a rebase is already in progress in the store; "
                             "skipped.")
            return res

        rb = _safe_git(store, "pull", "--rebase", "--autostash", timeout=timeout)
        if rb.returncode != 0:
            # #25: a conflict confined to STATE.md snapshots is decided by
            # the newer `modified:` frontmatter (prefer_local=False — a
            # read-side pull prefers the incoming snapshot on a tie);
            # anything touching a durable page or MEMORY.md falls through
            # to the normal abort so a human reconciles it. Post-fetch the
            # rebase is local-only, so ms helpers (no _safe_git wrapper)
            # are fine here.
            if not ms._finish_rebase_with_state_autoresolve(
                    store, prefer_local=False):
                _safe_git(store, "rebase", "--abort")
                res["conflict"] = True
                res["reason"] = ("pull --rebase conflict — resolve the store "
                                 "by hand; tree left clean (rebase aborted).")
                return res
        if ms._has_unmerged_paths(store):
            # The rebase completed (branch moved) but the trailing
            # autostash-pop conflicted — git exits 0 for that, so it must be
            # caught here (same latent gap #26 closed in memory_sync). #25:
            # a STATE.md-only pop conflict is resolved in place (newer
            # `modified:` wins, incoming on a tie) and the retained
            # autostash dropped; the mixed reset then unstages the
            # resolution so surviving local edits sit in the working tree
            # exactly as a clean autostash-pop would have left them.
            # Anything else: undo the rebase (ORIG_HEAD) and re-apply the
            # stash, restoring the original dirty tree for a human.
            if ms._auto_resolve_state_conflicts(store, prefer_local=False):
                _safe_git(store, "stash", "drop")
                _safe_git(store, "reset", "-q")
            else:
                _safe_git(store, "reset", "--hard", "ORIG_HEAD")
                _safe_git(store, "stash", "pop")
                res["conflict"] = True
                res["reason"] = ("pull --rebase autostash conflict — resolve "
                                 "the store by hand; original tree restored "
                                 "(rebase undone).")
                return res
        res["pulled"] = True
        res["reason"] = "rebased onto remote."
        return res
    except (OSError, subprocess.TimeoutExpired) as e:
        # "Never raises" contract: a lock-acquire or git-region OSError
        # (perm-denied, disk full, ...) must not surface as a SessionStart
        # traceback — nor a TimeoutExpired from the unwrapped ms._git calls
        # the #25 auto-resolve region makes. Collapse to a clean fail-open
        # no-op result.
        res["pulled"] = False
        res["up_to_date"] = False
        res["conflict"] = False
        res["offline"] = False
        res["reason"] = f"unexpected error; skipped ({e})."
        return res
    finally:
        if lock is not None:
            ms._release_lock(store, lock)


def unlinked_repo_notice(store):
    """Return a one-shot reminder when the session's repo has no memory link
    yet, else None.

    This runs on the SessionStart path, which is the ONLY okfmem surface that
    fires in a repo the user never ran `okfmem init` in -- and an unlinked repo
    fails invisibly (the agent simply never remembers anything), so silence is
    the wrong default. Deliberately printed even under `--quiet`: SessionStart
    stdout reaches the agent as context, and this is the one line worth the
    interruption. Never raises -- the fail-open contract covers this too.
    """
    try:
        engine = os.path.dirname(os.path.realpath(__file__))
        if engine not in sys.path:
            sys.path.insert(0, engine)
        import memory_init as mi

        state, name = mi.project_link_state(store)
        if state != "unlinked":
            return None
        return (
            f"okfmem: this repo ('{name}') is NOT wired to the memory store, "
            "so nothing said here will be remembered next session. Tell the "
            "user to run `okfmem init` once from this repo -- then continue "
            "with their request."
        )
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser(
        description="Fetch + integrate remote changes into the okfmem store "
                    "(fast-forward when clean, rebase --autostash otherwise).")
    ap.add_argument("--store", default=os.environ.get("OKFMEM_STORE",
                    os.path.expanduser("~/okfmem-store")))
    ap.add_argument("--quiet", action="store_true",
                    help="suppress output (for hook/automation use) -- the "
                         "unlinked-repo reminder still prints, since a silent "
                         "unlinked repo is the failure it exists to catch")
    args = ap.parse_args()

    res = pull_store(args.store)
    if not args.quiet:
        print(f"okfmem pull: {res['reason']}")

    notice = unlinked_repo_notice(args.store)
    if notice:
        print(notice)

    # Fail-open: non-zero ONLY on a rebase conflict a human must resolve.
    # Offline / no-upstream / already-up-to-date all exit 0 so an automated
    # caller (a SessionStart hook) never trips on this.
    sys.exit(1 if res["conflict"] else 0)


if __name__ == "__main__":
    main()

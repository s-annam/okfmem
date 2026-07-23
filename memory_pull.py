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
  - Rebase conflict: the half-done rebase is aborted (tree left clean) and
    reported; `okfmem pull` run manually exits 1 so a human notices and
    resolves the store by hand. This is the ONLY non-zero exit case.
  - No upstream configured, or already up to date: clean no-op, exit 0.
A caller that must never block (a SessionStart hook) can invoke this command
as-is and either ignore its exit code entirely or treat 1 as "needs a human,
but don't fail the session" — it never hangs and never leaves a half-done
rebase behind.

Shares its lockfile (`<store>/.okfmem-sync.lock`) and git helpers with
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
            _safe_git(store, "rebase", "--abort")
            res["conflict"] = True
            res["reason"] = ("pull --rebase conflict — resolve the store by "
                             "hand; tree left clean (rebase aborted).")
            return res
        res["pulled"] = True
        res["reason"] = "rebased onto remote."
        return res
    except OSError as e:
        # "Never raises" contract: a lock-acquire or git-region OSError
        # (perm-denied, disk full, ...) must not surface as a SessionStart
        # traceback. Collapse to a clean fail-open no-op result.
        res["pulled"] = False
        res["up_to_date"] = False
        res["conflict"] = False
        res["offline"] = False
        res["reason"] = f"unexpected error; skipped ({e})."
        return res
    finally:
        if lock is not None:
            ms._release_lock(store, lock)


def main():
    ap = argparse.ArgumentParser(
        description="Fetch + integrate remote changes into the okfmem store "
                    "(fast-forward when clean, rebase --autostash otherwise).")
    ap.add_argument("--store", default=os.environ.get("OKFMEM_STORE",
                    os.path.expanduser("~/okfmem-store")))
    ap.add_argument("--quiet", action="store_true",
                    help="suppress output (for hook/automation use)")
    args = ap.parse_args()

    res = pull_store(args.store)
    if not args.quiet:
        print(f"okfmem pull: {res['reason']}")

    # Fail-open: non-zero ONLY on a rebase conflict a human must resolve.
    # Offline / no-upstream / already-up-to-date all exit 0 so an automated
    # caller (a SessionStart hook) never trips on this.
    sys.exit(1 if res["conflict"] else 0)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""okfmem sync — the single git commit+push path for the store.

Both callers share this one code path so the pull-rebase + lock logic lives in
exactly one place instead of being duplicated (and drifting) between them:

  - `memory_consolidate.py` (P3 Stop-hook job) imports `sync_store` in-process.
  - the `/okfmem-save` skill shells `okfmem sync -m "<summary>"`.

Sequence:  add -A  →  (if staged) pull --rebase  →  commit  →  push

Guards:
  - An flock lockfile (`<store>/.okfmem-sync.lock`) serializes concurrent
    windows so two sessions never race on the same working tree.
  - A `pull --rebase` conflict aborts the half-done rebase and refuses to push
    (never force) — the caller surfaces the conflict to the user.
  - No-op + clean return when nothing is staged (no empty commits).

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import fcntl
import os
import subprocess
import sys
import time


def _git(store, *args, timeout=120):
    return subprocess.run(["git", "-C", store, *args],
                          capture_output=True, text=True, timeout=timeout)


def _acquire_lock(store, timeout=60):
    """Blocking flock on a per-store lockfile. Returns the open fd (keep it
    alive until the sync finishes) or None if the lock can't be taken in time."""
    path = os.path.join(store, ".okfmem-sync.lock")
    fd = open(path, "w")
    deadline = time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                fd.close()
                return None
            time.sleep(0.5)


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
    res = {"committed": False, "sha": None, "pushed": False,
           "reason": "", "push_error": None, "conflict": False}

    lock = _acquire_lock(store)
    if lock is None:
        res["reason"] = "another okfmem sync holds the lock; skipped."
        return res
    try:
        _git(store, "add", "-A")
        staged = _git(store, "diff", "--cached", "--quiet")
        if staged.returncode == 0:
            res["reason"] = "no changes to commit."
            return res

        c = _git(store, "commit", "-m", message)
        if c.returncode != 0:
            res["reason"] = (c.stdout.strip() or c.stderr.strip()
                             or "commit failed.")
            return res
        res["committed"] = True
        sha = _git(store, "rev-parse", "--short", "HEAD")
        res["sha"] = sha.stdout.strip() if sha.returncode == 0 else None
        res["reason"] = f"committed {res['sha']}: {message}"

        if not do_push:
            return res

        if do_pull:
            pr = _git(store, "pull", "--rebase")
            if pr.returncode != 0:
                _git(store, "rebase", "--abort")
                res["conflict"] = True
                res["reason"] = ("pull --rebase conflict — resolve "
                                 "~/okfmem-store by hand; NOT pushed.")
                return res

        p = _git(store, "push")
        if p.returncode == 0:
            res["pushed"] = True
        else:
            res["push_error"] = p.stderr.strip() or "push failed."
        return res
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def main():
    ap = argparse.ArgumentParser(description="Commit + push the okfmem store.")
    ap.add_argument("--store", default=os.environ.get("OKFMEM_STORE",
                    os.path.expanduser("~/okfmem-store")))
    ap.add_argument("-m", "--message", required=True, help="commit subject")
    ap.add_argument("--no-push", action="store_true", help="commit but don't push")
    ap.add_argument("--no-pull", action="store_true",
                    help="don't pull --rebase before push (single-machine)")
    args = ap.parse_args()

    res = sync_store(args.store, args.message,
                     do_push=not args.no_push, do_pull=not args.no_pull)
    print(f"okfmem sync: {res['reason']}")
    if res["pushed"]:
        print("  pushed.")
    elif res["push_error"]:
        print(f"  push failed: {res['push_error']}")
    # Non-zero exit only on a conflict the user must resolve.
    sys.exit(1 if res["conflict"] else 0)


if __name__ == "__main__":
    main()

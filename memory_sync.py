#!/usr/bin/env python3
"""okfmem sync — the single git commit+push path for the store.

Both callers share this one code path so the pull-rebase + lock logic lives in
exactly one place instead of being duplicated (and drifting) between them:

  - `memory_consolidate.py` (P3 Stop-hook job) imports `sync_store` in-process.
  - the `/okfmem-save` skill shells `okfmem sync -m "<summary>"`.

Sequence:  add -A  →  (if staged) pull --rebase  →  commit  →  push

Guards:
  - A lockfile (`<store>/.okfmem-sync.lock`) serializes concurrent windows so
    two sessions never race on the same working tree. It's a single pure-
    stdlib path on every platform (POSIX and Windows alike): `os.open` with
    `O_CREAT | O_EXCL` is atomic everywhere, so there is no fcntl/msvcrt
    branching. A lock older than `LOCK_STALE_SECONDS` is assumed to be left
    over from a process that died without releasing it and is broken rather
    than deadlocking forever.
  - A `pull --rebase` conflict aborts the half-done rebase and refuses to push
    (never force) — the caller surfaces the conflict to the user.
  - No-op + clean return when nothing is staged (no empty commits).

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import os
import subprocess
import sys
import time

# A lock older than this is treated as abandoned (owning process died without
# releasing it) and is broken rather than left to deadlock future syncs.
LOCK_STALE_SECONDS = 600  # 10 minutes


def _git(store, *args, timeout=120):
    return subprocess.run(["git", "-C", store, *args],
                          capture_output=True, text=True, timeout=timeout)


def _lock_path(store):
    return os.path.join(store, ".okfmem-sync.lock")


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
        return False          # no such process — genuinely gone
    except PermissionError:
        return True           # exists but owned by another user
    except OSError:
        return None           # can't determine — defer to age


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
            if (age is not None and age > LOCK_STALE_SECONDS
                    and owner_alive is not True):
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
        _release_lock(store, lock)


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

import os
import time

import pytest

import memory_sync as ms


def test_acquire_creates_lockfile_and_release_removes_it(tmp_path):
    store = str(tmp_path)
    fd = ms._acquire_lock(store, timeout=5)
    assert fd is not None
    lock_path = ms._lock_path(store)
    assert os.path.isfile(lock_path)

    ms._release_lock(store, fd)
    assert not os.path.exists(lock_path)


def test_release_is_idempotent_when_file_already_gone(tmp_path):
    store = str(tmp_path)
    fd = ms._acquire_lock(store, timeout=5)
    assert fd is not None

    # First release closes the fd and removes the lockfile.
    ms._release_lock(store, fd)
    assert not os.path.exists(ms._lock_path(store))

    # A second release (fd already closed, file already gone) must be a silent
    # no-op, not raise. Done this way rather than os.remove()-ing the lockfile
    # under the still-open fd, which Windows forbids (WinError 32: cannot
    # delete a file held open by another handle).
    ms._release_lock(store, fd)


def test_second_acquire_fails_while_first_holds_the_lock(tmp_path):
    store = str(tmp_path)
    fd1 = ms._acquire_lock(store, timeout=5)
    assert fd1 is not None
    try:
        # Short timeout — the lock is fresh, not stale, so this must fail
        # rather than break through.
        fd2 = ms._acquire_lock(store, timeout=1)
        assert fd2 is None
    finally:
        ms._release_lock(store, fd1)


def test_stale_lock_is_broken_and_reacquired(tmp_path):
    store = str(tmp_path)
    lock_path = ms._lock_path(store)
    with open(lock_path, "w") as f:
        f.write("99999 0\n")
    old_time = time.time() - (ms.LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old_time, old_time))

    fd = ms._acquire_lock(store, timeout=5)
    assert fd is not None
    ms._release_lock(store, fd)


def test_fresh_lock_is_not_treated_as_stale(tmp_path):
    store = str(tmp_path)
    fd1 = ms._acquire_lock(store, timeout=5)
    assert fd1 is not None
    try:
        # Well within LOCK_STALE_SECONDS — must not be broken.
        fd2 = ms._acquire_lock(store, timeout=1)
        assert fd2 is None
    finally:
        ms._release_lock(store, fd1)


def test_failed_stale_break_still_times_out(tmp_path, monkeypatch):
    """A stale lock whose file can't be removed (e.g. Windows holding it open)
    must NOT busy-loop forever — the acquire honors the timeout and returns
    None. Regression test for the stale-break infinite-spin bug."""
    store = str(tmp_path)
    lock_path = ms._lock_path(store)
    with open(lock_path, "w") as f:
        f.write("999999 0\n")  # PID that (almost certainly) doesn't exist
    old = time.time() - (ms.LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old, old))

    def _boom(*a, **k):
        raise OSError("cannot remove — still held open")

    monkeypatch.setattr(ms.os, "remove", _boom)

    start = time.monotonic()
    fd = ms._acquire_lock(store, timeout=1)
    elapsed = time.monotonic() - start
    assert fd is None
    assert elapsed < 5  # honored the ~1s timeout instead of hanging


@pytest.mark.skipif(os.name == "nt",
                    reason="POSIX os.kill(pid, 0) liveness probe")
def test_stale_looking_lock_with_live_owner_is_not_broken(tmp_path):
    """A lock old enough to look stale but owned by a still-running process
    (our own PID) must NOT be broken — a long sync is not a dead one."""
    store = str(tmp_path)
    lock_path = ms._lock_path(store)
    with open(lock_path, "w") as f:
        f.write(f"{os.getpid()} 0\n")  # our own PID — definitely alive
    old = time.time() - (ms.LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old, old))

    fd = ms._acquire_lock(store, timeout=1)
    assert fd is None                 # not broken -> acquire fails on timeout
    assert os.path.isfile(lock_path)  # lockfile left untouched


def test_corrupt_lockfile_falls_back_to_age(tmp_path):
    """A corrupt/empty lockfile (no parseable PID) is judged by age alone: if
    stale, it's still broken and re-acquired."""
    store = str(tmp_path)
    lock_path = ms._lock_path(store)
    with open(lock_path, "w") as f:
        f.write("")  # empty — no PID to read
    old = time.time() - (ms.LOCK_STALE_SECONDS + 60)
    os.utime(lock_path, (old, old))

    fd = ms._acquire_lock(store, timeout=5)
    assert fd is not None
    ms._release_lock(store, fd)

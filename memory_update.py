#!/usr/bin/env python3
"""okfmem — engine self-update (git pull + re-run init).

The engine is a git clone. Updating it is `git pull --ff-only` in that clone,
followed by re-running `init` so any new or changed skills relink and the managed
pointer blocks refresh. install.sh / install.ps1 bootstrap the first time;
`okfmem update` is every time after.

  okfmem update            # fast-forward the engine, then re-run init
  okfmem update --check    # report whether a newer version exists; change nothing

Fast-forward only: if you have committed local engine changes that diverge from
origin, the pull refuses rather than clobbering them — stash or branch first. A
dirty working tree is likewise refused up front, before anything is fetched.

Store passthrough: --store (else $OKFMEM_STORE, else ~/okfmem-store) is forwarded
to the init step, which resolves it the same way every other subcommand does.
"""
import argparse
import os
import subprocess
import sys

# realpath: the CLI is reached through a symlink/wrapper; resolve to the real
# engine dir so git operates on the clone, not on ~/.local/bin.
ENGINE = os.path.dirname(os.path.realpath(__file__))


def _git(*args, check=False):
    return subprocess.run(["git", "-C", ENGINE, *args],
                          capture_output=True, text=True, check=check)


def _short(ref="HEAD"):
    r = _git("rev-parse", "--short", ref)
    return r.stdout.strip() if r.returncode == 0 else "?"


def _upstream():
    """The current branch's upstream (e.g. `origin/main`), or None if unset."""
    r = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return r.stdout.strip() if r.returncode == 0 else None


def _ensure_git_repo():
    r = _git("rev-parse", "--is-inside-work-tree")
    if r.returncode != 0 or r.stdout.strip() != "true":
        print(f"error: engine dir is not a git clone: {ENGINE}", file=sys.stderr)
        print("  okfmem update expects the engine installed via `git clone`; "
              "re-clone to enable updates.", file=sys.stderr)
        sys.exit(2)


def cmd_check():
    """Report behind/ahead relative to upstream. Fetches, changes nothing."""
    up = _upstream()
    if not up:
        print("cannot check: current branch has no upstream tracking branch")
        sys.exit(1)
    fetch = _git("fetch", "--quiet")
    if fetch.returncode != 0:
        print(f"error: git fetch failed:\n{fetch.stderr.strip()}", file=sys.stderr)
        sys.exit(1)
    behind = _git("rev-list", "--count", f"HEAD..{up}").stdout.strip() or "0"
    ahead = _git("rev-list", "--count", f"{up}..HEAD").stdout.strip() or "0"
    if behind == "0" and ahead == "0":
        print(f"up to date ({_short()}, tracking {up})")
    elif behind != "0":
        print(f"update available: {behind} commit(s) behind {up} "
              f"(local {_short()} -> {_short(up)})")
        if ahead != "0":
            print(f"  note: also {ahead} local commit(s) ahead — the pull will "
                  f"refuse (diverged history); stash or branch them first")
        print("  run: okfmem update")
    else:
        print(f"ahead of {up} by {ahead} commit(s); nothing to pull")


def cmd_update(store_args):
    """Fast-forward the engine, then re-run init. Exits with init's code."""
    # Refuse on a dirty tree: a ff pull can fail partway or conflict, and we
    # must never clobber a user's local engine edits.
    dirty = _git("status", "--porcelain").stdout.strip()
    if dirty:
        print("error: engine has uncommitted local changes — refusing to update.",
              file=sys.stderr)
        print("  stash or discard them, then re-run `okfmem update`:", file=sys.stderr)
        print(f'    git -C "{ENGINE}" stash', file=sys.stderr)
        sys.exit(1)

    before = _short()
    pull = _git("pull", "--ff-only")
    sys.stdout.write(pull.stdout)
    if pull.returncode != 0:
        print(pull.stderr.strip(), file=sys.stderr)
        print("error: fast-forward pull failed (diverged history or no upstream).",
              file=sys.stderr)
        print("  if you have local engine commits, stash or branch them first.",
              file=sys.stderr)
        sys.exit(1)
    after = _short()

    if before == after:
        print(f"=> engine already up to date ({after})")
    else:
        print(f"=> engine updated {before} -> {after}")

    # Re-run init: relink new/changed skills, refresh managed pointer blocks.
    # Idempotent — the same flow install runs, safe to repeat.
    print("=> re-running init (relink skills, refresh pointers)...")
    init = os.path.join(ENGINE, "memory_init.py")
    r = subprocess.run([sys.executable, init, *store_args])
    sys.exit(r.returncode)


def main():
    ap = argparse.ArgumentParser(prog="okfmem update")
    ap.add_argument("--check", action="store_true",
                    help="report whether a newer version exists; change nothing")
    ap.add_argument("--store", help="store path, forwarded to the init step "
                    "(else $OKFMEM_STORE, else ~/okfmem-store)")
    args = ap.parse_args()

    _ensure_git_repo()
    if args.check:
        cmd_check()
    else:
        store_args = ["--store", args.store] if args.store else []
        cmd_update(store_args)


if __name__ == "__main__":
    main()

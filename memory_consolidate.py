#!/usr/bin/env python3
"""okfmem P3 — sleep-time consolidation job (Component 3).

Runs at Claude Code session end (Stop hook) — or by hand — to keep the OKF
store self-maintaining without `/memory-curate`:

  1. Access tracking. Parse the session transcript for reads/greps of pages
     under any `projects/*/` memory dir; bump `last_accessed`/`access_count`.
  2. Decay scoring. `R = exp(-t_days / S)`, `S = access_count + 1`,
     `t_days` = days since `last_accessed` (MemoryBank Ebbinghaus).
  3. Graceful archival (never delete). Move page → `projects/<proj>/archive/`,
     set `status: archived`, drop its `MEMORY.md` line, iff:
       not pinned AND type not in {user,feedback}
       AND t_days > 30 AND R < 0.40 AND age > 14d
     Cap: <= N archives/run (default 20).
  4. Commit + push the store (guarded; reuses git, no daemon).

Guardrails: `--dry-run` prints the plan and writes nothing; never `rm` (archive
+ git history are the backstop); never touch `type: user|feedback` or
`pinned: true`; refuses to run in apply mode if the store working tree already
has uncommitted changes (unless --force / --no-commit) so it never sweeps
unrelated edits into its commit.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.

Access tracking is Claude-Code-only in v1 (Antigravity has no Stop hook); it is
best-effort — if the transcript is missing or unreadable, decay proceeds on
pure age (no LRU reinforcement), which is still correct.
"""
import argparse
import glob
import json
import math
import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone

# Shared git commit+push path (pull-rebase + lock) lives beside this script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_sync import sync_store  # noqa: E402

SKIP_NAMES = {"MEMORY.md", "STATE.md", "CONTEXT.md"}
DECAY_EXEMPT_TYPES = {"user", "feedback"}

OPEN_RE = re.compile(r"^---(\r\n|\n)")
TOP_TYPE_RE = re.compile(r"^type:\s*(.*?)\s*$")

ARCHIVE_R_MAX = 0.40      # R below this is a decay candidate
ARCHIVE_T_DAYS = 30       # untouched-for-a-month plain-language gate
ARCHIVE_AGE_DAYS = 14     # never archive a page younger than this
ARCHIVE_CAP = 20          # max archives per run (mass-sweep backstop)


# ---------------------------------------------------------------------------
# frontmatter parsing (byte-preserving; no PyYAML — matches sibling okf-*.py)
# ---------------------------------------------------------------------------
def find_frontmatter(text):
    """Return (eol, body_start, body_end) for the leading YAML block, or None."""
    m = OPEN_RE.match(text)
    if not m:
        return None
    eol = m.group(1)
    body_start = m.end()
    close_re = re.compile(r"(\r\n|\n)---[ \t]*(\r\n|\n|$)")
    cm = close_re.search(text, body_start - len(eol))
    if not cm:
        return None
    body_end = cm.start() + len(cm.group(1))
    return eol, body_start, body_end


def parse_fields(text, fm):
    """Top-level scalar key -> raw string value, over the frontmatter body."""
    eol, body_start, body_end = fm
    fields = {}
    for ln in text[body_start:body_end].split(eol):
        m = re.match(r"^([A-Za-z_][\w-]*):\s?(.*)$", ln)
        if m:
            fields[m.group(1)] = m.group(2).strip()
    return fields


def update_fields(text, fm, updates):
    """Return text with each key in `updates` replaced in place (or appended
    just before the closing `---`), preserving EOL style and all other bytes."""
    eol, body_start, body_end = fm
    head, body, tail = text[:body_start], text[body_start:body_end], text[body_end:]
    remaining = dict(updates)
    out_lines = []
    for ln in body.split(eol):
        m = re.match(r"^([A-Za-z_][\w-]*):", ln)
        if m and m.group(1) in remaining:
            key = m.group(1)
            out_lines.append(f"{key}: {remaining.pop(key)}")
        else:
            out_lines.append(ln)
    new_body = eol.join(out_lines)
    appended = "".join(f"{k}: {v}{eol}" for k, v in remaining.items())
    return head + new_body + appended + tail


# ---------------------------------------------------------------------------
# access tracking
# ---------------------------------------------------------------------------
def build_alias_index(store):
    """Map each store project dir -> list of Claude-Code symlink alias dirs.

    A page at `<store>/projects/<name>/<rel>` is read by the agent through the
    symlink `~/.claude/projects/<enc>/memory/<rel>`, so both spellings must be
    matched in the transcript. Returns {abs project dir: [alias dir, ...]}.
    """
    aliases = {}
    pattern = os.path.expanduser("~/.claude/projects/*/memory")
    for link in glob.glob(pattern):
        if not os.path.islink(link):
            continue
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        aliases.setdefault(target, []).append(os.path.abspath(link))
    return aliases


def load_transcript(args):
    """Resolve transcript text from --transcript, or a Stop-hook JSON on stdin."""
    path = args.transcript
    if not path and args.stdin_hook:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
            path = payload.get("transcript_path")
        except (ValueError, OSError):
            path = None
    if not path:
        return None
    path = os.path.expanduser(path)
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read()
    except OSError:
        return None


def page_touched(transcript, real_path, alias_dirs, proj_dir):
    """True if the transcript references this page by store or symlink path."""
    if transcript is None:
        return False
    rel = os.path.relpath(real_path, proj_dir)
    candidates = [real_path]
    for adir in alias_dirs:
        candidates.append(os.path.join(adir, rel))
    return any(c in transcript for c in candidates)


# ---------------------------------------------------------------------------
# decay scoring
# ---------------------------------------------------------------------------
def parse_date(s, fallback):
    try:
        return datetime.strptime(s.strip(), "%Y-%m-%d").date()
    except (ValueError, AttributeError):
        return fallback


def resolve_epoch(store, today, dry_run):
    """Decay epoch = the day access-tracking went live for this store.

    Cold-start guard: backfill seeded `last_accessed = created` (git-creation
    date), so a page written months ago but never re-read looks maximally
    decayed the instant the system turns on — which would sweep the whole store
    into archive 20/run. The timer that actually matters is "untouched *since
    tracking began*", so archival compares against `max(last_accessed, epoch)`.
    Every page thus gets a fair 30-day window post-go-live to prove usage.

    Persisted in `<store>/decay_state.json`; created on first apply run.
    """
    path = os.path.join(store, "decay_state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            ep = parse_date(json.load(f).get("epoch", ""), None)
            if ep:
                return ep
    except (OSError, ValueError):
        pass
    if not dry_run:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"epoch": today.isoformat()}, f)
                f.write("\n")
        except OSError:
            pass
    return today


def retention(t_days, access_count):
    s = access_count + 1
    return math.exp(-max(t_days, 0) / s)


# ---------------------------------------------------------------------------
# MEMORY.md line removal
# ---------------------------------------------------------------------------
def drop_memory_lines(memory_path, slugs, dry_run):
    """Remove index lines pointing at any archived slug. Returns dropped count."""
    if not slugs or not os.path.isfile(memory_path):
        return 0
    with open(memory_path, "r", encoding="utf-8", newline="") as f:
        text = f.read()
    eol = "\r\n" if "\r\n" in text else "\n"
    # match `](slug.md)`, `](sub/slug.md)`, or a leading `- slug.md ` bullet
    pats = [re.compile(r"[(/]" + re.escape(s) + r"\.md\)") for s in slugs]
    pats += [re.compile(r"^\s*[-*]\s+" + re.escape(s) + r"\.md\b") for s in slugs]
    kept, dropped = [], 0
    for ln in text.split(eol):
        if any(p.search(ln) for p in pats):
            dropped += 1
            continue
        kept.append(ln)
    if dropped and not dry_run:
        with open(memory_path, "w", encoding="utf-8", newline="") as f:
            f.write(eol.join(kept))
    return dropped


# ---------------------------------------------------------------------------
# main pass
# ---------------------------------------------------------------------------
def scan_project(proj_dir, transcript, alias_dirs, today, epoch):
    """Return (bumps, candidates) for one project.

    bumps: [(path, fm-updates)] access-reinforced pages (write even if kept).
    candidates: [dict] archivable pages, each with r/t_days/slug for sorting.
    """
    bumps, candidates = [], []
    for path in sorted(glob.glob(os.path.join(proj_dir, "**", "*.md"),
                                 recursive=True)):
        base = os.path.basename(path)
        if base in SKIP_NAMES or base.startswith("ck_"):
            continue
        if (os.sep + "archive" + os.sep) in path:
            continue
        try:
            with open(path, "r", encoding="utf-8", newline="") as f:
                text = f.read()
        except OSError:
            continue
        fm = find_frontmatter(text)
        if not fm:
            continue
        fields = parse_fields(text, fm)
        typ = fields.get("type")
        if typ is None:
            continue

        created = parse_date(fields.get("created", ""), today)
        last_acc = parse_date(fields.get("last_accessed", ""), created)
        try:
            access_count = int(fields.get("access_count", "0"))
        except ValueError:
            access_count = 0
        pinned = fields.get("pinned", "false").strip().lower() == "true"
        status = fields.get("status", "active").strip().lower()

        touched = page_touched(transcript, path, alias_dirs, proj_dir)
        if touched:
            last_acc, access_count = today, access_count + 1
            bumps.append((path, {
                "last_accessed": today.isoformat(),
                "access_count": str(access_count),
            }))

        if status != "active":
            continue

        # untouched *since tracking began* (cold-start guard) — see resolve_epoch
        effective_seen = max(last_acc, epoch)
        t_days = (today - effective_seen).days
        age = (today - created).days
        r = retention(t_days, access_count)
        eligible = (not pinned and typ not in DECAY_EXEMPT_TYPES
                    and t_days > ARCHIVE_T_DAYS and r < ARCHIVE_R_MAX
                    and age > ARCHIVE_AGE_DAYS)
        if eligible:
            candidates.append({
                "path": path, "slug": base[:-3], "r": r,
                "t_days": t_days, "age": age, "type": typ, "fm": fm,
                "text": text,
            })
    return bumps, candidates


def apply_bumps(bumps, dry_run):
    for path, updates in bumps:
        if dry_run:
            continue
        with open(path, "r", encoding="utf-8", newline="") as f:
            text = f.read()
        fm = find_frontmatter(text)
        if not fm:
            continue
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(update_fields(text, fm, updates))


def archive_page(cand, today, dry_run):
    """Move a page into archive/, stamp status: archived. Returns dest path."""
    src = cand["path"]
    proj_dir = _project_dir_of(src)
    dest_dir = os.path.join(proj_dir, "archive")
    dest = os.path.join(dest_dir, os.path.basename(src))
    if dry_run:
        return dest
    os.makedirs(dest_dir, exist_ok=True)
    text = cand["text"]
    fm = find_frontmatter(text)
    text = update_fields(text, fm, {"status": "archived",
                                    "archived_on": today.isoformat()})
    with open(dest, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    os.remove(src)  # archive/ + git history are the backstop; never lost
    return dest


def _project_dir_of(path):
    """The `<store>/projects/<name>` dir that contains `path`."""
    d = os.path.dirname(path)
    while d and os.path.basename(os.path.dirname(d)) != "projects":
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return d


def git_clean(store):
    try:
        out = subprocess.run(["git", "-C", store, "status", "--porcelain"],
                             capture_output=True, text=True, timeout=30)
        return out.returncode == 0 and out.stdout.strip() == ""
    except Exception:
        return False


def git_commit_push(store, n_archived, n_bumped, do_push):
    """Commit + push via the shared `okfmem sync` path (pull-rebase + lock).
    Stages the whole tree so decay_state.json (store root) rides along too —
    the old `add -A projects` left it uncommitted."""
    msg = (f"chore(okfmem): consolidate — archived {n_archived}, "
           f"reinforced {n_bumped}")
    res = sync_store(store, msg, do_push=do_push)
    print(f"  {res['reason']}")
    if res["pushed"]:
        print("  pushed.")
    elif res["push_error"]:
        print(f"  push failed: {res['push_error']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, write nothing")
    ap.add_argument("--store", default=os.environ.get("OKFMEM_STORE",
                    os.path.expanduser("~/okfmem-store")))
    ap.add_argument("--transcript", help="session transcript .jsonl path")
    ap.add_argument("--stdin-hook", action="store_true",
                    help="read Stop-hook JSON from stdin for transcript_path")
    ap.add_argument("--today", help="override today (YYYY-MM-DD) for testing")
    ap.add_argument("--cap", type=int, default=ARCHIVE_CAP,
                    help=f"max archives per run (default {ARCHIVE_CAP})")
    ap.add_argument("--no-commit", action="store_true",
                    help="apply file changes but do not git commit/push")
    ap.add_argument("--no-push", action="store_true",
                    help="commit but do not push")
    ap.add_argument("--force", action="store_true",
                    help="run apply even if the store working tree is dirty")
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    proj_root = os.path.join(store, "projects")
    if not os.path.isdir(proj_root):
        print(f"error: no projects/ under {store}", file=sys.stderr)
        sys.exit(2)

    today = (parse_date(args.today, None) if args.today
             else datetime.now(timezone.utc).date())
    if today is None:
        print(f"error: bad --today {args.today!r}", file=sys.stderr)
        sys.exit(2)

    if not args.dry_run and not args.no_commit and not args.force:
        if not git_clean(store):
            # Consolidation is idempotent background maintenance; a dirty tree
            # just means uncommitted memory writes (STATE.md, pages,
            # decay_state.json) or an in-flight `okfmem sync` this session.
            # As a Stop hook, degrade gracefully — skip this run and exit 0
            # (it runs again next session on a clean tree). Only a manual
            # invocation gets the actionable error.
            if args.stdin_hook:
                print("okfmem consolidate: store working tree dirty — "
                      "skipping this run (uncommitted memory writes or an "
                      "in-flight `okfmem sync`); will retry next session.")
                sys.exit(0)
            print("error: store working tree is dirty — commit/stash first, "
                  "or pass --force / --no-commit.", file=sys.stderr)
            sys.exit(3)

    epoch = resolve_epoch(store, today, args.dry_run)
    transcript = load_transcript(args)
    alias_index = build_alias_index(store)

    projects = sorted(d for d in os.listdir(proj_root)
                      if os.path.isdir(os.path.join(proj_root, d)))
    all_bumps, all_cands = [], []
    for proj in projects:
        proj_dir = os.path.join(proj_root, proj)
        aliases = alias_index.get(os.path.realpath(proj_dir), [])
        bumps, cands = scan_project(proj_dir, transcript, aliases, today, epoch)
        all_bumps.extend(bumps)
        all_cands.extend(cands)

    # apply global archive cap: lowest retention first
    all_cands.sort(key=lambda c: (c["r"], -c["t_days"]))
    to_archive = all_cands[:args.cap]
    skipped = all_cands[args.cap:]

    print(f"store: {store}")
    print(f"today: {today.isoformat()}   epoch: {epoch.isoformat()}   "
          f"transcript: {'yes' if transcript else 'none (age-only decay)'}")
    print(f"access reinforced: {len(all_bumps)} page(s)")
    print(f"archive candidates: {len(all_cands)} "
          f"(archiving {len(to_archive)}, cap {args.cap})")
    if skipped:
        print(f"  ! {len(skipped)} over cap deferred to next run")
    for c in to_archive:
        print(f"  archive  {os.path.relpath(c['path'], store)}  "
              f"R={c['r']:.3f} t={c['t_days']}d age={c['age']}d type={c['type']}")

    # apply
    apply_bumps(all_bumps, args.dry_run)

    per_proj_slugs = {}
    for c in to_archive:
        pdir = _project_dir_of(c["path"])
        archive_page(c, today, args.dry_run)
        per_proj_slugs.setdefault(pdir, []).append(c["slug"])

    dropped_total = 0
    for pdir, slugs in per_proj_slugs.items():
        dropped_total += drop_memory_lines(
            os.path.join(pdir, "MEMORY.md"), slugs, args.dry_run)
    if to_archive:
        print(f"MEMORY.md lines dropped: {dropped_total}")

    print(f"mode: {'DRY-RUN' if args.dry_run else 'APPLY'}")

    if args.dry_run:
        return
    if args.no_commit:
        print("  (--no-commit: not committing)")
        return
    if not to_archive and not all_bumps:
        print("  nothing changed; no commit.")
        return
    git_commit_push(store, len(to_archive), len(all_bumps),
                    do_push=not args.no_push)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""okfmem P2 — init wrapper (one-time, idempotent, cross-platform).

Anchors the OKF memory store into every AI-coding harness on this machine and
records how a working directory maps to a memory project. Re-runnable to repair.

Does four things:

  1. Detect harnesses — Claude Code (`~/.claude/`), Antigravity (`~/.gemini/`
     and/or `agy` on PATH).
  2. Write a managed MEMORY-POINTER block (delimited by HTML-comment markers)
     into each detected harness's global slot, editing in place between the
     markers — never duplicating, never touching surrounding content:
       Claude Code  -> ~/.claude/CLAUDE.md
       Antigravity  -> ~/.gemini/config/AGENTS.md   (global user-rule)
  3. Build/refresh `<store>/registry.json` — absolute git-root -> project name.
     Source of truth is the set of `~/.claude/projects/<encoded>/memory`
     symlinks: the symlink target's basename is the project; the encoded dir
     name decodes (filesystem-probed) to the real git root. Default rule is
     `basename(git-root)`; anything that deviates is recorded as an override.
  4. Stale-reference cleanup — scan registered project roots' CLAUDE.md /
     AGENTS.md / CLAUDE.local.md for retired-system references (memgraph, ck,
     projector, ...). DETECTION + report only by default; actual rewriting is
     gated behind --apply-cleanup (deferred — hand-review first).

`--status` prints what is wired and any drift. Nothing here ever deletes.

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys

# ---------------------------------------------------------------------------
# Output formatting — TTY-gated color + status glyphs, ASCII-safe when piped
# ---------------------------------------------------------------------------
_ANSI = {"reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
         "green": "\033[32m", "yellow": "\033[33m", "red": "\033[31m"}


def _use_color():
    return (sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb")


def _c(s, style):
    return f"{_ANSI[style]}{s}{_ANSI['reset']}" if _use_color() else s


# kind -> (unicode glyph, ascii fallback, color)
_GLYPH = {"ok": ("✓", "ok", "green"),
          "chg": ("~", "~", "yellow"),
          "warn": ("!", "!", "red")}


def glyph(kind):
    uni, ascii_, color = _GLYPH[kind]
    ch = uni if _use_color() else ascii_
    return _c(ch, color)


def _short(path):
    """Collapse the home prefix to ~ for compact, portable-looking paths."""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


# ---------------------------------------------------------------------------
# Managed pointer block
# ---------------------------------------------------------------------------
MARKER_OPEN = "<!-- MEMORY-POINTER v1 (managed by memory-init — do not edit between markers) -->"
MARKER_CLOSE = "<!-- /MEMORY-POINTER -->"

POINTER_BODY = """## Memory
Durable project memory: `~/okfmem-store/projects/<PROJECT>/`
`<PROJECT>` = basename of the git root, unless overridden in `~/okfmem-store/registry.json`.
- **At session start, eagerly read `STATE.md`** (bounded active-state snapshot) and
  `MEMORY.md` (the topic index) from that dir. Harnesses without native memory
  auto-load (e.g. Antigravity) MUST read both up front — do not wait to be asked.
- To recall a topic: grep the memory dir for keywords, then open the matching `<slug>.md`.
  Do NOT eager-read every page.
- Pages are OKF markdown. Frontmatter (`pinned`/`importance`/`status`/`access_count`) is
  maintenance metadata — ignore it when reasoning."""

POINTER_BLOCK = f"{MARKER_OPEN}\n{POINTER_BODY}\n{MARKER_CLOSE}"

# Retired-system references the cleanup pass looks for. The ONLY legitimate
# surviving mention is the retirement-notice sentence in ~/.claude/CLAUDE.md.
STALE_PATTERNS = [
    r"\bclaude-memory\b",                       # renamed to okfmem-store
    r"\bmemgraph\b",
    r"\bread_graph\b", r"\bsearch_nodes\b",
    r"\bcreate_entities\b", r"\badd_observations\b",
    r"\bprojector\b", r"\bpush-primer\b",
    r"source:\s*graph",
    r"\bcontext-keeper\b", r"/ck:save", r"\bcontext\.json\b",
    r"\bck/contexts\b",
]
STALE_RE = re.compile("|".join(STALE_PATTERNS), re.IGNORECASE)

# The old data repo was renamed claude-memory -> okfmem-store. A line that only
# references that path is a mechanical, unambiguous rewrite (the one thing
# --apply-cleanup edits automatically).
CLAUDE_MEMORY_RE = re.compile(r"claude-memory")

# A line that mentions a retired system only to say it is retired is a
# *notice* (tells agents to ignore leftovers) — same class as the preserved
# sentence in ~/.claude/CLAUDE.md. Never rewrite these.
NOTICE_RE = re.compile(r"retire|do not use|no longer|removed|deprecat",
                       re.IGNORECASE)


def classify_line(text):
    """'path' = safe claude-memory→okfmem-store swap; 'notice' = leave as-is;
    'review' = flagged but needs a human (never auto-edited)."""
    if NOTICE_RE.search(text) and not (
            CLAUDE_MEMORY_RE.search(text) and "okfmem-store" not in text):
        return "notice"
    if CLAUDE_MEMORY_RE.search(text):
        return "path"
    return "review"


# ---------------------------------------------------------------------------
# Harness detection
# ---------------------------------------------------------------------------
def detect_harnesses():
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    gemini_dir = os.path.join(home, ".gemini")
    return {
        "claude_code": os.path.join(claude_dir, "CLAUDE.md")
        if os.path.isdir(claude_dir) else None,
        "antigravity": os.path.join(gemini_dir, "config", "AGENTS.md")
        if (os.path.isdir(gemini_dir) or shutil.which("agy")) else None,
    }


# ---------------------------------------------------------------------------
# Encoded-dir -> real path (filesystem-probed; dir names may contain '-')
# ---------------------------------------------------------------------------
def _windows_drive_root(tokens):
    """If `tokens` starts with a drive letter and that drive actually exists
    on this machine, return (root, consumed) so the probe can start from
    `C:\\` instead of `/`. The first token is either a bare letter `C` (from
    an encoded `C-Users-name-project`) or a drive spec `C:` — the latter is
    what Claude Code's encoding produces on Windows, where `str(Path)` uses
    `\\` separators, so `C:\\Users` -> `C:-Users` -> first token `C:`. Returns
    None on POSIX or when the first token isn't a real drive letter — the
    caller then falls back to the original `/`-rooted behavior unchanged."""
    if os.name != "nt" or not tokens:
        return None
    first = tokens[0]
    letter = first[:-1] if first.endswith(":") else first
    if len(letter) != 1 or not letter.isalpha():
        return None
    drive = f"{letter}:\\"
    if not os.path.isdir(drive):
        return None
    return drive, 1


def decode_root(encoded):
    """`-Users-you-worktree-autosync` -> `/Users/you/worktree-autosync`.

    Claude Code encodes a cwd by replacing '/' with '-', which is lossy because
    directory names can themselves contain '-'. Reverse it by probing the real
    filesystem: walk the '-'-split tokens left to right, greedily preferring the
    longest existing directory at each step.

    On Windows, an encoded path like `C-Users-name-project` (or
    `-C-Users-name-project`) starts with a drive letter instead of a leading
    slash; the probe root is switched to `C:\\` for that case (POSIX behavior,
    including existing encodings like `-Users-you-worktree-autosync`, is
    unchanged).
    """
    tokens = encoded.lstrip("-").split("-")
    path = "/"
    i = 0
    drive = _windows_drive_root(tokens)
    if drive is not None:
        path, i = drive
    while i < len(tokens):
        # extend the current segment with as many '-'-joined tokens as still
        # name a real directory; fall back to the single token.
        best = None
        cand = tokens[i]
        j = i
        if os.path.isdir(os.path.join(path, cand)):
            best, best_j = cand, j
        while j + 1 < len(tokens):
            j += 1
            cand = cand + "-" + tokens[j]
            if os.path.isdir(os.path.join(path, cand)):
                best, best_j = cand, j
        if best is None:
            # nothing on disk matches — reconstruct verbatim and stop probing
            return os.path.normpath(os.path.join(path, "-".join(tokens[i:])))
        path = os.path.join(path, best)
        i = best_j + 1
    return os.path.normpath(path)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def build_registry(store, claude_projects):
    """Return (registry_dict, drift_notes) from the memory symlinks."""
    mapping = {}          # abs git-root -> project
    overrides = {}        # subset where project != basename(root)
    drift = []
    if not os.path.isdir(claude_projects):
        drift.append(f"no Claude Code projects dir at {claude_projects}")
        return _registry_shell(mapping, overrides), drift

    store_projects = os.path.realpath(os.path.join(store, "projects"))
    for entry in sorted(os.listdir(claude_projects)):
        link = os.path.join(claude_projects, entry, "memory")
        if not os.path.islink(link):
            continue
        target = os.path.realpath(link)
        if not target.startswith(store_projects + os.sep) and target != store_projects:
            continue  # symlink points outside this store — not ours
        project = os.path.basename(target)
        root = decode_root(entry)
        mapping[root] = project
        if os.path.basename(root) != project:
            overrides[root] = project
    return _registry_shell(mapping, overrides), drift


def _registry_shell(mapping, overrides):
    return {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": dict(sorted(mapping.items())),
        "overrides": dict(sorted(overrides.items())),
    }


def write_registry(store, reg, dry_run):
    """Write registry.json only when its content actually changes. Returns
    (path, changed) so the caller can report accurately and avoid needless
    writes on a re-run of an already-wired setup."""
    path = os.path.join(store, "registry.json")
    text = json.dumps(reg, indent=2) + "\n"
    old = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            old = f.read()
    changed = old != text
    if changed and not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path, changed


# ---------------------------------------------------------------------------
# Pointer upsert
# ---------------------------------------------------------------------------
def upsert_pointer(path, dry_run):
    """Insert or replace the managed pointer block in `path`. Returns action."""
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()

    block_re = re.compile(
        re.escape(MARKER_OPEN) + r".*?" + re.escape(MARKER_CLOSE),
        re.DOTALL,
    )
    if block_re.search(existing):
        new_text = block_re.sub(POINTER_BLOCK, existing)
        action = "unchanged" if new_text == existing else "updated"
    else:
        sep = "" if existing == "" or existing.endswith("\n\n") else (
            "\n" if existing.endswith("\n") else "\n\n")
        new_text = existing + sep + POINTER_BLOCK + "\n"
        action = "inserted"

    if action != "unchanged" and not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    return action


# ---------------------------------------------------------------------------
# Stale-reference scan (detection only unless --apply-cleanup)
# ---------------------------------------------------------------------------
CLEANUP_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
CLEANUP_SKIP_DIRS = {".git", "node_modules", ".venv", "venv",
                     "__pycache__", "dist", "build", ".next"}


def _scan_one(real, findings, seen, fpath):
    if real in seen:
        return
    seen.add(real)
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    hits = [(i + 1, ln.rstrip("\n"), classify_line(ln))
            for i, ln in enumerate(lines) if STALE_RE.search(ln)]
    if hits:
        findings.append((fpath, hits))


def scan_stale(reg, self_claude_md, harness_globals=()):
    """Report retired-system references across registered project roots plus the
    harness global files. Each hit is (lineno, text, category) where category is
    'path' (safe claude-memory->okfmem-store swap), 'notice' (retirement note,
    left as-is), or 'review' (needs a human).

    Dedupes by real path (many repos have AGENTS.md -> CLAUDE.md symlinks). The
    retirement-notice sentence in ~/.claude/CLAUDE.md is the one legitimate
    mention and is excluded entirely.
    """
    seen = set()
    if self_claude_md:
        seen.add(os.path.realpath(self_claude_md))  # leave the retirement notice
    findings = []
    roots = sorted(set(reg["map"].keys()))
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in CLEANUP_SKIP_DIRS]
            for name in filenames:
                if name not in CLEANUP_FILES:
                    continue
                fpath = os.path.join(dirpath, name)
                _scan_one(os.path.realpath(fpath), findings, seen, fpath)
    for g in harness_globals:
        if g and os.path.isfile(g):
            _scan_one(os.path.realpath(g), findings, seen, g)
    return findings


def apply_path_rewrites(findings, dry_run):
    """Swap claude-memory->okfmem-store on 'path' lines only. Returns
    (files_changed, lines_changed). Never edits 'notice'/'review' lines."""
    files_changed = lines_changed = 0
    for fpath, hits in findings:
        path_linenos = {ln for ln, _, cat in hits if cat == "path"}
        if not path_linenos:
            continue
        real = os.path.realpath(fpath)
        with open(real, "r", encoding="utf-8", newline="") as f:
            lines = f.readlines()
        changed = False
        for ln in path_linenos:
            if 1 <= ln <= len(lines) and "claude-memory" in lines[ln - 1]:
                lines[ln - 1] = lines[ln - 1].replace("claude-memory",
                                                      "okfmem-store")
                lines_changed += 1
                changed = True
        if changed:
            files_changed += 1
            if not dry_run:
                with open(real, "w", encoding="utf-8", newline="") as f:
                    f.writelines(lines)
    return files_changed, lines_changed


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def skill_dirs():
    """Harness skill directories that exist on this machine, keyed by name.

    The engine self-installs its skills here so a fresh `git clone ~/okfmem &&
    okfmem init` surfaces /okfmem, /okfmem-save, /okfmem-curate in every harness
    present — no dependency on ~/tools/sync-skills.sh (which still fans the
    back-compat alias names primer/memory-curate from ~/tools/skills)."""
    home = os.path.expanduser("~")
    out = {}
    if os.path.isdir(os.path.join(home, ".claude")):
        out["claude_code"] = os.path.join(home, ".claude", "skills")
    if os.path.isdir(os.path.join(home, ".codex")):
        out["codex"] = os.path.join(home, ".codex", "skills")
    if os.path.isdir(os.path.join(home, ".gemini")) or shutil.which("agy"):
        out["antigravity"] = os.path.join(home, ".gemini", "config", "skills")
    return out


def _is_junction(path):
    """True if `path` is a Windows directory junction / mount-point reparse
    point. Junctions aren't `os.path.islink`-true on every Python version, so
    callers that need to tell "a link we made" from "a real directory" must
    check this too.

    Use `os.lstat` (NOT `os.stat`): a junction is a reparse point, and
    `os.stat` follows it to the real directory — whose attributes never carry
    the reparse flag, so it would always report False. `os.lstat` inspects the
    link itself. Prefer the specific `st_reparse_tag == IO_REPARSE_TAG_MOUNT_
    POINT` when the running Python exposes it, else fall back to the generic
    FILE_ATTRIBUTE_REPARSE_POINT bit. All of these attributes are Windows-only
    and appear only on recent Python, so everything is getattr-guarded and any
    failure (or POSIX) returns False."""
    if os.name != "nt":
        return False
    try:
        st = os.lstat(path)
    except OSError:
        return False
    tag = getattr(st, "st_reparse_tag", None)
    mount_point = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    if tag is not None and mount_point is not None:
        return tag == mount_point
    attrs = getattr(st, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attrs & reparse)


# Marker file dropped inside a tier-3 copytree fallback so a later run can
# recognize the directory as one WE created (and re-copy it when the engine
# updates) rather than mistaking it for a user's own real directory. Its body
# records the engine target the copy was made from.
MANAGED_COPY_MARKER = ".okfmem-managed-copy"


def _make_link(target, link):
    """Point `link` at `target` using the best mechanism this platform/
    privilege level allows, three tiers deep:

      1. `os.symlink` — works everywhere Claude Code already assumed (POSIX
         always; Windows when Developer Mode or admin grants the privilege).
      2. Directory junction via `mklink /J` — Windows-only, needs no
         elevation, stays live (repoints transparently like a symlink), but
         only works for directories (fine here — skill dirs are directories).
      3. One-time `shutil.copytree` — last resort when neither of the above
         is available; the copy goes stale on engine updates, so it is stamped
         with a MANAGED_COPY_MARKER and callers warn the user to re-run
         `okfmem init` after upgrading.

    Returns the tier that succeeded: "symlink", "junction", or "copy".
    Raises OSError if all three fail (should only happen on a broken/
    read-only destination).
    """
    try:
        # Skill targets are directories; target_is_directory=True makes the
        # Windows symlink a DIRECTORY symlink (ignored on POSIX). Without it a
        # bare os.symlink creates a file symlink to a directory on Windows.
        os.symlink(target, link, target_is_directory=True)
        return "symlink"
    except OSError:
        if os.name != "nt":
            raise
    # Tier 2: junction (Windows only, reached only after symlink failed).
    # NOTE: passing link/target as separate argv items keeps Python from
    # word-splitting spaced paths, but `cmd /c mklink` itself does not quote
    # them internally — a target containing spaces can still trip cmd's own
    # tokenizer. Skill dirs under ~/.claude live in space-free paths today.
    try:
        subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                       capture_output=True, text=True, check=True)
        return "junction"
    except (OSError, subprocess.CalledProcessError):
        pass
    # Tier 3: last-resort copy — always succeeds or raises loudly. Stamp a
    # marker so a later run treats this as a managed copy (re-copied on engine
    # update) instead of a user's real directory.
    shutil.copytree(target, link)
    try:
        with open(os.path.join(link, MANAGED_COPY_MARKER), "w",
                  encoding="utf-8") as f:
            f.write(target + "\n")
    except OSError:
        pass  # marker is best-effort; a copy without it just degrades to
        # "skip (real file)" on the next run — no crash
    return "copy"


def _managed_copy_target(link):
    """If `link` is a plain directory this engine created via the tier-3
    copytree fallback, return the engine target path recorded in its marker
    file (possibly ""); otherwise None. Presence of the marker — not content
    equality — is what identifies a managed copy, so a copy that has since
    diverged from an updated engine is still recognized as ours (and re-copied)
    instead of being mistaken for a user's real directory."""
    if os.path.islink(link) or not os.path.isdir(link):
        return None
    try:
        with open(os.path.join(link, MANAGED_COPY_MARKER), "r",
                  encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


def _link_matches_target(link, target):
    """True if `link` already points at (or, for a copy, was made from)
    `target`. Handles all three `_make_link` tiers so `link_skills` stays
    idempotent: a real symlink is compared via `readlink`; a junction or a
    plain directory (possibly a stale one-time copy) is compared via
    `realpath`, which resolves through junctions and matches copies whose
    SKILL.md content is identical to the source."""
    if os.path.islink(link):
        return os.readlink(link) == target
    if os.path.isdir(link):
        if _is_junction(link):
            return os.path.realpath(link) == os.path.realpath(target)
        # Plain directory: a managed tier-3 copy (see _managed_copy_target).
        # Treat "same SKILL.md bytes" as "matches" — an engine update changes
        # those bytes, which correctly reports a mismatch so the caller
        # re-copies and warns. (Only reached for managed copies; a user's real
        # directory has no marker and never gets here.)
        src_skill = os.path.join(target, "SKILL.md")
        dst_skill = os.path.join(link, "SKILL.md")
        try:
            with open(src_skill, "rb") as a, open(dst_skill, "rb") as b:
                return a.read() == b.read()
        except OSError:
            return False
    return False


def link_skills(dry_run):
    """Link every engine skill (~/okfmem/skills/<name>/SKILL.md) into each
    detected harness skill dir, via `_make_link`'s three-tier fallback
    (symlink -> junction -> copy) so this works on Windows without admin
    privileges. Idempotent; never deletes a real (non-symlink, non-junction,
    non-managed-copy) entry. Returns a list of (harness, name, action) tuples
    for reporting — action is "ok"/"link"/"repoint" plus a tier suffix for
    non-symlink mechanisms (e.g. "link (junction)", "link (copy — will go
    stale; re-run okfmem init after engine updates)")."""
    engine = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    actions = []
    if not os.path.isdir(engine):
        return actions
    names = sorted(n for n in os.listdir(engine)
                   if os.path.isfile(os.path.join(engine, n, "SKILL.md")))
    for harness, dest in skill_dirs().items():
        if not dry_run:
            os.makedirs(dest, exist_ok=True)
        for name in names:
            target = os.path.join(engine, name)
            link = os.path.join(dest, name)
            # "Managed" = something we created and may repoint: a symlink, a
            # junction, or a tier-3 copy identified by its marker (regardless
            # of whether its contents still match — that's how a stale copy
            # gets re-copied instead of misfiled as a user's real file).
            is_managed = (os.path.islink(link) or _is_junction(link)
                          or _managed_copy_target(link) is not None)
            if is_managed:
                if _link_matches_target(link, target):
                    actions.append((harness, name, "ok"))
                    continue
                action = "repoint"
                if not dry_run:
                    if _is_junction(link):
                        # A junction is a directory reparse point: unlink it
                        # with rmdir (removes the link, not the target). rmtree
                        # on a junction can traverse INTO the target on older
                        # Python — never do that here.
                        os.rmdir(link)
                    elif os.path.isdir(link) and not os.path.islink(link):
                        shutil.rmtree(link)  # tier-3 real copy dir
                    else:
                        os.remove(link)
                    tier = _make_link(target, link)
                    if tier == "copy":
                        action = ("repoint (copy — will go stale; re-run "
                                  "okfmem init after engine updates)")
                    elif tier != "symlink":
                        action = f"repoint ({tier})"
            elif os.path.exists(link):
                actions.append((harness, name, "skip (real file)"))
                continue
            else:
                action = "link"
                if not dry_run:
                    tier = _make_link(target, link)
                    if tier == "junction":
                        action = "link (junction)"
                    elif tier == "copy":
                        action = ("link (copy — will go stale; re-run "
                                  "okfmem init after engine updates)")
            actions.append((harness, name, action))
    return actions


def cmd_run(store, dry_run, apply_cleanup, verbose=False):
    """Wire the store into every harness and report the result.

    Output is quiet-by-default: each of the five steps prints ONE status line
    (glyph + summary); a step only expands to per-item detail when it has
    something that would change, or under --verbose. A final verdict line says
    whether anything needs doing. `changes` counts would-change/changed items
    across all steps and drives that verdict.
    """
    home = os.path.expanduser("~")
    claude_projects = os.path.join(home, ".claude", "projects")
    harnesses = detect_harnesses()
    mode = "dry-run" if dry_run else "apply"
    changes = 0

    print(_c("okfmem init", "bold") + _c(f"  ·  {mode}", "dim") + "\n")

    # --- 1. harnesses ------------------------------------------------------
    missing = [n for n, p in harnesses.items() if not p]
    ndet = len(harnesses) - len(missing)
    summary = f"{ndet} detected" + (f", {len(missing)} not found" if missing else "")
    print(f"{glyph('warn' if missing else 'ok')} harnesses   {summary}")
    if verbose or missing:
        for name, path in harnesses.items():
            k = "ok" if path else "warn"
            print(f"    {glyph(k)} {name:12} {_short(path) if path else 'not found'}")

    # --- 2. registry -------------------------------------------------------
    reg, drift = build_registry(store, claude_projects)
    reg_path, reg_changed = write_registry(store, reg, dry_run)
    changes += 1 if reg_changed else 0
    detail = f"{len(reg['map'])} projects" + (
        f", {len(reg['overrides'])} renamed" if reg["overrides"] else "")
    if reg_changed:
        print(f"{glyph('chg')} registry    "
              f"{'would update' if dry_run else 'updated'} ({detail})")
    else:
        print(f"{glyph('ok')} registry    up to date ({detail})")
    if verbose and reg["overrides"]:
        for root, proj in reg["overrides"].items():
            print(f"    {_short(root)} → {proj}")
    for d in drift:
        print(f"    {glyph('warn')} {d}")

    # --- 3. pointers -------------------------------------------------------
    pacts = [(n, upsert_pointer(p, dry_run), p)
             for n, p in harnesses.items() if p]
    pchg = [a for a in pacts if a[1] != "unchanged"]
    changes += len(pchg)
    if pchg:
        print(f"{glyph('chg')} pointers    "
              f"{len(pchg)} to write ({len(pacts)} harness globals)")
    else:
        print(f"{glyph('ok')} pointers    up to date ({len(pacts)} harness globals)")
    if verbose or pchg:
        for name, action, path in pacts:
            k = "ok" if action == "unchanged" else "chg"
            print(f"    {glyph(k)} {name:12} {action:10} {_short(path)}")

    # --- 4. stale references ----------------------------------------------
    findings = scan_stale(reg, harnesses["claude_code"] or "",
                          harness_globals=[harnesses["antigravity"]])
    total = sum(len(h) for _, h in findings)
    cats = {"path": 0, "notice": 0, "review": 0}
    for _, hits in findings:
        for _, _, cat in hits:
            cats[cat] += 1
    actionable = cats["path"] + cats["review"]  # notices are expected, not work
    if total == 0:
        print(f"{glyph('ok')} stale refs  none")
    else:
        parts = []
        if cats["path"]:
            parts.append(f"{cats['path']} to rewrite")
        if cats["review"]:
            parts.append(f"{cats['review']} need review")
        if cats["notice"]:
            parts.append(f"{cats['notice']} notice"
                         f"{'s' if cats['notice'] != 1 else ''} (kept)")
        kind = "ok" if not actionable else ("warn" if cats["review"] else "chg")
        print(f"{glyph(kind)} stale refs  {total} across "
              f"{len(findings)} file(s): {', '.join(parts)}")
        # detail: everything under --verbose, else only actionable hits
        for fpath, hits in findings:
            show = hits if verbose else [h for h in hits if h[2] != "notice"]
            if not show:
                continue
            print(f"    {_short(fpath)}")
            for lineno, text, cat in show[:6]:
                gk = {"path": "chg", "review": "warn", "notice": "ok"}[cat]
                print(f"      {glyph(gk)} L{lineno} {cat}: {text.strip()[:78]}")
            if len(show) > 6:
                print(f"      … +{len(show) - 6} more")
        if apply_cleanup:
            fc, lc = apply_path_rewrites(findings, dry_run)
            changes += lc
            verb = "would rewrite" if dry_run else "rewrote"
            print(f"    → {verb} {lc} path line(s) in {fc} file(s); "
                  f"notice/review lines untouched")
            if cats["review"]:
                print(f"    {glyph('warn')} {cats['review']} review line(s) "
                      f"need a human — not edited")
        elif cats["path"]:
            print(_c(f"    → run with --apply-cleanup to rewrite "
                     f"{cats['path']} path line(s)", "dim"))

    # --- 5. skills ---------------------------------------------------------
    sk = link_skills(dry_run)
    if not sk:
        print(f"{glyph('warn')} skills      none to link (no harness skill dirs)")
    else:
        chg = [a for a in sk if a[2] != "ok"]
        changes += len(chg)
        n_ok = len(sk) - len(chg)
        if chg:
            print(f"{glyph('chg')} skills      "
                  f"{len(chg)} to link ({n_ok} already linked)")
        else:
            print(f"{glyph('ok')} skills      all linked ({n_ok})")
        if verbose or chg:
            for harness, name, action in chg:
                print(f"    {glyph('chg')} {harness:12} {action:16} {name}")

    # --- verdict -----------------------------------------------------------
    print()
    tail = _c(f"({_short(store)} · {mode})", "dim")
    if changes == 0:
        print(f"{glyph('ok')} {_c('fully wired', 'bold')} — nothing to do  {tail}")
    else:
        verb = "would change" if dry_run else "changed"
        print(f"{glyph('chg')} {_c(f'{changes} item(s) {verb}', 'bold')}  {tail}")
        if dry_run:
            print(_c("    re-run without --dry-run to apply", "dim"))


def cmd_status(store):
    home = os.path.expanduser("~")
    harnesses = detect_harnesses()
    reg_path = os.path.join(store, "registry.json")

    print("== okfmem status ==")
    for name, path in harnesses.items():
        wired = "—"
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                wired = "pointer PRESENT" if MARKER_OPEN in f.read() else "pointer MISSING"
        print(f"  {name:12} {path or 'not found'}   {wired}")

    if os.path.exists(reg_path):
        with open(reg_path) as f:
            reg = json.load(f)
        print(f"\n  registry: {len(reg.get('map', {}))} roots, "
              f"{len(reg.get('overrides', {}))} overrides  ({reg_path})")
    else:
        print(f"\n  registry: MISSING ({reg_path})")

    reg = build_registry(store, os.path.join(home, ".claude", "projects"))[0]
    findings = scan_stale(reg, harnesses["claude_code"] or "",
                          harness_globals=[harnesses["antigravity"]])
    total = sum(len(h) for _, h in findings)
    cats = {"path": 0, "notice": 0, "review": 0}
    for _, hits in findings:
        for _, _, cat in hits:
            cats[cat] += 1
    print(f"  stale refs: {total} line(s) across {len(findings)} file(s) "
          f"({cats['path']} path-swap, {cats['notice']} notice, "
          f"{cats['review']} review)")

    sk = link_skills(dry_run=True)
    if sk:
        pending = [a for a in sk if a[2] != "ok"]
        by_h = {}
        for harness, _, _ in sk:
            by_h[harness] = by_h.get(harness, 0) + 1
        wired = ", ".join(f"{h}:{c}" for h, c in sorted(by_h.items()))
        note = f"  skills: {wired}"
        if pending:
            note += f"  ({len(pending)} not linked — run `okfmem init`)"
        print(note)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true",
                    help="print wiring + drift, change nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, write nothing")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="expand every step to per-item detail (default: quiet — "
                         "idle steps collapse to one line)")
    ap.add_argument("--apply-cleanup", action="store_true",
                    help="rewrite claude-memory->okfmem-store on path lines "
                         "(retirement-notice + review lines left untouched)")
    ap.add_argument("--store", default=os.environ.get(
        "OKFMEM_STORE", os.path.expanduser("~/okfmem-store")))
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    if not os.path.isdir(os.path.join(store, "projects")):
        print(f"error: no projects/ under {store}", file=sys.stderr)
        sys.exit(2)

    if args.status:
        cmd_status(store)
    else:
        cmd_run(store, args.dry_run, args.apply_cleanup, args.verbose)


if __name__ == "__main__":
    main()

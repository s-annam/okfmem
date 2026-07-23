#!/usr/bin/env python3
"""okfmem P2 — init wrapper (one-time, idempotent, cross-platform).

Anchors the OKF memory store into every AI-coding harness on this machine and
records how a working directory maps to a memory project. Re-runnable to repair.

Does five things:

  1. Detect harnesses — Claude Code (`~/.claude/`), Antigravity (`~/.gemini/`
     and/or `agy` on PATH).
  2. Create/repair the CURRENT repo's per-project memory symlink --
     `~/.claude/projects/<encoded-root>/memory` -> `<store>/projects/<name>`
     -- when the store already has a project dir for it. Without this, a
     fresh machine leaves that dir an empty real directory forever: nothing
     for the harness to auto-load, and nothing for step 3 to discover.
  3. Build/refresh `<store>/registry.json` — absolute git-root -> project name.
     Source of truth is the set of `~/.claude/projects/<encoded>/memory`
     symlinks: the symlink target's basename is the project; the encoded dir
     name decodes (filesystem-probed) to the real git root. Default rule is
     `basename(git-root)`; anything that deviates is recorded as an override.
  4. Write a managed MEMORY-POINTER block (delimited by HTML-comment markers)
     into each detected harness's global slot, editing in place between the
     markers — never duplicating, never touching surrounding content:
       Claude Code  -> ~/.claude/CLAUDE.md
       Antigravity  -> ~/.gemini/config/AGENTS.md   (global user-rule)
  5. Stale-reference cleanup — scan registered project roots' CLAUDE.md /
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
import time

# ---------------------------------------------------------------------------
# Output formatting — TTY-gated color + status glyphs, ASCII-safe when piped
# ---------------------------------------------------------------------------
_ANSI = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
}


def _use_color():
    return (
        sys.stdout.isatty()
        and os.environ.get("NO_COLOR") is None
        and os.environ.get("TERM") != "dumb"
    )


def _c(s, style):
    return f"{_ANSI[style]}{s}{_ANSI['reset']}" if _use_color() else s


# kind -> (unicode glyph, ascii fallback, color)
_GLYPH = {
    "ok": ("✓", "ok", "green"),
    "chg": ("~", "~", "yellow"),
    "warn": ("!", "!", "red"),
}


def glyph(kind):
    uni, ascii_, color = _GLYPH[kind]
    ch = uni if _use_color() else ascii_
    return _c(ch, color)


def _short(path):
    """Collapse the home prefix to ~ for compact, portable-looking paths."""
    home = os.path.expanduser("~")
    return (
        "~" + path[len(home) :]
        if path == home or path.startswith(home + os.sep)
        else path
    )


# ---------------------------------------------------------------------------
# Managed pointer block
# ---------------------------------------------------------------------------
MARKER_OPEN = (
    "<!-- MEMORY-POINTER v1 (managed by memory-init — do not edit between markers) -->"
)
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

# ---------------------------------------------------------------------------
# Managed store .gitignore block (#27)
# ---------------------------------------------------------------------------
# The store is data the engine owns, so its git hygiene is the engine's job
# too -- a fresh store (new machine, `curl | bash`, CI) has no `.gitignore`
# until this runs. Marker-delimited like the pointer block above: idempotent,
# re-runnable, and never clobbers a user's hand-added rules living outside
# the markers.
GITIGNORE_MARKER_OPEN = "# BEGIN okfmem-managed v1 (do not edit between markers)"
GITIGNORE_MARKER_CLOSE = "# END okfmem-managed"

GITIGNORE_LINES = (".okfmem-sync.lock", "*.db", "__pycache__/", "*.pyc", ".DS_Store")

GITIGNORE_BODY = (
    "# okfmem per-machine runtime / rebuildable -- never synced\n"
    + "\n".join(GITIGNORE_LINES)
)

GITIGNORE_BLOCK = f"{GITIGNORE_MARKER_OPEN}\n{GITIGNORE_BODY}\n{GITIGNORE_MARKER_CLOSE}"

# Retired-system references the cleanup pass looks for. The ONLY legitimate
# surviving mention is the retirement-notice sentence in ~/.claude/CLAUDE.md.
STALE_PATTERNS = [
    r"\bclaude-memory\b",  # renamed to okfmem-store
    r"\bmemgraph\b",
    r"\bread_graph\b",
    r"\bsearch_nodes\b",
    r"\bcreate_entities\b",
    r"\badd_observations\b",
    r"\bprojector\b",
    r"\bpush-primer\b",
    r"source:\s*graph",
    r"\bcontext-keeper\b",
    r"/ck:save",
    r"\bcontext\.json\b",
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
NOTICE_RE = re.compile(r"retire|do not use|no longer|removed|deprecat", re.IGNORECASE)


def classify_line(text):
    """'path' = safe claude-memory→okfmem-store swap; 'notice' = leave as-is;
    'review' = flagged but needs a human (never auto-edited)."""
    if NOTICE_RE.search(text) and not (
        CLAUDE_MEMORY_RE.search(text) and "okfmem-store" not in text
    ):
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
        if os.path.isdir(claude_dir)
        else None,
        "antigravity": os.path.join(gemini_dir, "config", "AGENTS.md")
        if (os.path.isdir(gemini_dir) or shutil.which("agy"))
        else None,
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


def encode_root(root):
    """Inverse of `decode_root`: turn an absolute git root into the on-disk
    Claude Code project-dir encoding (`~/.claude/projects/<encoded>/`).

    Claude Code encodes a cwd by replacing path separators with '-'. On
    Windows, `str(WindowsPath)` uses '\\', and the drive colon is ALSO
    replaced (`C:\\Users\\name\\proj` -> `C--Users-name-proj`, a double dash
    between the drive letter and the next segment) -- `decode_root`'s
    `_windows_drive_root` already tolerates both the bare-letter and
    colon-suffixed first-token shapes, so this stays the single inverse for
    both. POSIX is untouched: only '/' is replaced."""
    root = os.path.normpath(root)
    if os.name == "nt":
        root = root.replace(":", "-")
        return root.replace("\\", "-")
    return root.replace("/", "-")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
def build_registry(store, claude_projects):
    """Return (registry_dict, drift_notes) from the memory symlinks."""
    mapping = {}  # abs git-root -> project
    overrides = {}  # subset where project != basename(root)
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


def _load_registry(path):
    """Read an existing registry.json, tolerating absence or corruption.
    Always returns a dict with at least 'map' and 'overrides' keys."""
    if not os.path.exists(path):
        return {"map": {}, "overrides": {}}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("registry.json is not a JSON object")
        data.setdefault("map", {})
        data.setdefault("overrides", {})
        return data
    except (json.JSONDecodeError, ValueError, OSError):
        # A malformed registry shouldn't wedge init -- treat as empty and let
        # the merge rebuild from local symlinks + whatever we can preserve.
        return {"map": {}, "overrides": {}}


def _is_foreign_root(root):
    """True if `root` is an absolute path in ANOTHER OS's native form -- i.e. it
    belongs to a different machine sharing this store.

    Decided by path SHAPE, never by os.path.isdir: on Windows a bare POSIX path
    like `/Users/you/proj` is drive-*relative* and isdir() happily resolves it
    to `C:\\Users\\you\\proj`, so a dir check would misjudge another machine's
    mac paths as local and drop them. A native Windows root instead carries a
    drive (`C:\\`) or UNC (`\\\\host`); a native POSIX root starts with `/` and
    has no backslashes."""
    drive, _ = os.path.splitdrive(root)
    if os.name == "nt":
        # Native here == a drive (C:\) or a UNC share (\\host\...). splitdrive
        # catches the drive form; check the UNC prefix explicitly since it isn't
        # always split out. Anything else (a bare /Users/... POSIX path) is a
        # different machine's.
        return not (drive or root[:2] in ("\\\\", "//"))
    # POSIX: native == starts with '/' and no Windows drive/backslashes.
    return bool(drive) or "\\" in root or not root.startswith("/")


def _merge_registry(existing, derived):
    """Merge the locally-derived registry onto an existing one WITHOUT clobbering
    entries that belong to other machines.

    The store is shared across machines via git, but registry keys are ABSOLUTE
    git-root paths -- inherently machine-specific. This machine is authoritative
    only for roots in THIS OS's native path form; foreign roots (e.g. a mac's
    `/Users/you/...` seen from Windows) are carried through untouched so running
    init on one machine no longer wipes another machine's mappings to empty.

    Within local scope this run stays authoritative: a native root that is no
    longer symlinked gets dropped, exactly as the old rebuild-from-scratch did."""

    def merge_section(key):
        out = {}
        # Preserve foreign entries -- another machine's native paths.
        for root, proj in existing.get(key, {}).items():
            if _is_foreign_root(root):
                out[root] = proj
        # Local entries: this run's derived set is the source of truth.
        for root, proj in derived.get(key, {}).items():
            out[root] = proj
        return dict(sorted(out.items()))

    return {
        "version": 1,
        "default_rule": "basename(git-root)",
        "map": merge_section("map"),
        "overrides": merge_section("overrides"),
    }


def write_registry(store, reg, dry_run):
    """Merge the locally-derived registry `reg` onto the existing registry.json
    (preserving other machines' entries) and write only when the MAPPING itself
    changes. Returns (path, changed, merged) so the caller reports the merged
    counts and avoids needless writes.

    The change check is SEMANTIC (compare the map/overrides dicts), not a byte
    compare of the serialized JSON. Dict equality ignores key order, so a store
    file written by an older or differently-formatted engine -- insertion order
    instead of sorted, or missing the trailing newline -- does NOT count as a
    change. Without this, every init on a second machine would rewrite the file
    purely to reformat it and produce a spurious, perpetual cross-platform diff.
    A real mapping change still rewrites, and rewrites in the normalized form."""
    path = os.path.join(store, "registry.json")
    existing = _load_registry(path)
    merged = _merge_registry(existing, reg)
    same = (
        existing.get("map", {}) == merged["map"]
        and existing.get("overrides", {}) == merged["overrides"]
    )
    # A missing file must be created even when the mapping is empty (bootstrap);
    # otherwise only a genuine mapping change triggers a (normalized) write.
    changed = (not os.path.exists(path)) or (not same)
    if changed and not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(merged, indent=2) + "\n")
    return path, changed, merged


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
        sep = (
            ""
            if existing == "" or existing.endswith("\n\n")
            else ("\n" if existing.endswith("\n") else "\n\n")
        )
        new_text = existing + sep + POINTER_BLOCK + "\n"
        action = "inserted"

    if action != "unchanged" and not dry_run:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    return action


# ---------------------------------------------------------------------------
# Store .gitignore upsert (#27)
# ---------------------------------------------------------------------------
def ensure_store_gitignore(path, dry_run):
    """Insert or refresh the managed `.gitignore` block at `path` (the
    store's `.gitignore`). Absent -> create with the block appended after any
    existing content. Present with the block -> replace it in place (a no-op
    when it already matches, so re-running is idempotent). Present without
    the block -> append it, leaving the user's hand-authored rules untouched.

    This writes INSIDE the store (the user's own data repo) -- rung-1
    additive bookkeeping, same tier as the registry write, so it runs
    unconditionally (not gated behind `apply_config`; it never touches
    `~/.claude`). Returns "created" | "appended" | "updated" | "unchanged"."""
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()

    block_re = re.compile(
        re.escape(GITIGNORE_MARKER_OPEN) + r".*?" + re.escape(GITIGNORE_MARKER_CLOSE),
        re.DOTALL,
    )
    if block_re.search(existing):
        new_text = block_re.sub(GITIGNORE_BLOCK, existing)
        action = "unchanged" if new_text == existing else "updated"
    else:
        sep = (
            ""
            if existing == "" or existing.endswith("\n\n")
            else ("\n" if existing.endswith("\n") else "\n\n")
        )
        new_text = existing + sep + GITIGNORE_BLOCK + "\n"
        action = "created" if existing == "" else "appended"

    if action != "unchanged" and not dry_run:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    return action


# ---------------------------------------------------------------------------
# Stale-reference scan (detection only unless --apply-cleanup)
# ---------------------------------------------------------------------------
CLEANUP_FILES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md")
CLEANUP_SKIP_DIRS = {
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    "dist",
    "build",
    ".next",
}


def _scan_one(real, findings, seen, fpath):
    if real in seen:
        return
    seen.add(real)
    try:
        with open(real, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return
    hits = [
        (i + 1, ln.rstrip("\n"), classify_line(ln))
        for i, ln in enumerate(lines)
        if STALE_RE.search(ln)
    ]
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
                lines[ln - 1] = lines[ln - 1].replace("claude-memory", "okfmem-store")
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
    # Claude Code: ~/.claude is its home dir (settings.json, CLAUDE.md live
    # there), a strong "installed" signal on its own.
    if os.path.isdir(os.path.join(home, ".claude")):
        out["claude_code"] = os.path.join(home, ".claude", "skills")
    # Codex: require the `codex` binary on PATH. A bare ~/.codex can linger after
    # an uninstall (or be created by another tool) holding nothing but a skills/
    # dir -- the dir alone is too weak a signal, and gating on it links skills
    # for an app that isn't actually present.
    if shutil.which("codex"):
        out["codex"] = os.path.join(home, ".codex", "skills")
    # Antigravity: its ~/.gemini home dir OR the `agy` binary on PATH.
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
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", link, target],
            capture_output=True,
            text=True,
            check=True,
        )
        return "junction"
    except (OSError, subprocess.CalledProcessError):
        pass
    # Tier 3: last-resort copy — always succeeds or raises loudly. Stamp a
    # marker so a later run treats this as a managed copy (re-copied on engine
    # update) instead of a user's real directory.
    shutil.copytree(target, link)
    try:
        with open(os.path.join(link, MANAGED_COPY_MARKER), "w", encoding="utf-8") as f:
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
        with open(os.path.join(link, MANAGED_COPY_MARKER), "r", encoding="utf-8") as f:
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
        # Compare RESOLVED targets, not the raw readlink string. On Windows
        # os.readlink returns an extended-length "\\?\C:\..." path that never
        # string-equals the plain target, which would repoint every single run.
        # realpath + normcase is correct on both platforms (and case-insensitive
        # on Windows).
        return os.path.normcase(os.path.realpath(link)) == os.path.normcase(
            os.path.realpath(target)
        )
    if os.path.isdir(link):
        if _is_junction(link):
            return os.path.normcase(os.path.realpath(link)) == os.path.normcase(
                os.path.realpath(target)
            )
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
    names = sorted(
        n
        for n in os.listdir(engine)
        if os.path.isfile(os.path.join(engine, n, "SKILL.md"))
    )
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
            is_managed = (
                os.path.islink(link)
                or _is_junction(link)
                or _managed_copy_target(link) is not None
            )
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
                        action = (
                            "repoint (copy — will go stale; re-run "
                            "okfmem init after engine updates)"
                        )
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
                        action = (
                            "link (copy — will go stale; re-run "
                            "okfmem init after engine updates)"
                        )
            actions.append((harness, name, action))
    return actions


def _current_git_root():
    """Absolute git root of the current working directory, or None if cwd
    isn't inside a git repo (or `git` isn't on PATH). Normalized so it is
    directly comparable to registry.json keys and safe to feed to
    `encode_root`."""
    try:
        # Force C locale so the "not a git repository" match below is reliable
        # regardless of the user's git locale -- otherwise the ordinary
        # not-in-a-repo case prints a spurious diagnostic on a non-English box.
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C"},
        )
    except OSError as e:
        # git missing from PATH / not executable -- a real failure, not the
        # ordinary "cwd isn't a repo" case. Surface it so it isn't silently
        # misreported downstream as "not inside a git repo".
        print(f"okfmem: could not run git to find the repo root: {e}", file=sys.stderr)
        return None
    if out.returncode != 0:
        stderr = (out.stderr or "").strip()
        # rc 128 + "not a git repository" is the expected not-in-a-repo case
        # (e.g. installer launched from ~) -- stay quiet, callers report a
        # clean skip. Anything else is unexpected: surface rc + stderr.
        if not (out.returncode == 128 and "not a git repository" in stderr.lower()):
            print(
                f"okfmem: git rev-parse failed (rc={out.returncode}): "
                f"{stderr or 'no stderr'}",
                file=sys.stderr,
            )
        return None
    return os.path.normpath(out.stdout.strip())


def link_project_memory(store, claude_projects, harnesses, reg, dry_run):
    """Create/repair the per-project memory symlink for the CURRENT repo
    (cwd's git root) -- `<claude_projects>/<encoded-root>/memory` ->
    `<store>/projects/<name>`.

    This is the missing half of `build_registry` (which only READS these
    links, never creates them) and of a harness's `STATE.md`/`MEMORY.md`
    auto-load: without it, a fresh machine leaves the memory dir as an empty
    real directory forever. Reuses `_make_link`'s tiered fallback (symlink ->
    junction -> copy), so this needs no elevation on Windows, exactly like
    `link_skills`.

    `reg` is a loaded registry dict (at least an `overrides` key) used ONLY
    to resolve root -> project name (the same `basename(root) unless
    overridden` rule the registry itself documents) -- pass the registry as
    it exists on disk BEFORE this run's `build_registry` re-derives it, so
    name resolution doesn't depend on a link this function is about to create.

    Returns (status, message):
      status in {"ok", "changed", "skip"}. "ok" = already correctly linked
      (idempotent no-op, matching the rest of `init`); "changed" = created or
      repaired (or, under `dry_run`, would be); "skip" = nothing to do this
      run, with the reason in `message` -- not in a git repo, no Claude Code
      harness detected, or the store has no project dir for this repo yet
      (nothing to link to before a first page is ever authored).
    """
    if not harnesses.get("claude_code"):
        return ("skip", "no Claude Code harness detected")
    root = _current_git_root()
    if not root:
        return ("skip", "not inside a git repo")
    name = reg.get("overrides", {}).get(root, os.path.basename(root))
    target = os.path.join(store, "projects", name)
    if not os.path.isdir(target):
        return ("skip", f"no store project dir for '{name}' yet")
    target_real = os.path.realpath(target)
    proj_dir = os.path.join(claude_projects, encode_root(root))
    link = os.path.join(proj_dir, "memory")

    # A tier-3 managed copy is a plain directory carrying our marker (the box
    # had neither symlink nor junction available). `_managed_copy_target`
    # returns the recorded source path, or None if this isn't our copy.
    managed_copy = _managed_copy_target(link)

    if os.path.islink(link) or _is_junction(link):
        if os.path.normcase(os.path.realpath(link)) == os.path.normcase(target_real):
            tier = "symlink" if os.path.islink(link) else "junction"
            return ("ok", f"{name} ({tier})")
        verb = "repoint"
    elif managed_copy is not None:
        # Our own tier-3 copy: recognize and refresh it the way `link_skills`
        # does, instead of misfiling it as a user's real dir ("resolve by
        # hand"). Idempotency is decided on TARGET identity (recorded marker
        # path vs the resolved store project) -- the same basis the live-link
        # branches above use. Deliberately NOT a byte-for-byte content compare
        # (which `link_skills` does for its single engine-versioned SKILL.md):
        # memory pages change every session, so content-comparing would re-copy
        # on nearly every run. Repoint only when the copy points at a different
        # project (rename/move); the "will go stale; re-run init" note carries
        # the content-refresh expectation for this rare last-resort tier.
        if os.path.normcase(os.path.realpath(managed_copy)) == os.path.normcase(
            target_real
        ):
            # A copy is a frozen snapshot, not a live link: pages added to the
            # store since it was made are NOT reflected, and (unlike a target
            # rename) init won't re-copy while the target matches. Flag it so
            # "up to date" doesn't read as "current". To refresh, delete the
            # copy and re-init (or enable a live link via Developer Mode).
            return ("ok", f"{name} (copy — frozen snapshot)")
        verb = "repoint"
    elif os.path.isdir(link):
        if os.listdir(link):
            return (
                "skip",
                "non-empty directory already at the memory link path — resolve by hand",
            )
        verb = "link"  # empty placeholder dir left by the harness -- nothing
        # was pointed before, so "linked to X" reads correctly (not "repointed").
        # The "empty real directory" case this issue exists for; safe to replace.
    elif os.path.exists(link):
        return ("skip", "unexpected file at the memory link path")
    else:
        verb = "link"

    if dry_run:
        return ("changed", f"would {verb} to {name}")

    os.makedirs(proj_dir, exist_ok=True)
    if os.path.islink(link):
        os.remove(link)
    elif _is_junction(link):
        os.rmdir(link)  # unlink the reparse point, not the target
    elif managed_copy is not None:
        shutil.rmtree(link)  # our tier-3 copy: a non-empty real dir we own
    elif os.path.isdir(link):
        os.rmdir(link)  # confirmed empty above
    tier = _make_link(target_real, link)
    if tier == "symlink":
        tier_note = ""
    elif tier == "junction":
        tier_note = " (junction)"
    else:
        tier_note = " (copy — will go stale; re-run okfmem init after engine updates)"
    return ("changed", f"{verb}ed to {name}{tier_note}")


def _write_settings_json(claude_dir, settings, data):
    """Write `data` to Claude Code's `settings.json`, backing up the
    immediately-prior state to `.okfmem.bak` on each write (the backup is
    overwritten every time, not kept once). Shared by `wire_stop_hook` and
    `wire_pull_hook` so both hooks back up/write identically."""
    os.makedirs(claude_dir, exist_ok=True)
    if os.path.exists(settings):
        try:
            shutil.copy2(settings, settings + ".okfmem.bak")
        except OSError:
            pass  # backup is best-effort; the write below is the real work
    with open(settings, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")


def wire_stop_hook(dry_run):
    """Idempotently wire okfmem's consolidation Stop hook into Claude Code's
    ~/.claude/settings.json so a fresh install needs no manual JSON paste.

    Returns (action, path):
      'added'      — appended our Stop hook (.okfmem.bak backs up the
                     immediately-prior state; overwritten on each write)
      'present'    — a Stop hook already runs memory_consolidate.py; left as-is
      'no-claude'  — ~/.claude missing; nothing to wire (other harnesses differ)
      'skip (...)' — settings.json unreadable/wrong shape; print the snippet

    Only Claude Code is auto-wired (its settings.json schema is known). Every
    existing setting and other hook is preserved — we append to hooks.Stop only
    when no entry there already invokes memory_consolidate.py.
    """
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    if not os.path.isdir(claude_dir):
        return ("no-claude", None)
    settings = os.path.join(claude_dir, "settings.json")
    engine = os.path.dirname(os.path.realpath(__file__))
    consolidate = os.path.join(engine, "memory_consolidate.py")
    # Absolute interpreter + script: robust at hook time regardless of PATH.
    command = f'"{sys.executable}" "{consolidate}" --stdin-hook'

    data = {}
    if os.path.exists(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return ("skip (settings.json unreadable — wire manually)", settings)
    if not isinstance(data, dict):
        return ("skip (settings.json not an object — wire manually)", settings)

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return ("skip (hooks not an object — wire manually)", settings)
    stop = hooks.setdefault("Stop", [])
    if not isinstance(stop, list):
        return ("skip (Stop hook not a list — wire manually)", settings)

    for group in stop:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []):
            if isinstance(h, dict) and "memory_consolidate.py" in h.get("command", ""):
                return ("present", settings)

    stop.append({"hooks": [{"type": "command", "command": command}]})
    if not dry_run:
        _write_settings_json(claude_dir, settings, data)
    return ("added", settings)


# ---------------------------------------------------------------------------
# SessionStart store-pull hook (cross-machine sync)
# ---------------------------------------------------------------------------
# Pre-#16, the ONLY way this hook existed was a hand-added `git -C <path> pull
# --rebase` command per the manual setup instructions (see the `okfmem` skill's
# old "Usage" section) -- never through the okfmem CLI. SessionStart has no
# other conventional use for a raw `git ... pull` command, so ANY such entry
# that isn't already ours is that legacy shape and is unconditionally stale:
# it freezes the store path at the moment someone typed it, so a store rename
# (the `claude-memory` -> `okfmem-store` migration this issue's evidence
# describes) leaves it silently pulling a dead clone forever. Treat it the
# same way MEMORY-POINTER/Stop-hook markers are treated -- heal in place, no
# human required.
# Only the pre-#16 MANAGED shape heals: a raw `git -C <path> pull` whose
# <path> references the memory store (the `claude-memory` -> `okfmem-store`
# clone this heal exists for). A user's unrelated `git -C ~/notes pull`
# SessionStart hook must be LEFT ALONE — the old unanchored `\bgit\b.*\bpull\b`
# clobbered it. Match `git ... -C <path-with-a-store-name> ... pull`.
_LEGACY_STORE_NAMES = r"(?:claude-memory|okfmem-store|okfmem[\\/]store)"
# NB: NOT DOTALL and matched per shell-segment (see `_split_command_segments`).
# `.*` must never span a `;`/`&&` into a second, unrelated `git ... pull`
# clause — a compound line like
#   git -C "~/okfmem-store" fetch; git -C "~/my-notes" pull --rebase
# has the store-named part and the unrelated pull in DIFFERENT segments, so
# neither segment alone is legacy and the whole line is left untouched.
_LEGACY_PULL_RE = re.compile(
    r"\bgit\b.*-C\s+[\"']?[^\"'\s]*" + _LEGACY_STORE_NAMES + r"[^\"'\s]*"
    r"[\"']?.*\bpull\b",
    re.IGNORECASE,
)
# Any `git ... pull` in a single segment, store-named or not — used to detect
# an unrelated pull we must NOT clobber.
_ANY_GIT_PULL_RE = re.compile(r"\bgit\b.*\bpull\b", re.IGNORECASE)
# Shell command separators that start a fresh command clause.
_CMD_SEPARATORS = re.compile(r"(?:&&|\|\||[;&\n])")


def _split_command_segments(cmd):
    """Split a hook command line into independent shell-command segments on
    `;`, `&&`, `||`, `&`, and newlines, so a per-invocation regex can't leak
    `.*` across an unrelated clause. Cheap textual split (not a real shell
    parse) — good enough to keep each `git ... pull` isolated."""
    return [seg.strip() for seg in _CMD_SEPARATORS.split(cmd) if seg.strip()]


def _is_managed_pull_command(cmd):
    """True if `cmd` already invokes okfmem's own `pull` subcommand (any prior
    okfmem release / engine path), so re-running init recognizes an
    up-to-date entry and leaves it alone rather than rewriting every run."""
    return bool(re.search(r"\bokfmem[\"']?\s+pull\b", cmd))


def _is_legacy_pull_command(cmd):
    """True if `cmd` is the pre-#16 hand-added managed store-pull shape that
    should heal in place. Guards two ways: a command that already invokes
    okfmem's own `pull` subcommand (our managed line) is NEVER legacy, and
    only a `git -C <store-path> pull` whose path names the memory store
    matches — an unrelated `git -C ~/notes pull` is left alone. Note the store
    dir may itself be named `okfmem-store`, so the guard keys on the okfmem
    *CLI invocation*, not the bare substring `okfmem`.

    Compound lines are split into shell segments first: a command is legacy
    ONLY if at least one segment is the store-named legacy pull AND no segment
    is an unrelated `git ... pull` we'd otherwise clobber. So
    `git -C "~/okfmem-store" fetch; git -C "~/my-notes" pull` is left alone —
    its pull segment isn't store-named."""
    if _is_managed_pull_command(cmd):
        return False
    segments = _split_command_segments(cmd)
    has_legacy = any(_LEGACY_PULL_RE.search(seg) for seg in segments)
    if not has_legacy:
        return False
    # Any git-pull segment that is NOT the store-named legacy shape is an
    # unrelated pull — refuse to heal (would destroy the user's own hook).
    for seg in segments:
        if _ANY_GIT_PULL_RE.search(seg) and not _LEGACY_PULL_RE.search(seg):
            return False
    return True


def detect_legacy_clone():
    """Detect a leftover `~/claude-memory` git clone from the claude-memory ->
    okfmem-store rename (the dead-clone bug #16 exists to prevent recurring).
    Returns the path if found, else None. NEVER deleted automatically --
    migration cleanup is left to the user; this is report-only."""
    legacy = os.path.join(os.path.expanduser("~"), "claude-memory")
    if os.path.isdir(os.path.join(legacy, ".git")):
        return legacy
    return None


def wire_pull_hook(dry_run):
    """Idempotently wire a SessionStart hook that runs `okfmem pull --quiet`
    (memory_pull.py) so a session on ANY machine starts from the store's
    latest pushed state -- before the harness auto-loads STATE.md/MEMORY.md.

    Mirrors `wire_stop_hook`'s contract (same settings.json shape, same
    backup-then-write discipline via `_write_settings_json`) but additionally
    HEALS a legacy hand-added hook: a raw `git -C <path> pull --rebase`
    command from the pre-#16 manual setup instructions, most visibly the case
    where `<path>` still points at the pre-rename `claude-memory` clone. Any
    such entry -- or a stale command from an older okfmem release -- is
    replaced in place with the current `okfmem pull --quiet` invocation, which
    always resolves the CURRENT store (`--store` -> `$OKFMEM_STORE` ->
    `~/okfmem-store`) rather than a path frozen at hook-write time.

    Fail-open is enforced by `okfmem pull` itself (memory_pull.py), not here:
    this only ever writes `... pull --quiet`, which exits 0 on
    offline/no-op/already-up-to-date and 1 ONLY on a manual rebase conflict a
    human must resolve by hand -- Claude Code does not treat a hook's exit
    code as fatal to SessionStart, so this never blocks a session.

    The command embeds the absolute interpreter (`sys.executable`) and engine
    script path via `os.path.join`/f-string (not a hardcoded shell path), so
    it is portable across macOS/Linux/Windows without hand-editing -- same
    approach `wire_stop_hook` already uses for the consolidation hook.

    Returns (action, path):
      'added'      — no prior SessionStart pull hook; appended ours
      'healed'     — replaced a legacy/stale entry (raw git pull, or an
                     outdated okfmem command) with the current one
      'present'    — already wired correctly; left as-is
      'no-claude'  — ~/.claude missing; nothing to wire
      'skip (...)' — settings.json unreadable/wrong shape; print the snippet
    """
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    if not os.path.isdir(claude_dir):
        return ("no-claude", None)
    settings = os.path.join(claude_dir, "settings.json")
    engine = os.path.dirname(os.path.realpath(__file__))
    okfmem_cli = os.path.join(engine, "okfmem")
    # Absolute interpreter + script: robust at hook time regardless of PATH,
    # cross-platform (os.path.join uses the right separator per-OS).
    command = f'"{sys.executable}" "{okfmem_cli}" pull --quiet'

    data = {}
    if os.path.exists(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return ("skip (settings.json unreadable — wire manually)", settings)
    if not isinstance(data, dict):
        return ("skip (settings.json not an object — wire manually)", settings)

    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        return ("skip (hooks not an object — wire manually)", settings)
    starts = hooks.setdefault("SessionStart", [])
    if not isinstance(starts, list):
        return ("skip (SessionStart hook not a list — wire manually)", settings)

    for group in starts:
        if not isinstance(group, dict):
            continue
        for h in group.get("hooks", []):
            if not isinstance(h, dict):
                continue
            cur = h.get("command", "")
            if _is_managed_pull_command(cur):
                if cur == command:
                    return ("present", settings)
                h["command"] = command  # heal: older okfmem release/path
                if not dry_run:
                    _write_settings_json(claude_dir, settings, data)
                return ("healed", settings)
            if _is_legacy_pull_command(cur):
                h["command"] = command  # heal: pre-#16 hand-added raw git pull
                if not dry_run:
                    _write_settings_json(claude_dir, settings, data)
                return ("healed", settings)

    starts.append({"hooks": [{"type": "command", "command": command}]})
    if not dry_run:
        _write_settings_json(claude_dir, settings, data)
    return ("added", settings)


def _prompt_yes_no(question, *, assume_yes, non_interactive, manual_hint):
    """Rung-2 confirmation gate (see CLAUDE.md 'Confirmation discipline').

    Returns True to proceed with a config-mutating op. `assume_yes` (the
    installer / `--yes` / CI path) short-circuits to True. A non-interactive
    run WITHOUT assume_yes takes the safe default -- skip -- and prints the
    exact manual command so an automated install never hangs on a prompt and
    the user can still apply it later. Otherwise ask, defaulting to No.

    Prints its own outcome line (so the caller doesn't double-report), and
    returns the decision so the caller can skip the actual mutation."""
    if assume_yes:
        return True
    if non_interactive:
        print(f"{glyph('ok')} {question}\n    skipped (non-interactive). {manual_hint}")
        return False
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        # stdin closed mid-prompt -- treat as the safe default, not a crash.
        print(f"    skipped. {manual_hint}")
        return False
    if ans in ("y", "yes"):
        return True
    print(f"    skipped. {manual_hint}")
    return False


def cmd_run(
    store, dry_run, apply_cleanup, verbose=False, wire_hook=True, assume_yes=False
):
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

    # --- config-mutation gate (rung-2; CLAUDE.md 'Confirmation discipline') -
    # init mutates user config: writes ~/.claude/settings.json (hooks), injects
    # okfmem's pointer block into the harness globals (~/.claude/CLAUDE.md,
    # ~/.gemini/config/AGENTS.md), and creates skill/memory links under
    # ~/.claude. All of that sits behind ONE consent gate. --dry-run only
    # previews (never prompts). Otherwise ask once; --yes (installers/CI) or a
    # non-TTY caller resolve without blocking -- skipping and printing the
    # manual command rather than hanging. The registry write to the STORE (the
    # user's own data repo) is additive bookkeeping (rung-1) and runs either way.
    config_hint = "Apply later with: okfmem init --yes"
    if dry_run:
        apply_config = True  # preview only -- ops run in dry_run, mutate nothing
    else:
        apply_config = _prompt_yes_no(
            "Wire okfmem into ~/.claude (hooks, harness pointers, and skill/"
            "memory links)?",
            assume_yes=assume_yes,
            non_interactive=not sys.stdin.isatty(),
            manual_hint=config_hint,
        )

    # --- 1b. memory link (current repo) ------------------------------------
    # Runs BEFORE the registry step, on the registry as it exists on disk,
    # so name resolution doesn't depend on the link we're about to create --
    # and so that once created, THIS run's registry build (below) already
    # sees it and reports the project as linked, with no second `init` needed.
    if apply_config:
        existing_reg = _load_registry(os.path.join(store, "registry.json"))
        maction, mmsg = link_project_memory(
            store, claude_projects, harnesses, existing_reg, dry_run
        )
        if maction == "changed":
            changes += 1
            print(f"{glyph('chg')} memory link {mmsg}")
        elif maction == "ok":
            print(f"{glyph('ok')} memory link up to date ({mmsg})")
        else:
            print(f"{glyph('ok')} memory link skipped ({mmsg})")
    else:
        print(f"{glyph('ok')} memory link skipped (config changes declined)")

    # --- 2. registry -------------------------------------------------------
    reg, drift = build_registry(store, claude_projects)
    reg_path, reg_changed, merged = write_registry(store, reg, dry_run)
    changes += 1 if reg_changed else 0
    # Report the MERGED registry (what's actually on disk), not just this
    # machine's locally-derived slice -- otherwise a machine with no local
    # symlinks would misleadingly report "0 projects" for a full store.
    detail = f"{len(merged['map'])} projects" + (
        f", {len(merged['overrides'])} renamed" if merged["overrides"] else ""
    )
    if reg_changed:
        print(
            f"{glyph('chg')} registry    "
            f"{'would update' if dry_run else 'updated'} ({detail})"
        )
    else:
        print(f"{glyph('ok')} registry    up to date ({detail})")
    if verbose and merged["overrides"]:
        for root, proj in merged["overrides"].items():
            print(f"    {_short(root)} → {proj}")
    for d in drift:
        print(f"    {glyph('warn')} {d}")

    # --- 2b. store .gitignore (#27) -----------------------------------------
    # Rung-1: writes INSIDE the store (the user's own data repo), same tier
    # as the registry write above -- runs unconditionally, never gated behind
    # the config-mutation prompt (it never touches ~/.claude).
    gi_path = os.path.join(store, ".gitignore")
    gi_action = ensure_store_gitignore(gi_path, dry_run)
    if gi_action != "unchanged":
        changes += 1
        verb = {
            "created": "would create" if dry_run else "created",
            "appended": "would append to" if dry_run else "appended to",
            "updated": "would update" if dry_run else "updated",
        }[gi_action]
        print(f"{glyph('chg')} .gitignore  {verb} ({_short(gi_path)})")
    else:
        print(f"{glyph('ok')} .gitignore  up to date ({_short(gi_path)})")

    # --- 3. pointers -------------------------------------------------------
    # Also rung-2: upsert_pointer injects okfmem's managed block into the
    # user's hand-edited harness globals (~/.claude/CLAUDE.md,
    # ~/.gemini/config/AGENTS.md). That's user-config mutation, so it lives
    # under the same consent gate as the hooks/links above -- declining must
    # leave those files untouched.
    if not apply_config:
        print(f"{glyph('ok')} pointers    skipped (config changes declined)")
    else:
        pacts = [(n, upsert_pointer(p, dry_run), p) for n, p in harnesses.items() if p]
        pchg = [a for a in pacts if a[1] != "unchanged"]
        changes += len(pchg)
        if pchg:
            print(
                f"{glyph('chg')} pointers    "
                f"{len(pchg)} to write ({len(pacts)} harness globals)"
            )
        else:
            print(
                f"{glyph('ok')} pointers    up to date ({len(pacts)} harness globals)"
            )
        if verbose or pchg:
            for name, action, path in pacts:
                k = "ok" if action == "unchanged" else "chg"
                print(f"    {glyph(k)} {name:12} {action:10} {_short(path)}")

    # --- 4. stale references ----------------------------------------------
    findings = scan_stale(
        reg, harnesses["claude_code"] or "", harness_globals=[harnesses["antigravity"]]
    )
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
            parts.append(
                f"{cats['notice']} notice{'s' if cats['notice'] != 1 else ''} (kept)"
            )
        kind = "ok" if not actionable else ("warn" if cats["review"] else "chg")
        print(
            f"{glyph(kind)} stale refs  {total} across "
            f"{len(findings)} file(s): {', '.join(parts)}"
        )
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
            print(
                f"    → {verb} {lc} path line(s) in {fc} file(s); "
                f"notice/review lines untouched"
            )
            if cats["review"]:
                print(
                    f"    {glyph('warn')} {cats['review']} review line(s) "
                    f"need a human — not edited"
                )
        elif cats["path"]:
            print(
                _c(
                    f"    → run with --apply-cleanup to rewrite "
                    f"{cats['path']} path line(s)",
                    "dim",
                )
            )

    # --- 5. skills ---------------------------------------------------------
    if not apply_config:
        print(f"{glyph('ok')} skills      skipped (config changes declined)")
        sk = []
    else:
        sk = link_skills(dry_run)
    if apply_config and not sk:
        print(f"{glyph('warn')} skills      none to link (no harness skill dirs)")
    elif apply_config:
        chg = [a for a in sk if a[2] != "ok"]
        changes += len(chg)
        n_ok = len(sk) - len(chg)
        # The label column already says "skills", so the value doesn't repeat it.
        # A bare count ("9") is opaque and the skills-x-harnesses math means
        # nothing to the user -- lead with the outcome and NAME the harnesses.
        hnames = ", ".join(sorted({a[0] for a in sk}))
        if chg:
            print(
                f"{glyph('chg')} skills      "
                f"{len(chg)} to link into {hnames} ({n_ok} already linked)"
            )
        else:
            print(f"{glyph('ok')} skills      all linked into {hnames}")
        if verbose or chg:
            for harness, name, action in chg:
                print(f"    {glyph('chg')} {harness:12} {action:16} {name}")

    # --- 6. Stop hook (Claude Code, auto-wired) ---------------------------
    if not apply_config:
        print(f"{glyph('ok')} stop hook   skipped (config changes declined)")
    elif wire_hook:
        haction, hpath = wire_stop_hook(dry_run)
        if haction == "added":
            changes += 1
            verb = "would wire" if dry_run else "wired"
            print(
                f"{glyph('chg')} stop hook   {verb} consolidation hook "
                f"({_short(hpath)})"
            )
        elif haction == "present":
            print(f"{glyph('ok')} stop hook   already wired ({_short(hpath)})")
        elif haction == "no-claude":
            print(f"{glyph('ok')} stop hook   no Claude Code dir — skipped")
        else:
            print(f"{glyph('warn')} stop hook   {haction}")
    else:
        print(f"{glyph('ok')} stop hook   skipped (--no-hook)")

    # --- 7. SessionStart pull hook (cross-machine sync, auto-wired) --------
    if not apply_config:
        print(f"{glyph('ok')} pull hook   skipped (config changes declined)")
    elif wire_hook:
        paction, ppath = wire_pull_hook(dry_run)
        if paction in ("added", "healed"):
            changes += 1
            verb = (
                ("would wire" if dry_run else "wired")
                if paction == "added"
                else ("would heal" if dry_run else "healed")
            )
            print(
                f"{glyph('chg')} pull hook   {verb} store-pull hook ({_short(ppath)})"
            )
        elif paction == "present":
            print(f"{glyph('ok')} pull hook   already wired ({_short(ppath)})")
        elif paction == "no-claude":
            print(f"{glyph('ok')} pull hook   no Claude Code dir — skipped")
        else:
            print(f"{glyph('warn')} pull hook   {paction}")
    else:
        print(f"{glyph('ok')} pull hook   skipped (--no-hook)")

    # --- 8. leftover claude-memory clone (migration cleanup, warn only) ----
    legacy = detect_legacy_clone()
    if legacy:
        print(
            f"{glyph('warn')} legacy clone  found {_short(legacy)} — a "
            f"leftover pre-rename clone; not the live store, not deleted "
            f"automatically. Remove it by hand once you've confirmed "
            f"nothing still points at it."
        )

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


# ---------------------------------------------------------------------------
# Update nudge — passive "engine update available" hint on interactive status
# ---------------------------------------------------------------------------
def _update_cache_path():
    """A cache file OUTSIDE both git repos. Writing into the engine or store
    clone would dirty its tree and break `okfmem update --ff-only`."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return os.path.join(base, "okfmem", "update-check")


def _git_engine(*args, timeout=None):
    engine = os.path.dirname(os.path.realpath(__file__))
    return subprocess.run(
        ["git", "-C", engine, *args], capture_output=True, text=True, timeout=timeout
    )


def update_nudge():
    """Return a one-line 'update available' hint, or None.

    Interactive (TTY) only, so pipes and the consolidation Stop hook never touch
    the network. Fetches at most once per day (a date stamp cached outside both
    git repos), and the stamp is written BEFORE the fetch so an offline machine
    tries once and then reads local refs for the rest of the day rather than
    hanging on every status. Silent on any failure — not a clone, no upstream,
    offline, git missing.
    """
    if not sys.stdout.isatty():
        return None
    try:
        r = _git_engine("rev-parse", "--is-inside-work-tree")
        if r.returncode != 0 or r.stdout.strip() != "true":
            return None
        up = _git_engine("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if up.returncode != 0:
            return None
        upstream = up.stdout.strip()

        today = time.strftime("%Y-%m-%d", time.localtime())
        cache = _update_cache_path()
        last = ""
        if os.path.exists(cache):
            with open(cache, "r", encoding="utf-8") as f:
                last = f.read().strip()
        if last != today:
            os.makedirs(os.path.dirname(cache), exist_ok=True)
            with open(cache, "w", encoding="utf-8") as f:
                f.write(today)  # stamp first: one attempt/day even when offline
            try:
                _git_engine("fetch", "--quiet", timeout=5)
            except Exception:
                pass  # offline / slow remote — fall back to local refs

        behind = _git_engine("rev-list", "--count", f"HEAD..{upstream}").stdout.strip()
        if behind and behind != "0":
            return (
                f"{behind} engine update(s) available on {upstream} — "
                f"run `okfmem update`"
            )
    except Exception:
        return None
    return None


def cmd_status(store):
    home = os.path.expanduser("~")
    harnesses = detect_harnesses()
    reg_path = os.path.join(store, "registry.json")

    print("== okfmem status ==")
    for name, path in harnesses.items():
        wired = "—"
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                wired = (
                    "pointer PRESENT" if MARKER_OPEN in f.read() else "pointer MISSING"
                )
        print(f"  {name:12} {path or 'not found'}   {wired}")

    if os.path.exists(reg_path):
        with open(reg_path) as f:
            reg = json.load(f)
        print(
            f"\n  registry: {len(reg.get('map', {}))} roots, "
            f"{len(reg.get('overrides', {}))} overrides  ({reg_path})"
        )
    else:
        print(f"\n  registry: MISSING ({reg_path})")

    reg = build_registry(store, os.path.join(home, ".claude", "projects"))[0]
    findings = scan_stale(
        reg, harnesses["claude_code"] or "", harness_globals=[harnesses["antigravity"]]
    )
    total = sum(len(h) for _, h in findings)
    cats = {"path": 0, "notice": 0, "review": 0}
    for _, hits in findings:
        for _, _, cat in hits:
            cats[cat] += 1
    print(
        f"  stale refs: {total} line(s) across {len(findings)} file(s) "
        f"({cats['path']} path-swap, {cats['notice']} notice, "
        f"{cats['review']} review)"
    )

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

    # Stop hook (Claude Code)
    settings = os.path.join(home, ".claude", "settings.json")
    hook = "not wired — run `okfmem init`"
    if os.path.exists(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            stop = (sdata.get("hooks", {}) or {}).get("Stop", []) or []
            if any(
                isinstance(h, dict) and "memory_consolidate.py" in h.get("command", "")
                for g in stop
                if isinstance(g, dict)
                for h in g.get("hooks", [])
            ):
                hook = "wired"
        except (OSError, ValueError):
            hook = "settings.json unreadable"
    print(f"  stop hook: {hook}")

    # SessionStart store-pull hook (Claude Code)
    pull_hook = "not wired — run `okfmem init`"
    if os.path.exists(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            starts = (sdata.get("hooks", {}) or {}).get("SessionStart", []) or []
            cmds = [
                h.get("command", "")
                for g in starts
                if isinstance(g, dict)
                for h in g.get("hooks", [])
                if isinstance(h, dict)
            ]
            if any(_is_managed_pull_command(c) for c in cmds):
                pull_hook = "wired"
            elif any(_is_legacy_pull_command(c) for c in cmds):
                pull_hook = "STALE (legacy raw git pull — run `okfmem init` to heal)"
        except (OSError, ValueError):
            pull_hook = "settings.json unreadable"
    print(f"  pull hook: {pull_hook}")

    # Store sync state — the "is my memory actually backed up?" half of
    # status. Wiring can be perfectly green while the store itself sits
    # dirty, unpushed, or delinked from its remote (exactly what an
    # uninstall/reinstall cycle leaves behind); without this line, status
    # says "all ok" while memory exists on one disk only.
    def _sgit(*args):
        return subprocess.run(
            ["git", "-C", store, *args], capture_output=True, text=True, timeout=30
        )

    if _sgit("rev-parse", "--git-dir").returncode != 0:
        print("  ! store sync: NOT A GIT REPO — re-run install to initialize")
    else:
        problems = []
        st = _sgit("status", "--porcelain")
        if st.returncode == 0:
            n_dirty = len([ln for ln in st.stdout.splitlines() if ln.strip()])
            if n_dirty:
                problems.append(f"{n_dirty} uncommitted change(s)")

        upstream_name = None
        remote = _sgit("remote", "get-url", "origin")
        if remote.returncode != 0:
            problems.append(
                "NO REMOTE — memory is not backed up (re-run install to relink)"
            )
        else:
            up = _sgit("rev-parse", "--abbrev-ref", "@{u}")
            if up.returncode != 0:
                problems.append(
                    "no upstream tracking branch "
                    "(git branch --set-upstream-to=origin/main)"
                )
            else:
                upstream_name = up.stdout.strip()
                # `--left-right --count @{u}...HEAD` prints "<behind>\t<ahead>":
                # left = commits only on upstream, right = commits only on HEAD.
                counts = _sgit("rev-list", "--left-right", "--count", "@{u}...HEAD")
                if counts.returncode == 0:
                    parts = counts.stdout.split()
                    behind, ahead = (parts + ["0", "0"])[:2]
                    if ahead != "0":
                        problems.append(f"{ahead} unpushed commit(s)")
                    if behind != "0":
                        problems.append(f"{behind} commit(s) behind {upstream_name}")

        if problems:
            print(f"  ! store sync: {'; '.join(problems)} — run `okfmem sync`")
        else:
            suffix = f" with {upstream_name}" if upstream_name else ""
            print(f"  store sync: clean, in sync{suffix}")

    legacy_clone = detect_legacy_clone()
    if legacy_clone:
        print(
            f"  {glyph('warn')} legacy clone: {_short(legacy_clone)} "
            f"(leftover pre-rename clone — not deleted automatically)"
        )

    nudge = update_nudge()
    if nudge:
        print(f"\n  {glyph('chg')} {nudge}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--status", action="store_true", help="print wiring + drift, change nothing"
    )
    ap.add_argument(
        "--dry-run", action="store_true", help="print the plan, write nothing"
    )
    ap.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="expand every step to per-item detail (default: quiet — "
        "idle steps collapse to one line)",
    )
    ap.add_argument(
        "--apply-cleanup",
        action="store_true",
        help="rewrite claude-memory->okfmem-store on path lines "
        "(retirement-notice + review lines left untouched)",
    )
    ap.add_argument(
        "--no-hook",
        action="store_true",
        help="do not auto-wire the Claude Code consolidation Stop "
        "hook or the SessionStart store-pull hook into "
        "~/.claude/settings.json",
    )
    ap.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="apply config changes (settings.json hooks + skill/"
        "memory links under ~/.claude) without prompting -- "
        "for installers/CI. A non-interactive run WITHOUT this "
        "skips them and prints the manual command instead.",
    )
    ap.add_argument(
        "--store",
        default=os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store")),
    )
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    if not os.path.isdir(os.path.join(store, "projects")):
        print(f"error: no projects/ under {store}", file=sys.stderr)
        sys.exit(2)

    if args.status:
        cmd_status(store)
    else:
        cmd_run(
            store,
            args.dry_run,
            args.apply_cleanup,
            args.verbose,
            wire_hook=not args.no_hook,
            assume_yes=args.yes,
        )


if __name__ == "__main__":
    main()

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
import time

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
    same = (existing.get("map", {}) == merged["map"]
            and existing.get("overrides", {}) == merged["overrides"])
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
        # Compare RESOLVED targets, not the raw readlink string. On Windows
        # os.readlink returns an extended-length "\\?\C:\..." path that never
        # string-equals the plain target, which would repoint every single run.
        # realpath + normcase is correct on both platforms (and case-insensitive
        # on Windows).
        return os.path.normcase(os.path.realpath(link)) == \
            os.path.normcase(os.path.realpath(target))
    if os.path.isdir(link):
        if _is_junction(link):
            return os.path.normcase(os.path.realpath(link)) == \
                os.path.normcase(os.path.realpath(target))
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


def wire_stop_hook(dry_run):
    """Idempotently wire okfmem's consolidation Stop hook into Claude Code's
    ~/.claude/settings.json so a fresh install needs no manual JSON paste.

    Returns (action, path):
      'added'      — appended our Stop hook (a one-time .okfmem.bak is written)
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
        os.makedirs(claude_dir, exist_ok=True)
        if os.path.exists(settings):
            try:
                shutil.copy2(settings, settings + ".okfmem.bak")
            except OSError:
                pass  # backup is best-effort; the write below is the real work
        with open(settings, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
    return ("added", settings)


def cmd_run(store, dry_run, apply_cleanup, verbose=False, wire_hook=True):
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
    reg_path, reg_changed, merged = write_registry(store, reg, dry_run)
    changes += 1 if reg_changed else 0
    # Report the MERGED registry (what's actually on disk), not just this
    # machine's locally-derived slice -- otherwise a machine with no local
    # symlinks would misleadingly report "0 projects" for a full store.
    detail = f"{len(merged['map'])} projects" + (
        f", {len(merged['overrides'])} renamed" if merged["overrides"] else "")
    if reg_changed:
        print(f"{glyph('chg')} registry    "
              f"{'would update' if dry_run else 'updated'} ({detail})")
    else:
        print(f"{glyph('ok')} registry    up to date ({detail})")
    if verbose and merged["overrides"]:
        for root, proj in merged["overrides"].items():
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
        # The label column already says "skills", so the value doesn't repeat it.
        # A bare count ("9") is opaque and the skills-x-harnesses math means
        # nothing to the user -- lead with the outcome and NAME the harnesses.
        hnames = ", ".join(sorted({a[0] for a in sk}))
        if chg:
            print(f"{glyph('chg')} skills      "
                  f"{len(chg)} to link into {hnames} ({n_ok} already linked)")
        else:
            print(f"{glyph('ok')} skills      all linked into {hnames}")
        if verbose or chg:
            for harness, name, action in chg:
                print(f"    {glyph('chg')} {harness:12} {action:16} {name}")

    # --- 6. Stop hook (Claude Code, auto-wired) ---------------------------
    if wire_hook:
        haction, hpath = wire_stop_hook(dry_run)
        if haction == "added":
            changes += 1
            verb = "would wire" if dry_run else "wired"
            print(f"{glyph('chg')} stop hook   {verb} consolidation hook "
                  f"({_short(hpath)})")
        elif haction == "present":
            print(f"{glyph('ok')} stop hook   already wired ({_short(hpath)})")
        elif haction == "no-claude":
            print(f"{glyph('ok')} stop hook   no Claude Code dir — skipped")
        else:
            print(f"{glyph('warn')} stop hook   {haction}")
    else:
        print(f"{glyph('ok')} stop hook   skipped (--no-hook)")

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
    return subprocess.run(["git", "-C", engine, *args],
                          capture_output=True, text=True, timeout=timeout)


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
        up = _git_engine("rev-parse", "--abbrev-ref",
                         "--symbolic-full-name", "@{u}")
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

        behind = _git_engine("rev-list", "--count",
                             f"HEAD..{upstream}").stdout.strip()
        if behind and behind != "0":
            return (f"{behind} engine update(s) available on {upstream} — "
                    f"run `okfmem update`")
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

    # Stop hook (Claude Code)
    settings = os.path.join(home, ".claude", "settings.json")
    hook = "not wired — run `okfmem init`"
    if os.path.exists(settings):
        try:
            with open(settings, "r", encoding="utf-8") as f:
                sdata = json.load(f)
            stop = (sdata.get("hooks", {}) or {}).get("Stop", []) or []
            if any(isinstance(h, dict)
                   and "memory_consolidate.py" in h.get("command", "")
                   for g in stop if isinstance(g, dict)
                   for h in g.get("hooks", [])):
                hook = "wired"
        except (OSError, ValueError):
            hook = "settings.json unreadable"
    print(f"  stop hook: {hook}")

    nudge = update_nudge()
    if nudge:
        print(f"\n  {glyph('chg')} {nudge}")


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
    ap.add_argument("--no-hook", action="store_true",
                    help="do not auto-wire the Claude Code consolidation Stop "
                         "hook into ~/.claude/settings.json")
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
        cmd_run(store, args.dry_run, args.apply_cleanup, args.verbose,
                wire_hook=not args.no_hook)


if __name__ == "__main__":
    main()

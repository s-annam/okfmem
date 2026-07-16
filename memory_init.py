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
import sys

# ---------------------------------------------------------------------------
# Managed pointer block
# ---------------------------------------------------------------------------
MARKER_OPEN = "<!-- MEMORY-POINTER v1 (managed by memory-init — do not edit between markers) -->"
MARKER_CLOSE = "<!-- /MEMORY-POINTER -->"

POINTER_BODY = """## Memory
Durable project memory: `~/okfmem-store/projects/<PROJECT>/`
`<PROJECT>` = basename of the git root, unless overridden in `~/okfmem-store/registry.json`.
- Read `MEMORY.md` (the index) first — one grep-friendly line per topic page.
- To recall a topic: grep the memory dir for keywords, then open the matching `<slug>.md`.
  Do NOT eager-read every page.
- Pages are OKF markdown. Frontmatter (`pinned`/`importance`/`status`/`access_count`) is
  maintenance metadata — ignore it when reasoning."""

POINTER_BLOCK = f"{MARKER_OPEN}\n{POINTER_BODY}\n{MARKER_CLOSE}"

# Retired-system references the cleanup pass looks for. The ONLY legitimate
# surviving mention is the retirement-notice sentence in ~/.claude/CLAUDE.md.
STALE_PATTERNS = [
    r"\bmemgraph\b",
    r"\bread_graph\b", r"\bsearch_nodes\b",
    r"\bcreate_entities\b", r"\badd_observations\b",
    r"\bprojector\b", r"\bpush-primer\b",
    r"source:\s*graph",
    r"\bcontext-keeper\b", r"/ck:save", r"\bcontext\.json\b",
    r"\bck/contexts\b",
]
STALE_RE = re.compile("|".join(STALE_PATTERNS), re.IGNORECASE)


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
def decode_root(encoded):
    """`-Users-annam-worktree-autosync` -> `/Users/annam/worktree-autosync`.

    Claude Code encodes a cwd by replacing '/' with '-', which is lossy because
    directory names can themselves contain '-'. Reverse it by probing the real
    filesystem: walk the '-'-split tokens left to right, greedily preferring the
    longest existing directory at each step.
    """
    tokens = encoded.lstrip("-").split("-")
    path = "/"
    i = 0
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
            return os.path.join(path, "-".join(tokens[i:]))
        path = os.path.join(path, best)
        i = best_j + 1
    return path


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
    path = os.path.join(store, "registry.json")
    text = json.dumps(reg, indent=2) + "\n"
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path


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


def scan_stale(reg, self_claude_md):
    """Report retired-system references across registered project roots.

    Dedupes by real path (many repos have AGENTS.md -> CLAUDE.md symlinks).
    The retirement-notice sentence in ~/.claude/CLAUDE.md is the one legitimate
    mention and is excluded.
    """
    seen = set()
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
                real = os.path.realpath(fpath)
                if real in seen:
                    continue
                seen.add(real)
                if os.path.realpath(self_claude_md) == real:
                    continue  # retirement notice lives here — leave it
                try:
                    with open(real, "r", encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                except OSError:
                    continue
                hits = [(i + 1, ln.rstrip("\n"))
                        for i, ln in enumerate(lines) if STALE_RE.search(ln)]
                if hits:
                    findings.append((fpath, hits))
    return findings


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
def cmd_run(store, dry_run, apply_cleanup):
    home = os.path.expanduser("~")
    claude_projects = os.path.join(home, ".claude", "projects")
    harnesses = detect_harnesses()

    print("== harnesses ==")
    for name, path in harnesses.items():
        print(f"  {name:12} {'DETECTED  ' + path if path else 'not found'}")

    print("\n== registry ==")
    reg, drift = build_registry(store, claude_projects)
    reg_path = write_registry(store, reg, dry_run)
    print(f"  {len(reg['map'])} roots mapped, {len(reg['overrides'])} overrides"
          f"  -> {reg_path}")
    for root, proj in reg["overrides"].items():
        print(f"    override: {root} -> {proj}")
    for d in drift:
        print(f"  drift: {d}")

    print("\n== pointers ==")
    for name, path in harnesses.items():
        if not path:
            continue
        action = upsert_pointer(path, dry_run)
        print(f"  {name:12} {action:10} {path}")

    print("\n== stale references ==")
    findings = scan_stale(reg, harnesses["claude_code"] or "")
    if not findings:
        print("  none")
    else:
        total = sum(len(h) for _, h in findings)
        print(f"  {total} line(s) across {len(findings)} file(s) "
              f"reference retired systems:")
        for fpath, hits in findings:
            print(f"  - {fpath}")
            for lineno, text in hits[:6]:
                print(f"      {lineno}: {text.strip()[:100]}")
            if len(hits) > 6:
                print(f"      … +{len(hits) - 6} more")
        if apply_cleanup:
            print("\n  --apply-cleanup requested: rewriting is not yet "
                  "implemented (deferred to P4 hand-review). No files changed.")
        else:
            print("\n  (detection only — rewrite gated behind --apply-cleanup)")

    print(f"\nstore: {store}")
    print(f"mode: {'DRY-RUN' if dry_run else 'APPLY'}")


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
    findings = scan_stale(reg, harnesses["claude_code"] or "")
    total = sum(len(h) for _, h in findings)
    print(f"  stale refs: {total} line(s) across {len(findings)} file(s)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--status", action="store_true",
                    help="print wiring + drift, change nothing")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, write nothing")
    ap.add_argument("--apply-cleanup", action="store_true",
                    help="(deferred) also rewrite stale references")
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
        cmd_run(store, args.dry_run, args.apply_cleanup)


if __name__ == "__main__":
    main()

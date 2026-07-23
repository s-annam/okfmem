#!/usr/bin/env python3
"""okfmem — uninstall wrapper (harness unwiring only; idempotent).

Mirrors memory_init.py's five wiring steps in reverse: strips everything
okfmem's own installer/init created, and nothing else.

Does four things:

  1. Remove the managed MEMORY-POINTER block from each detected harness's
     global file (~/.claude/CLAUDE.md, ~/.gemini/config/AGENTS.md) -- the
     inverse of memory_init.upsert_pointer. Surrounding content is untouched.
  2. Remove the engine's skill links/junctions/copies from each harness's
     skill dir -- the inverse of memory_init.link_skills. A real (unmanaged)
     directory at that path is NEVER removed.
  3. Remove the per-project memory links registered in the store's
     registry.json -- the inverse of memory_init.link_project_memory. Same
     managed-only guard.
  4. Remove okfmem's Stop (consolidation) and SessionStart (store-pull) hook
     entries from ~/.claude/settings.json, leaving every other hook (and any
     legacy/unrelated pull hook) untouched.

This module unwires ONLY -- it never deletes the store or touches its git
remote. Those are outward/destructive ops and are gated (rung-2 / rung-3
confirmation) in the native uninstall.sh / uninstall.ps1 scripts that call
this module, not here. Every function here is idempotent: a second run over
already-unwired state reports "absent"/"no-claude" and changes nothing.

Called directly by the native uninstall scripts (not through the `okfmem`
CLI dispatcher), the same way memory_backfill.py and memory_init.py are:
`python3 <engine>/memory_uninstall.py [--dry-run] [--store PATH]`. Run this
way, sys.path[0] is the engine dir, so `import memory_init` resolves without
needing this repo installed as a package (see tests/conftest.py for the same
pattern used by the test suite).

Store location: --store, else $OKFMEM_STORE, else ~/okfmem-store.
"""
import argparse
import json
import os
import re
import shutil

import memory_init as mi

# ---------------------------------------------------------------------------
# 1. Pointer removal -- inverse of memory_init.upsert_pointer
# ---------------------------------------------------------------------------
# The managed block, greedily eating the blank line(s) immediately around it --
# the separator upsert_pointer inserted before the block and the trailing
# "\n" it appended. Anchored to the block, so ONLY that seam is normalized;
# blank-line runs elsewhere in the user's file are never touched.
_SEAM_RE = re.compile(
    r"\n*" + re.escape(mi.MARKER_OPEN) + r".*?" + re.escape(mi.MARKER_CLOSE)
    + r"\n*", re.DOTALL
)


def remove_pointer(path, dry_run):
    """Remove the managed MEMORY-POINTER block from `path`, if present.

    Returns 'removed' / 'absent' (file exists, no marker) / 'no-file'. Only
    the managed block and the blank-line seam upsert_pointer left around it
    are touched -- every other line in the file, INCLUDING the user's own
    blank-line runs elsewhere, survives byte-for-byte.
    """
    if not os.path.exists(path):
        return "no-file"
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    m = _SEAM_RE.search(text)
    if not m:
        return "absent"
    # Rejoin the content on either side of the removed seam: nothing if the
    # block sat at a file boundary, otherwise a single blank line so adjacent
    # user sections stay separated. The seam's whitespace was okfmem's own
    # (see upsert_pointer); collapsing it here is not editing user content.
    before, after = text[:m.start()], text[m.end():]
    joiner = "\n\n" if (before and after) else ""
    new_text = before + joiner + after
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_text)
    return "removed"


# ---------------------------------------------------------------------------
# Shared managed-link teardown -- one link at a time, three tiers deep
# (symlink / junction / tier-3 managed copy), mirroring _make_link's tiers
# and link_skills' / link_project_memory's repoint teardown exactly.
# ---------------------------------------------------------------------------
def _teardown_link(link, dry_run):
    """If `link` is something okfmem created (symlink, junction, or a tier-3
    managed copy carrying MANAGED_COPY_MARKER), remove it and return the
    action string ('removed' or 'removed (junction)'/'removed (copy)').

    If nothing exists at `link`, return 'absent'. If something exists but
    isn't ours, return 'skip (real file)' and never touch it.
    """
    if not os.path.lexists(link):
        return "absent"

    is_symlink = os.path.islink(link)
    is_junction = mi._is_junction(link)
    is_managed_copy = mi._managed_copy_target(link) is not None

    if not (is_symlink or is_junction or is_managed_copy):
        return "skip (real file)"

    if is_junction:
        tier_note = " (junction)"
    elif not is_symlink:
        tier_note = " (copy)"
    else:
        tier_note = ""

    if not dry_run:
        if is_junction:
            # Unlink the reparse point itself, not the target it points at.
            os.rmdir(link)
        elif os.path.isdir(link) and not is_symlink:
            shutil.rmtree(link)  # our tier-3 real copy
        else:
            os.remove(link)  # symlink

    return f"removed{tier_note}"


# ---------------------------------------------------------------------------
# 2. Skill links -- inverse of memory_init.link_skills
# ---------------------------------------------------------------------------
def unlink_skills(dry_run):
    """Remove the engine's skill links/junctions/copies from every detected
    harness skill dir. Never removes a real (unmanaged) directory or file at
    that path. Returns a list of (harness, name, action) tuples."""
    engine = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skills")
    actions = []
    if not os.path.isdir(engine):
        return actions
    names = sorted(n for n in os.listdir(engine)
                   if os.path.isfile(os.path.join(engine, n, "SKILL.md")))
    for harness, dest in mi.skill_dirs().items():
        for name in names:
            link = os.path.join(dest, name)
            actions.append((harness, name, _teardown_link(link, dry_run)))
    return actions


# ---------------------------------------------------------------------------
# 3. Project memory links -- inverse of memory_init.link_project_memory
# ---------------------------------------------------------------------------
def unlink_project_memory(store, claude_projects, reg, dry_run):
    """Remove the per-project memory link for every root registered in
    `reg["map"]`. Never removes a real (unmanaged) directory. A foreign
    (other-machine) root simply resolves to a link path that doesn't exist
    here and reports 'absent'. Returns a list of (root, project, action)."""
    actions = []
    for root, project in sorted(reg.get("map", {}).items()):
        link = os.path.join(claude_projects, mi.encode_root(root), "memory")
        actions.append((root, project, _teardown_link(link, dry_run)))
    return actions


# ---------------------------------------------------------------------------
# 4a. Stop hook -- inverse of memory_init.wire_stop_hook
# ---------------------------------------------------------------------------
def _load_settings(claude_dir):
    """Return (data, settings_path, early_result) where early_result is a
    ready-to-return (action, path) tuple if the caller should stop now, else
    None. Shared by unwire_stop_hook / unwire_pull_hook so both guard
    settings.json identically."""
    if not os.path.isdir(claude_dir):
        return None, None, ("no-claude", None)
    settings = os.path.join(claude_dir, "settings.json")
    if not os.path.exists(settings):
        return None, settings, ("absent", settings)
    try:
        with open(settings, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None, settings, (
            "skip (settings.json unreadable — unwire manually)", settings)
    if not isinstance(data, dict):
        return None, settings, (
            "skip (settings.json not an object — unwire manually)", settings)
    return data, settings, None


def _strip_hooks(groups, matches):
    """Drop hook dicts from `groups` (a hooks.<Event> list) whose command
    satisfies `matches(command)`, pruning any group left with an empty
    `hooks` list. Returns (new_groups, changed)."""
    changed = False
    new_groups = []
    for group in groups:
        if not isinstance(group, dict):
            new_groups.append(group)
            continue
        hlist = group.get("hooks", [])
        if not isinstance(hlist, list):
            new_groups.append(group)
            continue
        kept = [h for h in hlist
                if not (isinstance(h, dict) and matches(h.get("command", "")))]
        if len(kept) != len(hlist):
            changed = True
        if kept:
            new_group = dict(group)
            new_group["hooks"] = kept
            new_groups.append(new_group)
        # else: the group is now empty -- drop it entirely (pruned).
    return new_groups, changed


def unwire_stop_hook(dry_run):
    """Remove any Stop hook entry that invokes memory_consolidate.py.

    Returns (action, path):
      'removed'    — dropped one or more matching entries
      'absent'     — settings.json exists but nothing of ours is wired
      'no-claude'  — ~/.claude missing; nothing to unwire
      'skip (...)' — settings.json unreadable/wrong shape; left untouched

    Every other hook (Stop or otherwise) is preserved verbatim.
    """
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    data, settings, early = _load_settings(claude_dir)
    if early is not None:
        return early

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return ("absent", settings)
    stop = hooks.get("Stop")
    if not isinstance(stop, list):
        return ("absent", settings)

    new_stop, changed = _strip_hooks(
        stop, lambda cmd: "memory_consolidate.py" in cmd)
    if not changed:
        return ("absent", settings)

    hooks["Stop"] = new_stop
    if not dry_run:
        mi._write_settings_json(claude_dir, settings, data)
    return ("removed", settings)


# ---------------------------------------------------------------------------
# 4b. SessionStart pull hook -- inverse of memory_init.wire_pull_hook
# ---------------------------------------------------------------------------
def unwire_pull_hook(dry_run):
    """Remove any SessionStart hook entry that invokes okfmem's managed
    `okfmem pull` command. A legacy hand-added `git ... pull` hook (never
    healed by an `okfmem init` run) or any other unrelated SessionStart hook
    is NOT ours to remove and is left exactly as-is.

    Returns (action, path) with the same shape as unwire_stop_hook.
    """
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    data, settings, early = _load_settings(claude_dir)
    if early is not None:
        return early

    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return ("absent", settings)
    starts = hooks.get("SessionStart")
    if not isinstance(starts, list):
        return ("absent", settings)

    new_starts, changed = _strip_hooks(starts, mi._is_managed_pull_command)
    if not changed:
        return ("absent", settings)

    hooks["SessionStart"] = new_starts
    if not dry_run:
        mi._write_settings_json(claude_dir, settings, data)
    return ("removed", settings)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="Unwire okfmem's harness wiring on this machine (pointer "
                     "blocks, skill links, per-project memory links, Stop/"
                     "SessionStart hooks). Never touches the store's git "
                     "remote and never deletes data -- those steps live in "
                     "uninstall.sh / uninstall.ps1, gated separately.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan, remove nothing")
    ap.add_argument("--store", default=os.environ.get(
        "OKFMEM_STORE", os.path.expanduser("~/okfmem-store")))
    args = ap.parse_args()

    store = os.path.abspath(os.path.expanduser(args.store))
    dry_run = args.dry_run
    home = os.path.expanduser("~")
    claude_projects = os.path.join(home, ".claude", "projects")
    # Unlike memory_init, do NOT hard-exit when <store>/projects is missing --
    # uninstall must still strip harness wiring even when the store is
    # gone/partial (e.g. the data delete already ran, or a store that was
    # never fully initialized).
    reg = mi._load_registry(os.path.join(store, "registry.json"))

    mode = "dry-run" if dry_run else "apply"
    print(mi._c("okfmem uninstall", "bold") + mi._c(f"  ·  {mode}", "dim") + "\n")

    changes = 0

    # --- 1. pointers ---------------------------------------------------
    harnesses = mi.detect_harnesses()
    pacts = [(n, remove_pointer(p, dry_run), p) for n, p in harnesses.items() if p]
    pchg = [a for a in pacts if a[1] == "removed"]
    changes += len(pchg)
    if pchg:
        print(f"{mi.glyph('chg')} pointers    "
              f"{len(pchg)} to remove ({len(pacts)} harness globals)")
    else:
        print(f"{mi.glyph('ok')} pointers    none present ({len(pacts)} harness globals)")
    for name, action, path in pacts:
        k = "chg" if action == "removed" else "ok"
        print(f"    {mi.glyph(k)} {name:12} {action:10} {mi._short(path)}")

    # --- 2. skill links --------------------------------------------------
    sk = unlink_skills(dry_run)
    sk_removed = [a for a in sk if a[2].startswith("removed")]
    changes += len(sk_removed)
    if not sk:
        print(f"{mi.glyph('ok')} skills      none to remove (no harness skill dirs)")
    elif sk_removed:
        hnames = ", ".join(sorted({a[0] for a in sk_removed}))
        print(f"{mi.glyph('chg')} skills      "
              f"{len(sk_removed)} to remove from {hnames}")
    else:
        print(f"{mi.glyph('ok')} skills      none linked by okfmem")
    for harness, name, action in sk:
        if action != "absent":
            k = "chg" if action.startswith("removed") else "warn"
            print(f"    {mi.glyph(k)} {harness:12} {action:16} {name}")

    # --- 3. project memory links -----------------------------------------
    pm = unlink_project_memory(store, claude_projects, reg, dry_run)
    pm_removed = [a for a in pm if a[2].startswith("removed")]
    changes += len(pm_removed)
    if not pm:
        print(f"{mi.glyph('ok')} memory link none registered")
    elif pm_removed:
        print(f"{mi.glyph('chg')} memory link "
              f"{len(pm_removed)} to remove ({len(pm)} registered)")
    else:
        print(f"{mi.glyph('ok')} memory link none to remove ({len(pm)} registered)")
    for root, project, action in pm:
        if action != "absent":
            k = "chg" if action.startswith("removed") else "warn"
            print(f"    {mi.glyph(k)} {project:12} {action:16} {mi._short(root)}")

    # --- 4. Stop hook -------------------------------------------------
    haction, hpath = unwire_stop_hook(dry_run)
    if haction == "removed":
        changes += 1
        verb = "would remove" if dry_run else "removed"
        print(f"{mi.glyph('chg')} stop hook   {verb} consolidation hook "
              f"({mi._short(hpath)})")
    elif haction == "absent":
        print(f"{mi.glyph('ok')} stop hook   not wired")
    elif haction == "no-claude":
        print(f"{mi.glyph('ok')} stop hook   no Claude Code dir — skipped")
    else:
        print(f"{mi.glyph('warn')} stop hook   {haction}")

    # --- 5. SessionStart pull hook ----------------------------------------
    paction, ppath = unwire_pull_hook(dry_run)
    if paction == "removed":
        changes += 1
        verb = "would remove" if dry_run else "removed"
        print(f"{mi.glyph('chg')} pull hook   {verb} store-pull hook "
              f"({mi._short(ppath)})")
    elif paction == "absent":
        print(f"{mi.glyph('ok')} pull hook   not wired")
    elif paction == "no-claude":
        print(f"{mi.glyph('ok')} pull hook   no Claude Code dir — skipped")
    else:
        print(f"{mi.glyph('warn')} pull hook   {paction}")

    print()
    tail = mi._c(f"({mi._short(store)} · {mode})", "dim")
    if changes == 0:
        print(f"{mi.glyph('ok')} {mi._c('fully unwired', 'bold')} — nothing to do  {tail}")
    else:
        verb = "would remove" if dry_run else "removed"
        print(f"{mi.glyph('chg')} {mi._c(f'{changes} item(s) {verb}', 'bold')}  {tail}")
        if dry_run:
            print(mi._c("    re-run without --dry-run to apply", "dim"))


if __name__ == "__main__":
    main()

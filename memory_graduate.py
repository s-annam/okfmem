#!/usr/bin/env python3
"""okfmem graduate — promote a durable memory page into CLAUDE.md/AGENTS.md,
archiving the source (issue #10).

Mechanizes the forward promotion that `okfmem-curate`'s "duplicates with
CLAUDE.md" bucket only detects reactively (and, worse, recommends deleting):
a page that has matured into a house rule every session should see gets
distilled into the target `CLAUDE.md`, mirrored into a real (non-symlinked)
sibling `AGENTS.md`, and the source page is archived (never deleted) with a
`graduated_to:` breadcrumb so a later curate pass never re-flags or
hard-deletes it.

Usage:
  okfmem graduate <slug> [--to PATH] [--heading TEXT] [--project NAME]
                 [--pr REF] [--store PATH] [--yes] [--dry-run]

  <slug>       page name (without .md) under <store>/projects/<project>/
  --to         target CLAUDE.md path (default: <repo-root>/CLAUDE.md).
               A lane-scoped file that doesn't exist yet is created, seeded
               with a house header + a "read the root first" pointer.
  --heading    insert the distilled rule under this `## heading` (created if
               absent); default is the page's own title (frontmatter
               `description` if present, else its H1, else the slug).
  --project    store project name (default: cwd's git-root, registry-mapped —
               the same rule `okfmem init` uses).
  --pr         short PR/date reference stamped into the archived page's
               `graduated_to:` field.

This writes OUTSIDE the store (into the target repo's CLAUDE.md/AGENTS.md),
so it is a rung-2 confirmation-ladder op (see CLAUDE.md "Confirmation
discipline"): gated behind a `[y/N]` prompt, skippable non-interactively
(prints the exact manual command), bypassed entirely by `--dry-run`. The
archive-move is reversible, so `[y/N]` is the right rung, not a typed
confirmation.
"""
import argparse
import difflib
import os
import re
import sys
from datetime import datetime, timezone

# Engine modules live beside this file (repo root), same resolution the
# `okfmem` dispatcher's runpy invocation and sibling modules already use.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from memory_consolidate import (  # noqa: E402
    _project_dir_of,
    archive_page,
    drop_memory_lines,
    find_frontmatter,
    parse_fields,
    update_fields,
)
from memory_init import _current_git_root, _load_registry, _prompt_yes_no  # noqa: E402

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))

H1_RE = re.compile(r"^#\s+(.*?)\s*$", re.MULTILINE)
HEADING_RE = re.compile(r"^##[ \t]+", re.MULTILINE)

SEED_HEADER = (
    "# CLAUDE.md\n\n"
    "Guidance for Claude Code (and other agents) working in this directory.\n\n"
    "> Read the root `CLAUDE.md` first — this file adds directory-specific "
    "rules only.\n"
)


class GraduateError(Exception):
    pass


# ---------------------------------------------------------------------------
# project / page resolution
# ---------------------------------------------------------------------------
def resolve_project(store, explicit):
    """--project if given; else the same `basename(git-root) unless
    overridden` rule `okfmem init` uses to name the current repo's store dir."""
    if explicit:
        return explicit
    root = _current_git_root()
    if not root:
        return None
    reg = _load_registry(os.path.join(store, "registry.json"))
    return reg.get("overrides", {}).get(root, os.path.basename(root))


def page_title(text, slug):
    """Best available human title: frontmatter `description`, else the H1,
    else a titleized slug."""
    fm = find_frontmatter(text)
    if fm:
        desc = parse_fields(text, fm).get("description")
        if desc:
            return desc
    m = H1_RE.search(text)
    if m:
        return m.group(1)
    return slug.replace("-", " ").replace("_", " ").title()


def distilled_body(text):
    """Page markdown minus frontmatter and a leading H1 — the insertion is
    meant to read as a rule, not carry a memory-page header."""
    fm = find_frontmatter(text)
    body = text[fm[2]:] if fm else text
    body = body.lstrip("\n")
    m = H1_RE.match(body)
    if m:
        body = body[m.end():].lstrip("\n")
    body = body.rstrip("\n")
    if not body:
        raise GraduateError("page has no body to distill")
    return body + "\n"


def slugify_heading(heading):
    return re.sub(r"[^a-z0-9]+", "-", heading.lower()).strip("-")


# ---------------------------------------------------------------------------
# markdown insertion (pure string transform — testable without touching disk)
# ---------------------------------------------------------------------------
def insert_section(text, heading, addition):
    """Insert `addition` under `## heading`, splicing into an existing
    section (before the next `## ` heading, or EOF) if the heading already
    exists; otherwise append a brand-new `## heading` section at EOF."""
    addition = addition.rstrip("\n")
    heading_re = re.compile(
        r"^##[ \t]+" + re.escape(heading) + r"[ \t]*$", re.MULTILINE)
    m = heading_re.search(text)
    if m is None:
        sep = ("" if text == "" or text.endswith("\n\n")
               else ("\n" if text.endswith("\n") else "\n\n"))
        return text + sep + f"## {heading}\n\n{addition}\n"
    next_m = HEADING_RE.search(text, m.end())
    insert_at = next_m.start() if next_m else len(text)
    before, after = text[:insert_at], text[insert_at:]
    before = before.rstrip("\n") + "\n\n"
    after = after.lstrip("\n")
    return before + addition + "\n\n" + after


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------
def build_plan(store, slug, project, to, heading, pr_ref):
    proj_dir = os.path.join(store, "projects", project)
    # Path-traversal guard: <slug> is an attacker-influenceable positional that
    # gets joined into a filesystem path whose archive step calls os.remove().
    # Reject separators and `..` up front for a clean early error, then assert
    # the resolved path is still contained under proj_dir (commonpath is
    # path-component aware, so it dodges the prefix-substring pitfall a bare
    # str.startswith would fall into).
    if (os.sep in slug or (os.altsep and os.altsep in slug)
            or "/" in slug or ".." in slug):
        raise GraduateError(
            f"invalid slug (path separators and '..' are not allowed): {slug!r}")
    src = os.path.join(proj_dir, f"{slug}.md")
    real_proj = os.path.realpath(proj_dir)
    real_src = os.path.realpath(src)
    if os.path.commonpath([real_src, real_proj]) != real_proj:
        raise GraduateError(
            f"invalid slug (resolves outside the project dir): {slug!r}")
    if not os.path.isfile(src):
        raise GraduateError(f"no such page: {src}")
    with open(src, "r", encoding="utf-8", newline="") as f:
        src_text = f.read()
    # A page with no YAML frontmatter cannot be graduated: the archive step
    # stamps `status`/`graduated_to` via update_fields, which unpacks the
    # frontmatter tuple and would raise a bare TypeError on None mid-apply
    # (after archive_page has already moved the file). Refuse up front —
    # BEFORE any write — for a clean error, mirroring consolidate's own
    # `if not fm: continue` guard.
    if not find_frontmatter(src_text):
        raise GraduateError(
            f"page {slug} has no frontmatter; cannot graduate")

    title = page_title(src_text, slug)
    section_heading = heading or title
    body = distilled_body(src_text)

    target_path = os.path.abspath(os.path.expanduser(to))
    target_dir = os.path.dirname(target_path)
    target_before = SEED_HEADER
    target_existed = os.path.isfile(target_path)
    if target_existed:
        with open(target_path, "r", encoding="utf-8") as f:
            target_before = f.read()
    target_after = insert_section(target_before, section_heading, body)

    agents_path = os.path.join(target_dir, "AGENTS.md")
    agents_before = agents_after = None
    if (os.path.islink(agents_path)
            and os.path.realpath(agents_path) == os.path.realpath(target_path)):
        # Symlink-aware (the load-bearing bit): detect by [ -L ] + readlink
        # *target identity*, never by content compare. Only a symlink that
        # genuinely resolves to the CLAUDE.md we're editing is a no-op —
        # editing CLAUDE.md already covers it. A symlink pointing elsewhere is
        # NOT covered, so it falls through to real-file mirroring below.
        agents_action = "symlink-noop"
    elif os.path.isfile(agents_path):
        # Real file, or a symlink to some OTHER file: mirror the rule in.
        # (Writing through a foreign symlink lands the rule in whatever file it
        # resolves to — the point is the rule reaches an out-of-band AGENTS.md.)
        with open(agents_path, "r", encoding="utf-8") as f:
            agents_before = f.read()
        agents_after = insert_section(agents_before, section_heading, body)
        agents_action = "mirror"
    else:
        agents_action = "absent"  # leave absent — never invent one

    return {
        "slug": slug,
        "project": project,
        "src": src,
        "src_text": src_text,
        "heading": section_heading,
        "target_path": target_path,
        "target_existed": target_existed,
        "target_before": target_before,
        "target_after": target_after,
        "agents_path": agents_path,
        "agents_action": agents_action,
        "agents_before": agents_before,
        "agents_after": agents_after,
        "pr_ref": pr_ref,
    }


def _diff(before, after, label):
    return "".join(difflib.unified_diff(
        before.splitlines(keepends=True), after.splitlines(keepends=True),
        fromfile=label, tofile=label))


def render_plan(plan):
    lines = [f"okfmem graduate {plan['slug']} ({plan['project']}) "
             f"-> {plan['target_path']}", ""]
    diff = _diff(plan["target_before"], plan["target_after"], plan["target_path"])
    lines.append(diff if diff else "(no CLAUDE.md diff)")
    if plan["agents_action"] == "mirror":
        lines.append("")
        lines.append(_diff(plan["agents_before"], plan["agents_after"],
                           plan["agents_path"]))
    elif plan["agents_action"] == "symlink-noop":
        lines.append("")
        lines.append("AGENTS.md: symlink -> CLAUDE.md — already covered, "
                     "no separate edit")
    elif plan["agents_action"] == "absent":
        lines.append("")
        lines.append("AGENTS.md: absent — leaving absent")
    dest = os.path.join(_project_dir_of(plan["src"]), "archive",
                        os.path.basename(plan["src"]))
    lines.append("")
    lines.append(f"archive:  {plan['src']}")
    lines.append(f"      ->  {dest}  (MEMORY.md pointer dropped)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------
def apply_plan(plan, store, today):
    # Ordering rationale (crash-safety): there is no true cross-file
    # transaction between the store (git-backed pages) and an arbitrary target
    # repo's CLAUDE.md, so we sequence the work and bracket BOTH phases in
    # best-effort rollback. We do the reversible, store-internal side (archive +
    # stamp graduated_to + drop MEMORY.md pointer) FIRST, then the outward
    # CLAUDE.md/AGENTS.md writes. If ANY step in EITHER phase throws — including
    # a mid-archive `os.remove(src)` failure (Windows AV/lock) that would
    # otherwise leave the source live AND a half-stamped archive copy — we
    # best-effort roll every completed step back and re-raise. Guarantee: a
    # failed apply leaves the world as if graduate never ran; in particular no
    # split-brain (rule in both CLAUDE.md and the page, or source live beside a
    # duplicate archive copy) survives a throw.
    proj_dir = _project_dir_of(plan["src"])
    memory_path = os.path.join(proj_dir, "MEMORY.md")
    memory_before = None
    if os.path.isfile(memory_path):
        with open(memory_path, "r", encoding="utf-8", newline="") as f:
            memory_before = f.read()

    # The archive destination is deterministic (same path archive_page would
    # return), computed up front so the internal-phase rollback can clean up a
    # copy even when archive_page throws AFTER writing dest but during/after the
    # os.remove(src) that would normally hand dest back.
    dest = os.path.join(proj_dir, "archive", os.path.basename(plan["src"]))

    # --- reversible, store-internal side (rolled back on failure) ---
    try:
        # Archive, never delete — reuses P3 consolidation's own move+stamp path.
        cand = {"path": plan["src"], "text": plan["src_text"]}
        dest = archive_page(cand, today, dry_run=False)

        repo_root = _current_git_root() or os.path.dirname(plan["target_path"])
        rel_target = os.path.relpath(plan["target_path"], repo_root)
        anchor = slugify_heading(plan["heading"])
        graduated_to = f"{rel_target}#{anchor}"
        if plan["pr_ref"]:
            graduated_to += f" (via {plan['pr_ref']}, {today.isoformat()})"
        else:
            graduated_to += f" ({today.isoformat()})"

        with open(dest, "r", encoding="utf-8", newline="") as f:
            dest_text = f.read()
        fm = find_frontmatter(dest_text)
        dest_text = update_fields(dest_text, fm, {"graduated_to": graduated_to})
        with open(dest, "w", encoding="utf-8", newline="") as f:
            f.write(dest_text)

        dropped = drop_memory_lines(memory_path, [plan["slug"]], dry_run=False)
    except Exception:
        _rollback_internal(plan, dest, memory_path, memory_before)
        raise

    # --- outward, non-atomic side (rolled back on failure) ---
    target_written = False
    try:
        os.makedirs(os.path.dirname(plan["target_path"]), exist_ok=True)
        with open(plan["target_path"], "w", encoding="utf-8") as f:
            f.write(plan["target_after"])
        target_written = True
        if plan["agents_action"] == "mirror":
            with open(plan["agents_path"], "w", encoding="utf-8") as f:
                f.write(plan["agents_after"])
    except Exception:
        _rollback_apply(plan, dest, memory_path, memory_before, target_written)
        raise

    return dest, dropped


def _rollback_internal(plan, dest, memory_path, memory_before):
    """Best-effort undo of the store-internal phase: drop the archived copy,
    restore the live source page, and put MEMORY.md back. Remove the archive
    copy FIRST, then restore the source — so a partial rollback can never leave
    BOTH (the source-live-beside-a-duplicate-archive split-brain we most want to
    avoid). Each undo step is guarded independently; a failure in one must not
    skip the others or mask the original exception about to be re-raised."""
    try:
        if os.path.isfile(dest):
            os.remove(dest)
    except Exception:
        pass
    try:
        with open(plan["src"], "w", encoding="utf-8", newline="") as f:
            f.write(plan["src_text"])
    except Exception:
        pass
    try:
        if memory_before is not None:
            with open(memory_path, "w", encoding="utf-8", newline="") as f:
                f.write(memory_before)
    except Exception:
        pass


def _rollback_apply(plan, dest, memory_path, memory_before, target_written):
    """Best-effort undo of a partially-applied graduate: restore the live
    source page, drop the archived copy, put MEMORY.md back, and revert any
    outward CLAUDE.md/AGENTS.md write already made — so a failed apply leaves
    the world as if graduate never ran (no split-brain, no data loss). Each
    step is guarded independently; a rollback failure must not mask the
    original error that is about to be re-raised."""
    # Store-internal: un-archive the source.
    _rollback_internal(plan, dest, memory_path, memory_before)
    # Outward: revert CLAUDE.md (and a mirrored AGENTS.md) if we managed to
    # write it before failing. The mirror only runs after CLAUDE.md succeeds,
    # so it's only in play once target_written is True.
    if target_written:
        try:
            if plan["target_existed"]:
                with open(plan["target_path"], "w", encoding="utf-8") as f:
                    f.write(plan["target_before"])
            elif os.path.isfile(plan["target_path"]):
                os.remove(plan["target_path"])
        except Exception:
            pass
        if plan["agents_action"] == "mirror" and plan["agents_before"] is not None:
            try:
                with open(plan["agents_path"], "w", encoding="utf-8") as f:
                    f.write(plan["agents_before"])
            except Exception:
                pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def cmd_graduate(args):
    store = os.path.abspath(os.path.expanduser(args.store))
    project = resolve_project(store, args.project)
    if not project:
        print("okfmem graduate: not inside a git repo and no --project given",
              file=sys.stderr)
        return 2

    to = args.to or os.path.join(_current_git_root() or os.getcwd(), "CLAUDE.md")

    try:
        plan = build_plan(store, args.slug, project, to, args.heading, args.pr)
    except GraduateError as e:
        print(f"okfmem graduate: {e}", file=sys.stderr)
        return 2

    print(render_plan(plan))

    if args.dry_run:
        print("\nmode: DRY-RUN — nothing written")
        return 0

    manual = (f"okfmem graduate {args.slug} --to {plan['target_path']} "
             f"--project {project} --store {store} --yes")
    non_interactive = not sys.stdin.isatty()
    proceed = _prompt_yes_no(
        f"\nWrite into {plan['target_path']} and archive {args.slug}.md?",
        assume_yes=args.yes, non_interactive=non_interactive,
        manual_hint=f"Apply later with: {manual}")
    if not proceed:
        return 0

    today = datetime.now(timezone.utc).date()
    dest, dropped = apply_plan(plan, store, today)
    print(f"\narchived: {dest}")
    print(f"MEMORY.md lines dropped: {dropped}")
    return 0


def main():
    p = argparse.ArgumentParser(
        prog="okfmem graduate", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("slug", help="page slug (without .md) to graduate")
    p.add_argument("--to", help="target CLAUDE.md path "
                              "(default: <repo-root>/CLAUDE.md)")
    p.add_argument("--heading", help="insert under this `## heading` "
                                    "(default: the page's own title)")
    p.add_argument("--project", help="store project name "
                                    "(default: cwd's git-root, registry-mapped)")
    p.add_argument("--pr", help="PR/date reference stamped into graduated_to")
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--yes", action="store_true",
                   help="skip the [y/N] confirmation")
    p.add_argument("--dry-run", action="store_true",
                   help="print the planned diff + archive move, write nothing")
    args = p.parse_args()
    sys.exit(cmd_graduate(args) or 0)


if __name__ == "__main__":
    main()

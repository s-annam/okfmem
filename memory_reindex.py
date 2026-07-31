#!/usr/bin/env python3
"""okfmem reindex -- deterministic reindex engine for a memory store project
(issue #54).

Three read-only modes, no writes to the store (rung 1 on the confirmation
ladder -- see CLAUDE.md "Confirmation discipline"):

  okfmem reindex --report [TARGET]        # where the auto-loaded bytes sit
  okfmem reindex --verify [TARGET]        # link integrity across MEMORY*.md
  okfmem reindex --budget-check [TARGET]  # pointer lines over the char budget

`--report` leads with the only two files that reach the model at session start
(`MEMORY.md` and `STATE.md`) measured against their byte ceiling, then breaks
`MEMORY.md` down **per section** -- a single dominant block is the finding that
decides whether the remedy is "tighten a few hooks" or "split a lane", and total
file size alone never shows it. Pages on disk are reported too, explicitly
labelled non-context: they cost zero at session start.

`--verify` walks **every** `MEMORY*.md`, accepts **both** pointer syntaxes, and
reports dangling pointers *named with the index they came from* plus pages in no
index at all. It exits non-zero when either is found, so it can gate a reindex.

`--budget-check` counts pointer lines over the per-line character budget (#52)
across every index -- the after-the-fact check for what slipped past
`okfmem-save`'s write-time enforcement. Characters, never bytes: the pointer
convention's em-dash is one character and three bytes, so a byte count
over-reports every line that carries one. Advisory, so it always exits 0.

Both syntaxes are first-class. The bare form is not legacy -- it is what a
graph-era lane index uses today:

    - [Human title](some-slug.md) -- hook      # rich
    - some-slug.md -- hook                     # bare
    - one-slug.md + two-slug.md -- hook        # bare, several under one hook

The parsing is anchored on the **link target**, never on a loose `- \\[?` prefix,
and it is anchored at both ends of the line:

  * a rich pointer whose *title text* starts with a filename
    (`- [CLAUDE.md subdir lanes](real-slug.md)`) resolves to `real-slug.md`, not
    to a phantom `CLAUDE.md`;
  * a filename named in the *hook* (`- real-slug.md -- MEMORY.md is the only
    file loaded`) is prose, not a pointer.

This lives in Python rather than shell precisely because the shell version of
the same check fails open on BSD `sed` (macOS).

Usage:
  okfmem reindex [--report] [--verify] [--budget-check] [TARGET]
                 [--project NAME] [--store PATH] [--json] [--budget BYTES]

  TARGET      memory dir to inspect. Default: <store>/projects/<project>/,
              with <project> from --project, else the cwd's git-root name
              (the same rule `okfmem init`/`graduate` use).
  --json      machine-readable output instead of markdown (stable contract for
              callers such as the /okfmem-reindex skill).
  --budget    override the auto-load byte ceiling for this run.

Exit codes:
  0  clean (or --report/--budget-check, which never fail on content)
  1  --verify found dangling pointers and/or orphans
  2  usage error / target directory not found

Pure stdlib, cross-platform, read-only.
"""
import argparse
import glob
import json
import os
import re
import sys
from collections import namedtuple

DEFAULT_STORE = os.environ.get("OKFMEM_STORE", os.path.expanduser("~/okfmem-store"))

# Index files: the root index plus any lane index split out of it.
INDEX_PREFIX = "MEMORY"
ROOT_INDEX = "MEMORY.md"
# Files that live in a project dir but are neither an index nor a durable page,
# so they are never orphan candidates. Same set the backfill/consolidate passes
# skip.
NON_PAGE_NAMES = {"STATE.md", "CONTEXT.md"}

# Retired `ck_*.md` session snapshots. Every sibling pass already skips them by
# this exact prefix test (`memory_consolidate.scan_project`,
# `memory_backfill`), and curate flags them `ck_snapshot` cruft rather than
# durable pages -- so they are not orphan candidates here either. Counting them
# would make `--verify` fail on most real stores for a known-benign reason,
# and a gate nobody can turn on is not a gate.
CK_PREFIX = "ck_"

# The only two files a harness auto-loads at session start, so the only two that
# cost context. Named here rather than as literals so the ceiling has one home
# (issue #53's byte-based restructure trigger reads from here too). 8 KiB is
# roughly 2k tokens per file -- a routing table, not a page list.
MEMORY_BUDGET_BYTES = 8192
STATE_BUDGET_BYTES = 8192

# Per-line pointer budget (#52), in CHARACTERS. `okfmem-save` enforces it at
# write time; `--budget-check` is the after-the-fact sweep for what slipped
# through. One home for the number, so the skills can cite it instead of
# re-typing 150 into a shell one-liner that then drifts.
POINTER_BUDGET_CHARS = 150

# --- pointer syntax ---------------------------------------------------------
# Rich form. Captured loosely to the first `)` and normalized below: a slug
# never contains a paren, so the loose capture survives <>-wrapping, a
# `#fragment` and a link title without a regex that guesses at all three.
INLINE_LINK_RE = re.compile(r"\]\(([^)\n]*)\)")

# Bare form: `- <slug>.md -- hook`. Anchored so the filename must be the FIRST
# token of the list item. `[` is deliberately not a legal first character, which
# is what stops `- [CLAUDE.md subdir lanes](real-slug.md)` from being read as a
# bare pointer to `CLAUDE.md` -- the false positive a greedy `- \[?` prefix
# match produces.
BARE_POINTER_RE = re.compile(
    r"^[ \t]*[-*+][ \t]+([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?=[ \t]|$)"
)

# One list item may carry several slugs under a shared hook:
# `- slug-a.md + slug-b.md - hook`. Only `+` joins them, and only in the
# unbroken run at the head of the item -- deliberately NOT "any *.md token on
# the line". A hook routinely names a file in prose
# (`- index-is-the-only-loaded-file.md - MEMORY.md is the ONLY file loaded`),
# and reading that as a pointer is the same anchor-on-the-target failure as the
# `- \[?` prefix bug, just at the other end of the line.
BARE_CONTINUATION_RE = re.compile(
    r"^[ \t]*\+[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*\.md)(?=[ \t]|$)"
)

# Reference-style link definition: `[label]: slug.md`. Spec-legal markdown, so
# a page reachable only this way must not read as an orphan.
REF_DEF_RE = re.compile(r"^[ \t]{0,3}\[[^\]]+\]:[ \t]*(.+)$")

FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*$")

# target = same-dir `.md` filename; syntax = bare|inline|external; line = 1-based
Pointer = namedtuple("Pointer", "target syntax line")
Section = namedtuple("Section", "heading level bytes pointers")
Dangling = namedtuple("Dangling", "index target line syntax")


class ReindexError(Exception):
    pass


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------
def read_lines(path):
    """Return ``(lines, costs)`` for a file.

    ``lines`` are decoded, newline-stripped source lines; ``costs[i]`` is the
    exact byte cost of line ``i`` including its terminator, so ``sum(costs)``
    equals the file size on disk. Splitting on b"\\n" (rather than
    ``str.splitlines``) keeps the two lists index-aligned -- ``splitlines``
    also breaks on \\x0b/\\x0c/U+2028, which would silently desync a pointer's
    line number from the byte accounting.
    """
    with open(path, "rb") as f:
        raw = f.read()
    segs = raw.split(b"\n")
    costs = [len(s) + 1 for s in segs]
    if costs:
        costs[-1] -= 1  # the last segment has no trailing newline
    lines = [s.decode("utf-8", "replace").rstrip("\r") for s in segs]
    return lines, costs


def fence_mask(lines):
    """``True`` for every line inside a fenced code block, delimiters included.

    A pointer-shaped line in a code fence is documentation, not a pointer;
    counting it would manufacture a dangling link out of an example.
    """
    mask = [False] * len(lines)
    open_marker = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if open_marker is None:
            if m:
                open_marker = m.group(1)
                mask[i] = True
        else:
            mask[i] = True
            if (m and m.group(1)[0] == open_marker[0]
                    and len(m.group(1)) >= len(open_marker)):
                open_marker = None
    return mask


# ---------------------------------------------------------------------------
# pointer parsing
# ---------------------------------------------------------------------------
def classify_target(raw):
    """Normalize a raw inline-link target to ``(kind, value)``.

    kind is ``"page"`` for a same-directory ``.md`` filename, ``"external"``
    for a URL or a cross-directory path (real, but not resolvable against this
    memory dir, so never reported dangling), or ``None`` for anything that is
    not a page pointer at all (an image, a bare anchor, a non-md asset).
    """
    t = raw.strip()
    if t.startswith("<") and ">" in t:
        t = t[1:t.index(">")].strip()
    else:
        parts = t.split()
        t = parts[0] if parts else ""
    t = t.split("#", 1)[0]
    if not t.lower().endswith(".md"):
        return (None, "")
    if "://" in t or t.lower().startswith("mailto:"):
        return ("external", t)
    # An explicit same-dir link (`./slug.md`, `.\slug.md`) is a page pointer,
    # not a cross-directory one -- classifying it external would make the page
    # it names read as an orphan.
    if t.startswith("./"):
        t = t[2:]
    elif t.startswith(".\\"):
        t = t[2:]
    if "/" in t or "\\" in t:
        return ("external", t)
    return ("page", t)


def parse_pointers(lines):
    """Every pointer in one index file, in document order.

    Both syntaxes are first-class and a line may carry both (a bare pointer
    whose hook happens to contain a markdown link), so the two matchers run
    independently rather than as alternatives of one pattern.
    """
    out = []
    mask = fence_mask(lines)
    for i, line in enumerate(lines):
        if mask[i]:
            continue
        lineno = i + 1
        m = BARE_POINTER_RE.match(line)
        if m:
            out.append(Pointer(m.group(1), "bare", lineno))
            rest = line[m.end():]
            while True:
                cm = BARE_CONTINUATION_RE.match(rest)
                if not cm:
                    break
                out.append(Pointer(cm.group(1), "bare", lineno))
                rest = rest[cm.end():]
        rm = REF_DEF_RE.match(line)
        if rm:
            kind, val = classify_target(rm.group(1))
            if kind:
                out.append(Pointer(val, "inline" if kind == "page"
                                   else "external", lineno))
        for lm in INLINE_LINK_RE.finditer(line):
            kind, val = classify_target(lm.group(1))
            if kind == "page":
                out.append(Pointer(val, "inline", lineno))
            elif kind == "external":
                out.append(Pointer(val, "external", lineno))
    return out


def parse_pointers_text(text):
    """``parse_pointers`` over a string -- the shape tests and callers want."""
    return parse_pointers(text.split("\n"))


def parse_sections(lines, costs, pointers):
    """Partition a file into sections at every heading, with **own** bytes.

    Own bytes (heading line through the line before the next heading of *any*
    level) rather than subtree bytes, so the rows partition the file exactly:
    they sum to the file size and their percentages sum to 100. A subtree
    total would double-count a parent and hide which block is actually big --
    the one number this report exists to surface.
    """
    mask = fence_mask(lines)
    starts = []  # (line_index, heading_text, level)
    for i, line in enumerate(lines):
        if mask[i]:
            continue
        m = HEADING_RE.match(line)
        if m:
            starts.append((i, m.group(2), len(m.group(1))))

    bounds = []
    if not starts or starts[0][0] > 0:
        bounds.append((0, "(preamble)", 0))
    bounds.extend(starts)

    out = []
    for n, (start, heading, level) in enumerate(bounds):
        end = bounds[n + 1][0] if n + 1 < len(bounds) else len(lines)
        nbytes = sum(costs[start:end])
        npointers = sum(1 for p in pointers
                        if p.syntax != "external" and start < p.line <= end)
        out.append(Section(heading, level, nbytes, npointers))
    return out


# ---------------------------------------------------------------------------
# directory scan
# ---------------------------------------------------------------------------
def _md_names(mem_dir):
    return sorted(
        os.path.basename(p)
        for p in glob.glob(os.path.join(glob.escape(mem_dir), "*.md"))
        if os.path.isfile(p)
    )


def is_index_name(name):
    return name.startswith(INDEX_PREFIX) and name.endswith(".md")


def index_files(mem_dir):
    """Every ``MEMORY*.md`` in the dir, root index first then lanes sorted."""
    names = [n for n in _md_names(mem_dir) if is_index_name(n)]
    root = [ROOT_INDEX] if ROOT_INDEX in names else []
    return root + [n for n in names if n != ROOT_INDEX]


def is_ck_snapshot(name):
    """A retired ck session snapshot -- cruft, not a durable page. Same test
    the consolidate and backfill passes use (`startswith("ck_")`)."""
    return name.startswith(CK_PREFIX)


def page_files(mem_dir, include_ck=False):
    """Durable pages: every top-level ``*.md`` that is neither an index nor
    state/context nor a retired ``ck_*.md`` snapshot. These are the orphan
    candidates.

    ``include_ck=True`` adds the ck snapshots back in, for the one caller that
    wants to *see* them: the curate inventory flags them ``ck_snapshot`` so a
    curate pass can propose deleting them. Every integrity check wants them
    skipped -- they were never indexed and never will be.
    """
    return [n for n in _md_names(mem_dir)
            if not is_index_name(n) and n not in NON_PAGE_NAMES
            and (include_ck or not is_ck_snapshot(n))]


class Scan(object):
    """Everything both modes need, collected in one pass over the dir."""

    def __init__(self, mem_dir):
        self.dir = mem_dir
        self.indexes = index_files(mem_dir)
        self.pages = page_files(mem_dir)
        self.lines = {}
        self.costs = {}
        self.pointers = {}   # index -> [Pointer] (page targets only)
        self.external = {}   # index -> [Pointer] (cross-dir / URL targets)
        for idx in self.indexes:
            lines, costs = read_lines(os.path.join(mem_dir, idx))
            self.lines[idx] = lines
            self.costs[idx] = costs
            found = parse_pointers(lines)
            self.pointers[idx] = [p for p in found if p.syntax != "external"]
            self.external[idx] = [p for p in found if p.syntax == "external"]

        present = set(_md_names(mem_dir))

        # dangling: target missing on disk, attributed to the index it came
        # from. Per-index attribution is the whole point -- "something is
        # dangling somewhere" is not actionable.
        self.dangling = []
        for idx in self.indexes:
            seen = set()
            for p in self.pointers[idx]:
                if p.target in present or (p.target, p.line) in seen:
                    continue
                seen.add((p.target, p.line))
                self.dangling.append(Dangling(idx, p.target, p.line, p.syntax))

        # linked-from map, for orphans and for spotting a half-finished move.
        self.linked_from = {}
        for idx in self.indexes:
            for p in self.pointers[idx]:
                self.linked_from.setdefault(p.target, set()).add(idx)

        self.orphans = [p for p in self.pages if p not in self.linked_from]

        # An unreferenced lane index is an orphan too -- nothing opens it, so
        # every pointer it holds is unreachable. The root index is exempt: it
        # is the auto-loaded entry point, referenced by nothing by design.
        # A self-reference does not count as being referenced.
        self.orphan_indexes = [
            idx for idx in self.indexes
            if idx != ROOT_INDEX
            and not (self.linked_from.get(idx, set()) - {idx})
        ]

        # Pointed at from more than one index: legal, but the usual shape of a
        # half-finished move (new pointer added, old one never removed).
        self.duplicates = {t: sorted(s) for t, s in self.linked_from.items()
                           if len(s) > 1 and t in present and not is_index_name(t)}

    @property
    def ok(self):
        return not (self.dangling or self.orphans or self.orphan_indexes)

    def index_bytes(self, idx):
        return sum(self.costs[idx])

    def pointer_counts(self, idx):
        bare = sum(1 for p in self.pointers[idx] if p.syntax == "bare")
        inline = sum(1 for p in self.pointers[idx] if p.syntax == "inline")
        return bare, inline


# ---------------------------------------------------------------------------
# target resolution
# ---------------------------------------------------------------------------
def resolve_target(args):
    """The memory dir to inspect. Explicit path wins; else
    ``<store>/projects/<project>``, with the project resolved the same way
    `okfmem graduate` does (``--project``, else the cwd's git-root name,
    registry overrides applied)."""
    if args.target:
        d = os.path.abspath(os.path.expanduser(args.target))
        if not os.path.isdir(d):
            raise ReindexError(f"target directory not found: {d}")
        return d

    store = os.path.abspath(os.path.expanduser(args.store))
    project = args.project
    if not project:
        # Imported lazily: the parsing helpers above are imported by the
        # curate inventory script, which has no business loading the init
        # module just to read an index file.
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from memory_init import _current_git_root, _load_registry
        root = _current_git_root()
        if not root:
            raise ReindexError(
                "no TARGET given and cwd is not inside a git repo -- pass a "
                "memory dir, or --project NAME")
        reg = _load_registry(os.path.join(store, "registry.json"))
        project = reg.get("overrides", {}).get(root, os.path.basename(root))

    d = os.path.join(store, "projects", project)
    if not os.path.isdir(d):
        raise ReindexError(f"no store project dir for '{project}': {d}")
    return d


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------
def _pct(part, whole):
    return (100.0 * part / whole) if whole else 0.0


def _budget_row(name, path, budget):
    exists = os.path.isfile(path)
    nbytes = os.path.getsize(path) if exists else 0
    return {"file": name, "exists": exists, "bytes": nbytes,
            "budget": budget, "over": nbytes > budget,
            "pct_of_budget": round(_pct(nbytes, budget), 1)}


def report_data(scan, budget=None):
    """``--budget`` overrides the ceiling for both auto-loaded files -- they
    share one context budget, so splitting the override would let a caller
    tighten one and silently leave the other at the default."""
    mem_budget = MEMORY_BUDGET_BYTES if budget is None else budget
    st_budget = STATE_BUDGET_BYTES if budget is None else budget
    d = scan.dir

    auto = [
        _budget_row(ROOT_INDEX, os.path.join(d, ROOT_INDEX), mem_budget),
        _budget_row("STATE.md", os.path.join(d, "STATE.md"), st_budget),
    ]

    sections = []
    if ROOT_INDEX in scan.indexes:
        total = scan.index_bytes(ROOT_INDEX)
        for s in parse_sections(scan.lines[ROOT_INDEX], scan.costs[ROOT_INDEX],
                                scan.pointers[ROOT_INDEX]):
            sections.append({"heading": s.heading, "level": s.level,
                             "bytes": s.bytes, "pointers": s.pointers,
                             "pct": round(_pct(s.bytes, total), 1)})

    indexes = []
    for idx in scan.indexes:
        bare, inline = scan.pointer_counts(idx)
        indexes.append({"file": idx, "bytes": scan.index_bytes(idx),
                        "pointers": bare + inline, "bare": bare,
                        "inline": inline,
                        "external_refs": len(scan.external[idx]),
                        "auto_loaded": idx == ROOT_INDEX})

    page_bytes = sum(os.path.getsize(os.path.join(d, p)) for p in scan.pages)
    archived = len(glob.glob(os.path.join(glob.escape(d), "archive", "*.md")))
    return {"mode": "report", "dir": d, "auto_loaded": auto,
            "sections": sections, "indexes": indexes,
            "on_disk": {"pages": len(scan.pages), "bytes": page_bytes,
                        "archived": archived, "auto_loaded": False,
                        "note": "read on demand; costs zero context at "
                                "session start"}}


def render_report(data):
    out = ["# Reindex report: " + data["dir"], ""]
    out.append("## Auto-loaded bytes (the entire context cost)")
    out.append("")
    out.append("| File | Bytes | Ceiling | % of ceiling | Status |")
    out.append("|---|---|---|---|---|")
    for r in data["auto_loaded"]:
        if not r["exists"]:
            out.append(f"| {r['file']} | (absent) | {r['budget']} | - | - |")
            continue
        flag = "OVER" if r["over"] else "ok"
        out.append(f"| {r['file']} | {r['bytes']} | {r['budget']} | "
                   f"{r['pct_of_budget']}% | {flag} |")
    out.append("")

    out.append("## MEMORY.md, per section")
    out.append("")
    if not data["sections"]:
        out.append("_No MEMORY.md in this directory._")
    else:
        out.append("Own bytes per section (a heading row excludes its "
                   "subheadings), so the rows partition the file and the "
                   "percentages sum to 100.")
        out.append("")
        out.append("| Section | Bytes | % of file | Pointers |")
        out.append("|---|---|---|---|")
        for s in data["sections"]:
            # plain spaces, not `&nbsp;`: this is read in a terminal at least
            # as often as it is rendered, and the entity is noise in both.
            indent = "  " * max(0, s["level"] - 1)
            head = s["heading"].replace("|", "\\|")
            out.append(f"| {indent}{head} | {s['bytes']} | {s['pct']}% | "
                       f"{s['pointers']} |")
    out.append("")

    out.append("## Indexes")
    out.append("")
    out.append("| Index | Bytes | Pointers | rich | bare | auto-loaded |")
    out.append("|---|---|---|---|---|---|")
    for i in data["indexes"]:
        out.append(f"| {i['file']} | {i['bytes']} | {i['pointers']} | "
                   f"{i['inline']} | {i['bare']} | "
                   f"{'yes' if i['auto_loaded'] else 'no'} |")
    out.append("")

    od = data["on_disk"]
    out.append("## On disk (NOT auto-loaded -- zero context cost)")
    out.append("")
    out.append(f"- **Pages**: {od['pages']} ({od['bytes']} bytes)")
    out.append(f"- **Archived**: {od['archived']}")
    out.append(f"- These are {od['note']}. Only the auto-loaded table above "
               "spends context.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------
def verify_data(scan):
    return {
        "mode": "verify",
        "dir": scan.dir,
        "indexes": [{"file": i, "pointers": len(scan.pointers[i])}
                    for i in scan.indexes],
        "pages": len(scan.pages),
        "dangling": [{"index": d.index, "target": d.target, "line": d.line,
                      "syntax": d.syntax} for d in scan.dangling],
        "orphans": list(scan.orphans),
        "orphan_indexes": list(scan.orphan_indexes),
        "duplicates": scan.duplicates,
        "external_refs": sum(len(v) for v in scan.external.values()),
        "ok": scan.ok,
    }


def render_verify(data):
    out = ["# Reindex verify: " + data["dir"], ""]
    out.append(f"- indexes: {len(data['indexes'])} "
               f"({', '.join(i['file'] for i in data['indexes']) or 'none'})")
    out.append(f"- pointers: {sum(i['pointers'] for i in data['indexes'])} "
               f"across {len(data['indexes'])} index file(s)")
    out.append(f"- pages: {data['pages']}")
    out.append(f"- dangling: {len(data['dangling'])}")
    out.append(f"- orphans: {len(data['orphans'])}")
    out.append(f"- orphan indexes: {len(data['orphan_indexes'])}")
    if data["external_refs"]:
        out.append(f"- external/cross-dir refs (not checked): "
                   f"{data['external_refs']}")
    out.append("")

    if data["dangling"]:
        out.append("## Dangling pointers (target file does not exist)")
        out.append("")
        out.append("| Index | Line | Target | Syntax |")
        out.append("|---|---|---|---|")
        for d in data["dangling"]:
            out.append(f"| {d['index']} | {d['line']} | {d['target']} | "
                       f"{d['syntax']} |")
        out.append("")

    if data["orphans"]:
        out.append("## Orphan pages (exist on disk, in no index)")
        out.append("")
        for p in data["orphans"]:
            out.append(f"- {p}")
        out.append("")

    if data["orphan_indexes"]:
        out.append("## Orphan indexes (no other index points at them)")
        out.append("")
        for p in data["orphan_indexes"]:
            out.append(f"- {p}")
        out.append("")

    if data["duplicates"]:
        out.append("## Pointed at from more than one index (informational)")
        out.append("")
        for t, idxs in sorted(data["duplicates"].items()):
            out.append(f"- {t} -- {', '.join(idxs)}")
        out.append("")

    out.append("OK: index is intact." if data["ok"]
               else "FAIL: index is not intact (see above).")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# budget check
# ---------------------------------------------------------------------------
def budget_data(scan, limit=None):
    """Pointer lines over the per-line character budget, across every index.

    The unit is the *line*, not the pointer: the budget is a budget on the
    index line a session reads, and one list item may legally carry several
    slugs under a shared hook.

    Two things this deliberately does differently from the shell one-liner it
    replaces (which was blind to both):

      * **Both syntaxes.** A `/^- \\[/` guard sees only the rich form, and the
        bare form is exactly what a lane index uses -- i.e. it missed the
        pointers most likely to be over budget.
      * **Characters, not bytes.** `len` on a decoded line; BSD `awk`'s
        `length($0)` counts bytes, so the convention's em-dash alone
        over-reports by 2 per occurrence.

    A routing-table row (an index pointing at another index, `- MEMORY-lane.md
    -- covers ...`) is counted separately and not held to this budget: its
    "covers" clause is deliberately a sentence or two, so measuring it against
    the page-pointer budget would flag every correctly-reindexed store. It is
    reported, never silently dropped.
    """
    limit = POINTER_BUDGET_CHARS if limit is None else limit
    over, checked, routing = [], 0, 0
    for idx in scan.indexes:
        src = scan.lines[idx]
        by_line = {}
        for p in scan.pointers[idx]:
            by_line.setdefault(p.line, []).append(p.target)
        for lineno in sorted(by_line):
            targets = by_line[lineno]
            if all(is_index_name(t) for t in targets):
                routing += 1
                continue
            checked += 1
            nchars = len(src[lineno - 1])
            if nchars > limit:
                over.append({"index": idx, "line": lineno, "chars": nchars,
                             "targets": targets})
    return {"mode": "budget", "dir": scan.dir, "limit": limit,
            "pointer_lines": checked, "routing_rows": routing,
            "over": over, "ok": not over}


def render_budget(data):
    out = ["# Pointer budget: " + data["dir"], ""]
    out.append(f"{len(data['over'])}/{data['pointer_lines']} pointer lines "
               f"over the {data['limit']}-char budget "
               f"(characters, not bytes).")
    if data["routing_rows"]:
        out.append(f"({data['routing_rows']} routing-table row(s) point at "
                   "another index and are not held to this budget.)")
    out.append("")
    if data["over"]:
        out.append("| Index | Line | Chars | Points at |")
        out.append("|---|---|---|---|")
        for r in data["over"]:
            out.append(f"| {r['index']} | {r['line']} | {r['chars']} | "
                       f"{', '.join(r['targets'])} |")
        out.append("")
        out.append("Tighten the hook: move detail into the page body, where it "
                   "costs nothing. Advisory -- this mode never exits non-zero.")
    else:
        out.append("OK: every pointer line is within budget.")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser():
    p = argparse.ArgumentParser(
        prog="okfmem reindex",
        description="Read-only reindex engine: byte report + link-integrity "
                    "verification across every MEMORY*.md.")
    p.add_argument("target", nargs="?", metavar="TARGET",
                   help="memory dir (default: <store>/projects/<project>)")
    p.add_argument("--report", action="store_true",
                   help="where the auto-loaded bytes sit (the default)")
    p.add_argument("--verify", action="store_true",
                   help="link integrity; exits 1 on dangling/orphans")
    p.add_argument("--budget-check", action="store_true", dest="budget_check",
                   help=f"pointer lines over the {POINTER_BUDGET_CHARS}-char "
                        "budget, across every index (advisory; exits 0)")
    p.add_argument("--project", help="store project name")
    p.add_argument("--store", default=DEFAULT_STORE, help="store path")
    p.add_argument("--json", action="store_true", dest="as_json",
                   help="machine-readable output")
    p.add_argument("--budget", type=int, default=None,
                   help=f"auto-load byte ceiling (default "
                        f"{MEMORY_BUDGET_BYTES})")
    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Tolerate the subcommand token when this module is reached other than via
    # the dispatcher's runpy call (which already strips it).
    if argv and argv[0] == "reindex":
        argv = argv[1:]
    args = build_parser().parse_args(argv)

    try:
        mem_dir = resolve_target(args)
    except ReindexError as e:
        print(f"okfmem reindex: {e}", file=sys.stderr)
        return 2

    do_verify = args.verify
    do_budget = args.budget_check
    # no mode flag -> report
    do_report = args.report or not (do_verify or do_budget)

    scan = Scan(mem_dir)
    chunks = []
    payload = {}
    if do_report:
        data = report_data(scan, budget=args.budget)
        payload["report"] = data
        chunks.append(render_report(data))
    if do_verify:
        data = verify_data(scan)
        payload["verify"] = data
        chunks.append(render_verify(data))
    if do_budget:
        data = budget_data(scan)
        payload["budget"] = data
        chunks.append(render_budget(data))

    if args.as_json:
        # One mode -> that mode's object, so a caller asking for --verify gets
        # the documented shape rather than a wrapper it has to unpack.
        out = next(iter(payload.values())) if len(payload) == 1 else payload
        print(json.dumps(out, indent=2, sort_keys=True))
    else:
        print("\n\n".join(chunks))

    if do_verify and not scan.ok:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

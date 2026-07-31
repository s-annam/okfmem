#!/usr/bin/env python3
"""Memory-curate inventory pass.

Usage:  python3 inventory.py <memory-dir>

Emits a markdown report on stdout with three sections:
  1. Summary  - file count, total bytes, MEMORY.md size, orphan count
  2. Files    - per-file table with size, age, frontmatter type/name, link status
  3. Flags    - per-file heuristic signals (ck_snapshot, landed_doc, ...)

Deterministic only: this script flags candidates; the LLM decides verdicts.

Link status comes from the engine's shared reindex parser (`memory_reindex`),
so it walks **every** `MEMORY*.md` and accepts **both** pointer syntaxes. Parsing
only the root index in only the rich `[title](slug.md)` form -- what this script
used to do -- reported every page carried by a lane index as an orphan, and, far
worse, made a genuinely dangling pointer *inside a lane index* invisible (issue
#54). For the byte-level picture (auto-loaded budget, per-section breakdown),
run `okfmem reindex --report <memory-dir>`.

Pure stdlib, cross-platform (no `stat -f`/`stat -c`/GNU `date -d` shell-outs,
so this runs the same on macOS, Linux, and Windows).
"""
import os
import re
import sys
import time

# The engine modules normally live at the repo root, four levels up from this
# script (skills/okfmem-curate/scripts/) — realpath first, so that resolves the
# same whether the harness symlinked the file, the skill dir, or neither.
#
# But the tier-3 managed-copy install (`memory_init._make_link`'s copytree
# fallback, used on Windows without symlink privilege or junction support)
# copies the skill dir OUT of the repo, and four-up then lands on the harness
# skills parent, which holds no engine modules at all. So probe candidates in
# order and only import once one actually contains the engine; a miss prints
# one line, never a traceback.
_ENGINE_MODULE = "memory_reindex.py"
_HOME_ENGINE = os.path.expanduser("~/okfmem")


def _find_engine_root():
    four_up = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.realpath(__file__)))))
    for cand in (four_up, os.environ.get("OKFMEM_ENGINE"), _HOME_ENGINE):
        if cand and os.path.isfile(os.path.join(cand, _ENGINE_MODULE)):
            return cand
    return None


_ENGINE_ROOT = _find_engine_root()
if _ENGINE_ROOT and _ENGINE_ROOT not in sys.path:
    sys.path.insert(0, _ENGINE_ROOT)
try:
    import memory_reindex  # noqa: E402
except ImportError:
    sys.stderr.write(
        "inventory.py: okfmem engine not found (no %s beside this skill, in "
        "$OKFMEM_ENGINE, or in ~/okfmem) — run the engine copy instead: "
        "python3 ~/okfmem/skills/okfmem-curate/scripts/inventory.py "
        "<memory-dir>\n" % _ENGINE_MODULE)
    sys.exit(2)

FM_KEY_RE_TMPL = r'^{key}:\s*(.*)$'
CK_SNAPSHOT_RE = re.compile(r"^ck_\d{4}-\d{2}-\d{2}_.*\.md$")
LANDED_RE = re.compile(r"(landed|operational)", re.IGNORECASE)
SUPERSEDED_RE = re.compile(
    r"(SUPERSEDED|superseded by|replaced by|\(Note:)")

OLD_45D = 45
OLD_90D = 90


def read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def fm_field(text, key):
    """Extract a frontmatter field value between the leading '---' markers.
    Mirrors the bash version's awk logic: only looks inside the first
    frontmatter block, strips surrounding quotes, collapses '|' to '/'."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return ""
    pat = re.compile(r"^" + re.escape(key) + r":\s*(.*)$")
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = pat.match(line)
        if m:
            v = m.group(1).strip()
            v = v.strip('"').strip("'")
            v = v.replace("|", "/")
            return v
    return ""


def list_md_files(memory_dir):
    """Every file this report has something to say about: durable pages plus
    the retired `ck_*.md` snapshots. `MEMORY*.md` indexes and STATE/CONTEXT are
    excluded, so they can never be counted as orphans of themselves.

    ck snapshots are included here but NOT in the engine's orphan set: curate
    is the one pass that wants to see them (it flags them `ck_snapshot` so a
    plan can propose deleting them), while every integrity check skips them.
    """
    return memory_reindex.page_files(memory_dir, include_ck=True)


def file_age_days(path, today_epoch_days):
    mtime_days = int(os.path.getmtime(path) // 86400)
    return today_epoch_days - mtime_days


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <memory-dir>", file=sys.stderr)
        sys.exit(1)
    mem_dir = sys.argv[1]
    if not os.path.isdir(mem_dir):
        print(f"Usage: {sys.argv[0]} <memory-dir>", file=sys.stderr)
        print(f"Memory dir not found: {mem_dir}", file=sys.stderr)
        sys.exit(1)

    today_epoch_days = int(time.time() // 86400)
    scan = memory_reindex.Scan(mem_dir)
    files = list_md_files(mem_dir)
    linked = set(scan.linked_from)

    total_bytes = sum(os.path.getsize(os.path.join(mem_dir, f)) for f in files)
    memory_md_path = os.path.join(mem_dir, "MEMORY.md")
    mem_bytes = os.path.getsize(memory_md_path) if os.path.isfile(memory_md_path) else 0
    mem_lines = 0
    if os.path.isfile(memory_md_path):
        # Count newline characters to match the bash original's `wc -l < …`
        # (a final line without a trailing newline is NOT counted), not
        # len(splitlines()) which would over-count it by one.
        mem_lines = read_text(memory_md_path).count("\n")

    orphan_count = len(scan.orphans)
    dangling = scan.dangling

    out = []
    out.append(f"# Memory inventory: {mem_dir}")
    out.append("")
    out.append("## Summary")
    out.append("")
    ck_files = [f for f in files if memory_reindex.is_ck_snapshot(f)]
    out.append(f"- **Pages** (excl. MEMORY*.md / STATE.md): {len(files)}"
               + (f" (of which {len(ck_files)} retired ck_*.md snapshots)"
                  if ck_files else ""))
    out.append(f"- **Total bytes**: {total_bytes}")
    out.append(f"- **MEMORY.md**: {mem_bytes} bytes, {mem_lines} lines")
    out.append(f"- **Indexes** ({len(scan.indexes)}): "
               + ", ".join(f"{i} ({len(scan.pointers[i])} pointers)"
                           for i in scan.indexes))
    out.append(f"- **Orphans** (file exists, in no index): {orphan_count}"
               + (" — ck_*.md snapshots excluded; they were never indexed and "
                  "are flagged `ck_snapshot` below instead" if ck_files
                  else ""))
    out.append(f"- **Dangling** (in an index, file missing): {len(dangling)}")
    if scan.orphan_indexes:
        out.append("- **Orphan indexes** (no other index points at them): "
                   + ", ".join(scan.orphan_indexes))
    out.append("")
    out.append("## Files")
    out.append("")
    out.append("| File | Size | Age (d) | Type | Name | Status |")
    out.append("|------|------|---------|------|------|--------|")

    file_texts = {}
    for f in files:
        path = os.path.join(mem_dir, f)
        text = read_text(path)
        file_texts[f] = text
        size = os.path.getsize(path)
        age = file_age_days(path, today_epoch_days)
        ftype = fm_field(text, "type") or "?"
        name = fm_field(text, "name") or "?"
        if len(name) > 50:
            name = name[:47] + "..."
        if memory_reindex.is_ck_snapshot(f):
            status = "ck_snapshot"   # never indexed by design, so not an orphan
        else:
            status = "linked" if f in linked else "ORPHAN"
        out.append(f"| {f} | {size} | {age} | {ftype} | {name} | {status} |")

    if dangling:
        out.append("")
        out.append("### Dangling links (in an index, file missing)")
        out.append("")
        out.append("| Index | Line | Target |")
        out.append("|-------|------|--------|")
        for d in dangling:
            out.append(f"| {d.index} | {d.line} | {d.target} |")

    out.append("")
    out.append("## Heuristic flags")
    out.append("")
    out.append("| File | Flags |")
    out.append("|------|-------|")

    for f in files:
        text = file_texts[f]
        flags = []
        is_ck = memory_reindex.is_ck_snapshot(f)
        if is_ck or CK_SNAPSHOT_RE.match(f):
            flags.append("ck_snapshot")
        name = fm_field(text, "name")
        if LANDED_RE.search(f) or LANDED_RE.search(name):
            flags.append("landed_doc")
        if SUPERSEDED_RE.search(text):
            flags.append("superseded_marker")
        # `orphan` only for real pages: a ck snapshot is unindexed by design,
        # and flagging it here would contradict the Summary's orphan count,
        # which (like every integrity check) skips them.
        if f not in linked and not is_ck:
            flags.append("orphan")
        age = file_age_days(os.path.join(mem_dir, f), today_epoch_days)
        if age > OLD_90D:
            flags.append("old_90d")
        elif age > OLD_45D:
            flags.append("old_45d")
        if flags:
            out.append(f"| {f} | {','.join(flags)} |")

    out.append("")
    out.append("_Flags are signals, not verdicts. The LLM judgment pass turns "
               "these into keep/delete/compress/unsure recommendations._")

    print("\n".join(out))


if __name__ == "__main__":
    main()

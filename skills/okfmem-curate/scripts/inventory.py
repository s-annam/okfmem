#!/usr/bin/env python3
"""Memory-curate inventory pass.

Usage:  python3 inventory.py <memory-dir>

Emits a markdown report on stdout with three sections:
  1. Summary  - file count, total bytes, MEMORY.md size, orphan count
  2. Files    - per-file table with size, age, frontmatter type/name, link status
  3. Flags    - per-file heuristic signals (ck_snapshot, landed_doc, ...)

Deterministic only: this script flags candidates; the LLM decides verdicts.

Pure stdlib, cross-platform (no `stat -f`/`stat -c`/GNU `date -d` shell-outs,
so this runs the same on macOS, Linux, and Windows).
"""
import os
import re
import sys
import time

LINK_RE = re.compile(r"\]\(([A-Za-z][A-Za-z0-9_-]*\.md)\)")
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


def linked_filenames(memory_dir):
    memory_md = os.path.join(memory_dir, "MEMORY.md")
    if not os.path.isfile(memory_md):
        return set()
    text = read_text(memory_md)
    return set(LINK_RE.findall(text))


def list_md_files(memory_dir):
    files = [f for f in os.listdir(memory_dir)
              if f.endswith(".md") and f != "MEMORY.md"
              and os.path.isfile(os.path.join(memory_dir, f))]
    return sorted(files)


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
    files = list_md_files(mem_dir)
    linked = linked_filenames(mem_dir)

    total_bytes = sum(os.path.getsize(os.path.join(mem_dir, f)) for f in files)
    memory_md_path = os.path.join(mem_dir, "MEMORY.md")
    mem_bytes = os.path.getsize(memory_md_path) if os.path.isfile(memory_md_path) else 0
    mem_lines = 0
    if os.path.isfile(memory_md_path):
        # Count newline characters to match the bash original's `wc -l < …`
        # (a final line without a trailing newline is NOT counted), not
        # len(splitlines()) which would over-count it by one.
        mem_lines = read_text(memory_md_path).count("\n")

    orphan_count = sum(1 for f in files if f not in linked)
    dangling = sorted(f for f in linked
                       if not os.path.isfile(os.path.join(mem_dir, f)))

    out = []
    out.append(f"# Memory inventory: {mem_dir}")
    out.append("")
    out.append("## Summary")
    out.append("")
    out.append(f"- **Files** (excl. MEMORY.md): {len(files)}")
    out.append(f"- **Total bytes**: {total_bytes}")
    out.append(f"- **MEMORY.md**: {mem_bytes} bytes, {mem_lines} lines")
    out.append(f"- **Orphans** (file exists, not linked): {orphan_count}")
    out.append(f"- **Dangling** (linked, file missing): {len(dangling)}")
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
        status = "linked" if f in linked else "ORPHAN"
        out.append(f"| {f} | {size} | {age} | {ftype} | {name} | {status} |")

    if dangling:
        out.append("")
        out.append("### Dangling links (in MEMORY.md but file missing)")
        out.append("")
        for link in dangling:
            out.append(f"- {link}")

    out.append("")
    out.append("## Heuristic flags")
    out.append("")
    out.append("| File | Flags |")
    out.append("|------|-------|")

    for f in files:
        text = file_texts[f]
        flags = []
        if CK_SNAPSHOT_RE.match(f):
            flags.append("ck_snapshot")
        name = fm_field(text, "name")
        if LANDED_RE.search(f) or LANDED_RE.search(name):
            flags.append("landed_doc")
        if SUPERSEDED_RE.search(text):
            flags.append("superseded_marker")
        if f not in linked:
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

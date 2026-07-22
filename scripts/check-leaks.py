#!/usr/bin/env python3
"""Leak gate for the public okfmem repo — okfmem's analog to offlinecv's
fixture-PII check. The repo is public; a private string committed to a tracked
file is the one failure mode that is expensive to walk back, so this runs in CI
(and can be wired into a pre-commit / Stop hook) and exits non-zero naming the
offending file:line and pattern.

It scans the CONTENT of git-tracked files only — not commit history (history is
left as-is by policy; see CLAUDE.md). Its job is to stop NEW leaks reaching a
tracked file.

Patterns are assembled from fragments on purpose: it means this script does not
itself contain the literal strings it hunts for, so it never flags itself and
needs no self-exclusion.
"""
from __future__ import annotations

import re
import subprocess
import sys

# (label, compiled pattern). Fragments keep the literals out of this file.
#
# Patterns match a REAL leak (the actual payload), not prose that *describes* the
# forbidden shape. CLAUDE.md and the open-pr skill legitimately mention
# "no `Claude-Session:` trailer" and "no claude.ai/code/session_… URL" — those
# must not trip the gate. So the session patterns require a real hash / a real
# URL after the marker, which documentation of the rule never contains.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("maintainer home path", re.compile(r"/Users/" + "annam")),
    # real session URL: the marker followed by an actual base62 id, not "session_…".
    ("private session URL", re.compile(r"claude\.ai/code/" + r"session_[A-Za-z0-9]{6}")),
    # real provenance trailer: the marker followed by an actual URL, not `Claude-Session:` in prose.
    ("model provenance trailer", re.compile(r"Claude" + r"-Session:\s*https?://")),
    ("personal email", re.compile(r"116" + r"ideas\.com")),
]

# Allowed placeholders / intentional public references — never flag these.
# `~/okfmem-store`, `$OKFMEM_STORE`, `/Users/you`, `/Users/<name>`, `session_…`
# (ellipsis), and a bare `Claude-Session:` mentioned in a rule are documented
# examples, so the patterns above are scoped to real payloads only.

# Extensions worth scanning as text. Everything else (images, gifs, db) skipped.
SKIP_SUFFIXES = (".gif", ".png", ".jpg", ".jpeg", ".ico", ".db", ".pdf", ".woff", ".woff2")


def tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], capture_output=True, text=True, check=True
    ).stdout
    return [f for f in out.splitlines() if f and not f.endswith(SKIP_SUFFIXES)]


def main() -> int:
    hits: list[str] = []
    for path in tracked_files():
        try:
            with open(path, encoding="utf-8", errors="strict") as fh:
                lines = fh.readlines()
        except (OSError, UnicodeDecodeError):
            continue  # binary or unreadable — nothing text-scannable
        for lineno, line in enumerate(lines, 1):
            for label, pat in PATTERNS:
                if pat.search(line):
                    hits.append(f"{path}:{lineno}: {label} -> {line.strip()[:120]}")

    if hits:
        print("Leak gate FAILED — private strings in tracked files:\n", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        print(
            "\nRemove them before committing. Use ~/okfmem-store, $OKFMEM_STORE, or "
            "/Users/you placeholders instead. See CLAUDE.md 'Hard rules'.",
            file=sys.stderr,
        )
        return 1
    print(f"Leak gate OK — scanned {len(tracked_files())} tracked files, no leaks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

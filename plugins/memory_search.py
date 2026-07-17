#!/usr/bin/env python3
"""okfmem search — SQLite FTS5 index over normalized session turns (OPT-IN plugin).

Not wired into the core okfmem dispatcher's MODULES map — core stays
harness-agnostic. Reached only via `okfmem search ...` / `okfmem index ...`,
which the dispatcher lazily routes to plugins/memory_<cmd>.py when present.

The `.db` is a DERIVED LOCAL CACHE: gitignored, rebuildable from the harness
transcripts anytime, never committed. Default location keeps it out of any git
tree entirely: $OKFMEM_CACHE, else ~/.cache/okfmem/sessions.db.

Usage:
  okfmem index  [--rebuild] [--claude-root PATH] [--agy-path PATH] [--db PATH]
  okfmem search "<query>" [--limit N] [--harness H] [--project P] [--db PATH]

Design seam (deferred, #20): a semantic/embedding provider can slot behind
`search` later — FTS5 is the file-first v1 wedge, no embedding dep now.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from adapters import agy, claude_code  # noqa: E402

DEFAULT_DB = os.environ.get("OKFMEM_CACHE") or os.path.expanduser("~/.cache/okfmem/sessions.db")

# ts/idx UNINDEXED: stored + returned but not tokenized (they are metadata, not query terms).
_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS turns USING fts5(
    harness, project, session_id, ts UNINDEXED, idx UNINDEXED,
    role, text, tool_name,
    tokenize = 'porter unicode61'
);
"""


def _connect(db_path):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute(_SCHEMA)
    return con


def _all_turns(claude_root, agy_path):
    yield from claude_code.iter_turns(claude_root)
    yield from agy.iter_turns(agy_path)


def cmd_index(args):
    db_path = os.path.expanduser(args.db)
    if args.rebuild and os.path.exists(db_path):
        os.remove(db_path)
    con = _connect(db_path)
    if not args.rebuild:
        con.execute("DELETE FROM turns")  # cheap full refresh; corpus is small + rebuildable
    n = 0
    with con:
        for t in _all_turns(args.claude_root, args.agy_path):
            con.execute(
                "INSERT INTO turns (harness,project,session_id,ts,idx,role,text,tool_name) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (t["harness"], t["project"], t["session_id"], t["ts"], t["idx"],
                 t["role"], t["text"], t["tool_name"]),
            )
            n += 1
    con.close()
    print(f"okfmem search: indexed {n} turns -> {db_path}")


def cmd_search(args):
    db_path = os.path.expanduser(args.db)
    if not os.path.exists(db_path):
        print(f"okfmem search: no index at {db_path} — run `okfmem index` first.", file=sys.stderr)
        sys.exit(1)
    con = sqlite3.connect(db_path)
    where = ["turns MATCH ?"]
    params = [args.query]
    if args.harness:
        where.append("harness = ?"); params.append(args.harness)
    if args.project:
        where.append("project = ?"); params.append(args.project)
    sql = (
        "SELECT harness, project, session_id, idx, ts, role, tool_name, "
        "snippet(turns, 6, '[', ']', '…', 12) AS snip "
        "FROM turns WHERE " + " AND ".join(where) + " ORDER BY rank LIMIT ?"
    )
    params.append(args.limit)
    try:
        rows = con.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        print(f"okfmem search: bad FTS query: {e}", file=sys.stderr)
        sys.exit(2)
    con.close()
    if not rows:
        print("okfmem search: no hits.")
        return
    for harness, project, session, idx, ts, role, tool_name, snip in rows:
        loc = f"{harness}:{project}:{str(session)[:8]}:{idx}"
        tag = f" ({tool_name})" if tool_name else ""
        stamp = str(ts or "")[:19]  # claude=ISO str, agy=epoch int
        print(f"{loc}  [{role}{tag}] {stamp}\n    {snip}")


def main():
    p = argparse.ArgumentParser(prog="okfmem search", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="mode")

    pi = sub.add_parser("index", help="(re)build the FTS index from harness transcripts")
    pi.add_argument("--rebuild", action="store_true", help="drop + recreate the db")
    pi.add_argument("--claude-root", default=claude_code.DEFAULT_ROOT)
    pi.add_argument("--agy-path", default=agy.DEFAULT_PATH)
    pi.add_argument("--db", default=DEFAULT_DB)
    pi.set_defaults(func=cmd_index)

    ps = sub.add_parser("search", help="query the index")
    ps.add_argument("query", help="FTS5 MATCH expression")
    ps.add_argument("--limit", type=int, default=20)
    ps.add_argument("--harness", help="filter: claude-code | agy")
    ps.add_argument("--project", help="filter: project id")
    ps.add_argument("--db", default=DEFAULT_DB)
    ps.set_defaults(func=cmd_search)

    # Dispatcher invokes this file as either `search <q>` or `index ...`.
    # If the first token isn't a known subcommand, treat the whole invocation
    # as a search query (so `okfmem search "foo"` works via the plugin router).
    argv = sys.argv[1:]
    if argv and argv[0] not in ("index", "search", "-h", "--help"):
        argv = ["search"] + argv
    args = p.parse_args(argv)
    if not getattr(args, "func", None):
        p.print_help()
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()

"""Claude Code extractor adapter — JSONL session transcripts -> normalized turns.

Source: ~/.claude/projects/<project>/<uuid>.jsonl  (one JSON object per line).
v1 target per issue #2 — the easy, clean harness.

Coupling to Claude's on-disk format lives HERE and nowhere else. Emits the
harness-neutral turn schema from okfmem_session. Drops every tool_result body;
scrubs every text; keeps tool_name + a short signature off the tool_use side.
"""
from __future__ import annotations

import json
import os
import sys

# import the shared lib whether run as a module or via runpy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from okfmem_session import Turn, scrub, strip_signature  # noqa: E402

HARNESS = "claude-code"
DEFAULT_ROOT = os.path.expanduser("~/.claude/projects")


def _project_from_path(path):
    """Claude encodes the project as the parent dir name (e.g. -Users-you-myproject)."""
    return os.path.basename(os.path.dirname(path))


def extract_file(path):
    """Yield normalized turns for one .jsonl transcript."""
    project = _project_from_path(path)
    idx = 0
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") not in ("user", "assistant"):
                continue  # skip attachment/system/snapshot/mode/etc.
            msg = d.get("message") or {}
            role = msg.get("role") or d.get("type")
            ts = d.get("timestamp")
            session_id = d.get("sessionId") or os.path.splitext(os.path.basename(path))[0]
            content = msg.get("content")

            if isinstance(content, str):
                text = scrub(content).strip()
                if text:
                    yield Turn(HARNESS, project, session_id, ts, idx, role, text)
                    idx += 1
                continue

            if not isinstance(content, list):
                continue

            for block in content:
                if not isinstance(block, dict):
                    continue
                bt = block.get("type")
                if bt == "text":
                    text = scrub(block.get("text", "")).strip()
                    if text:
                        yield Turn(HARNESS, project, session_id, ts, idx, role, text)
                        idx += 1
                elif bt == "thinking":
                    text = scrub(block.get("thinking", "")).strip()
                    if text:
                        yield Turn(HARNESS, project, session_id, ts, idx, "thinking", text)
                        idx += 1
                elif bt == "tool_use":
                    sig = strip_signature(block.get("name"), block.get("input"))
                    yield Turn(HARNESS, project, session_id, ts, idx, "tool", sig,
                               tool_name=block.get("name"))
                    idx += 1
                # tool_result: body dropped entirely (tool_name already captured above)


def iter_turns(root=DEFAULT_ROOT, project=None):
    """Walk all (or one) project dir(s) under root, yielding turns from every .jsonl."""
    root = os.path.expanduser(root)
    if not os.path.isdir(root):
        return
    for proj in sorted(os.listdir(root)):
        if project and proj != project:
            continue
        pdir = os.path.join(root, proj)
        if not os.path.isdir(pdir):
            continue
        for fn in sorted(os.listdir(pdir)):
            if fn.endswith(".jsonl"):
                yield from extract_file(os.path.join(pdir, fn))

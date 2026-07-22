"""agy (Antigravity CLI) extractor adapter — coarse user-prompt index.

Source: ~/.gemini/antigravity-cli/history.jsonl  (plain JSONL, USER PROMPTS ONLY).
Keys: conversationId, display, timestamp, workspace.

v1 target per issue #2 is deliberately COARSE: the full-turn
per-conversation store (`conversations/<uuid>.db`, protobuf `step_payload`) is
opaque and DEFERRED. This adapter indexes only what history.jsonl exposes —
the user's prompts — which is enough for "when did I ask about X" recall.
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from okfmem_session import Turn, scrub  # noqa: E402

HARNESS = "agy"
DEFAULT_PATH = os.path.expanduser("~/.gemini/antigravity-cli/history.jsonl")


def _project_from_workspace(ws):
    return os.path.basename(ws.rstrip("/")) if ws else "unknown"


def iter_turns(path=DEFAULT_PATH, project=None):
    """Yield one normalized turn per user prompt in history.jsonl.

    idx is a per-conversation running counter so hits read
    agy:<project>:<conversationId>:<n>.
    """
    path = os.path.expanduser(path)
    if not os.path.isfile(path):
        return
    per_conv = {}
    with open(path, "r", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            ws = d.get("workspace")
            proj = _project_from_workspace(ws)
            if project and proj != project:
                continue
            conv = d.get("conversationId") or "unknown"
            text = scrub(d.get("display", "")).strip()
            if not text:
                continue
            idx = per_conv.get(conv, 0)
            per_conv[conv] = idx + 1
            yield Turn(HARNESS, proj, conv, d.get("timestamp"), idx, "user", text)

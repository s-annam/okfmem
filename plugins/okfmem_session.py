"""okfmem session-search — shared lib: normalized turn schema, secret scrub, body strip.

This is the ONE place coupling-free logic lives. Adapters (claude_code, agy) import
`scrub`, `strip_signature`, and `Turn` from here; `memory_search.py` consumes the turns.

Normalized turn (harness-neutral), per issue s-annam/tools#20:
    { harness, project, session_id, ts, idx, role, text, tool_name? }

Security posture (locked by #20):
  - ALL tool_result bodies dropped upstream (adapters never emit them).
  - Every emitted `text` runs through scrub() to strip secrets before it can hit disk.
"""
from __future__ import annotations

import re

# Ordered turn fields. tool_name is optional (None for plain chat turns).
FIELDS = ("harness", "project", "session_id", "ts", "idx", "role", "text", "tool_name")


def Turn(harness, project, session_id, ts, idx, role, text, tool_name=None):
    """A normalized turn as a plain dict (git-friendly, json-serializable)."""
    return {
        "harness": harness,
        "project": project,
        "session_id": session_id,
        "ts": ts,
        "idx": idx,
        "role": role,
        "text": text,
        "tool_name": tool_name,
    }


# --- Secret scrub ---------------------------------------------------------
# Category-borrowed from purge_pii_history.sh's posture, but written as a
# reusable regex family (that script uses repo-specific LITERAL tokens + a git
# rewriter, which does not transfer). Each pattern -> «REDACTED».
_REDACT = "«REDACTED»"

# High-confidence provider token shapes (structural — low false-positive rate).
_TOKEN_PATTERNS = [
    r"sk-[A-Za-z0-9_-]{16,}",                       # OpenAI / Anthropic style
    r"gh[pousr]_[A-Za-z0-9]{16,}",                  # GitHub PAT/OAuth/user/server/refresh
    r"github_pat_[A-Za-z0-9_]{20,}",                # GitHub fine-grained PAT
    r"xox[baprs]-[A-Za-z0-9-]{10,}",                # Slack
    r"AKIA[0-9A-Z]{16}",                            # AWS access key id
    r"AIza[A-Za-z0-9_-]{20,}",                      # Google API key
    r"lin_api_[A-Za-z0-9]{20,}",                    # Linear API key
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}",  # JWT
]

# Assignment-shaped secrets: KEY = "value" / KEY: value  (key name is the signal).
_ASSIGN_KEYS = r"(?:api[_-]?key|secret|password|passwd|authorization|auth[_-]?token|access[_-]?token|bearer|token|LINEAR_API[A-Z_]*)"
_ASSIGN_PATTERN = (
    r"(?i)(" + _ASSIGN_KEYS + r")"          # 1: key
    r"(\s*[:=]\s*|\s+)"                        # 2: separator (=, :, or bearer <tok>)
    r"['\"]?([A-Za-z0-9._\-/+]{8,})['\"]?"   # 3: value
)

_COMPILED = [re.compile(p) for p in _TOKEN_PATTERNS]
_ASSIGN = re.compile(_ASSIGN_PATTERN)


def scrub(text):
    """Redact secrets from free text. Idempotent, cheap, runs on every emitted turn."""
    if not text:
        return text
    for rx in _COMPILED:
        text = rx.sub(_REDACT, text)
    # keep the key name (searchable, harmless), redact only the value
    text = _ASSIGN.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACT}", text)
    return text


# --- Tool signature -------------------------------------------------------
_SIG_MAX = 120


def strip_signature(tool_name, tool_input):
    """Short, scrubbed one-line signature for a tool_use — NEVER the full body.

    Keeps enough to search ("Bash: git commit -m ...") without storing diffs,
    file contents, or command output. tool_result bodies are dropped entirely
    by callers; this covers the tool_use input side.
    """
    if isinstance(tool_input, dict):
        # prefer the human-meaningful field per common tools
        for k in ("command", "description", "query", "pattern", "file_path", "path", "url", "prompt"):
            v = tool_input.get(k)
            if isinstance(v, str) and v.strip():
                sig = v.strip().splitlines()[0]
                break
        else:
            sig = " ".join(sorted(tool_input.keys()))
    elif isinstance(tool_input, str):
        sig = tool_input.strip().splitlines()[0] if tool_input.strip() else ""
    else:
        sig = ""
    sig = scrub(sig)
    if len(sig) > _SIG_MAX:
        sig = sig[:_SIG_MAX] + "…"
    return sig

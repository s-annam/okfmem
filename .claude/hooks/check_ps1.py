#!/usr/bin/env python3
"""PostToolUse hook: parse-check any edited/written .ps1, .py, or .sh file.

Claude Code invokes this after every Edit/Write with a JSON payload on stdin.
Dispatches by the touched file's suffix:

  .ps1 / .psm1 -- PowerShell's own parser (unchanged from the original,
                  .ps1-only version of this hook).
  .py          -- ast.parse (hard fail on SyntaxError) plus, when `ruff` is
                  on PATH, `ruff format --check` / `ruff check` as
                  report-only feedback (never blocks -- CI treats ruff as
                  advisory today, and this hook must match that).
  .sh          -- `bash -n` (hard fail on syntax error), skipped cleanly
                  when no `bash` is on PATH.

A syntax-level hard fail exits 2, Claude Code's "blocking feedback" code for
PostToolUse hooks, with the parse/syntax error written to stderr so the
agent sees it immediately instead of shipping a file that only breaks when
something runs it.

Fail-open by design, always: a missing checker binary (no pwsh/powershell,
no ruff, no bash), an unreadable file, or a malformed hook payload must
never block unrelated work -- the hook silently no-ops in all of those
cases rather than failing the edit.

Limitation (deliberate honesty, carried over from the .ps1-only version):
the .ps1 check catches SYNTAX errors only. Runtime 5.1-isms -- e.g. stderr
redirection turning fatal under $ErrorActionPreference='Stop' -- parse
clean and still need a real 5.1 run (`powershell -File <script> -DryRun`)
to surface.
"""

import ast
import json
import shutil
import subprocess
import sys


def check_powershell(path: str) -> int:
    # Prefer pwsh (7+) over Windows PowerShell (5.1): the 7 grammar is a
    # superset, so it won't false-flag 7-only syntax (`??`, ternary `?:`,
    # null-conditional `?.`) that a script legitimately targeting pwsh 7 uses.
    # Resolving 5.1 first made every such file fail the parse check spuriously.
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        return 0  # no PowerShell engine available: silent no-op

    # Parser::ParseFile reports every syntax error with line numbers.
    ps = (
        "$errs = $null; "
        "[System.Management.Automation.Language.Parser]::ParseFile("
        "'{path}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs) {{ foreach ($e in $errs) {{ "
        "[Console]::Error.WriteLine('{path}:' + $e.Extent.StartLineNumber + ': ' + $e.Message) }}; "
        "exit 2 }}"
    ).format(path=path.replace("'", "''"))
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-Command", ps],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        sys.stderr.write(
            result.stderr or result.stdout or "PowerShell parse check failed.\n"
        )
        return 2
    return 0


def check_python(path: str) -> int:
    try:
        with open(path, "r", encoding="utf-8") as f:
            source = f.read()
    except OSError:
        return 0  # file unreadable/gone: never block on hook plumbing

    try:
        ast.parse(source, filename=path)
    except SyntaxError as e:
        lineno = e.lineno if e.lineno is not None else "?"
        sys.stderr.write(f"{path}:{lineno}: {e.msg}\n")
        return 2

    ruff = shutil.which("ruff")
    if not ruff:
        return 0  # fail-open: no ruff on PATH, matches CI treating it as advisory

    # Report-only: ruff findings never block the edit, only ast.parse does.
    for args, label in (
        (["format", "--check", path], "ruff format --check"),
        (["check", path], "ruff check"),
    ):
        result = subprocess.run(
            [ruff, *args], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            sys.stderr.write(f"{label} (report only, not blocking):\n")
            sys.stderr.write(result.stdout or result.stderr or "")
    return 0


def check_bash(path: str) -> int:
    bash = shutil.which("bash")
    if not bash:
        return 0  # no bash on PATH: silent no-op

    result = subprocess.run(
        [bash, "-n", path],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "bash -n check failed.\n")
        return 2
    return 0


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload: never block on hook plumbing

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    lower = path.lower()

    if lower.endswith((".ps1", ".psm1")):
        return check_powershell(path)
    if lower.endswith(".py"):
        return check_python(path)
    if lower.endswith(".sh"):
        return check_bash(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

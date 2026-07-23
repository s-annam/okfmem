#!/usr/bin/env python3
"""PostToolUse hook: parse-check any edited/written PowerShell file.

Claude Code invokes this after every Edit/Write with a JSON payload on stdin.
If the touched file is a .ps1/.psm1, run PowerShell's own parser over it and
fail the hook (exit 2) on syntax errors so the agent sees them immediately,
instead of shipping a script that only breaks when a user runs it.

Cross-platform by design: prefers pwsh (works on Linux/macOS runners too),
falls back to Windows PowerShell 5.1 (`powershell`) -- which is also the more
honest check on Windows, since 5.1 is the strictest/oldest engine the scripts
must support. If neither exists (e.g. a Linux contributor without pwsh), the
hook is a silent no-op: it must never block unrelated work.

Limitation (deliberate honesty): this catches SYNTAX errors only. Runtime
5.1-isms -- e.g. stderr redirection turning fatal under
$ErrorActionPreference='Stop' -- parse clean and still need a real 5.1 run
(`powershell -File <script> -DryRun`) to surface.
"""

import json
import shutil
import subprocess
import sys


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # malformed payload: never block on hook plumbing

    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.lower().endswith((".ps1", ".psm1")):
        return 0

    shell = shutil.which("powershell") or shutil.which("pwsh")
    if not shell:
        return 0  # no PowerShell engine available: silent no-op

    # Parser::ParseFile reports every syntax error with line numbers; exit 2
    # is Claude Code's "blocking feedback" code for PostToolUse hooks.
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


if __name__ == "__main__":
    sys.exit(main())

# okfmem - statusline badge for Claude Code (PowerShell).
# ASCII-only on purpose: powershell 5.1 reads a BOM-less .ps1 as cp1252, so any
# non-ASCII byte becomes a smart-quote and cascades into parse errors.
#
# Reads the save-state flag the Stop hook writes and emits a compact badge,
# mirroring git's dirty-marker convention (color + trailing `*` carry the state):
#   okfmem*  amber  - work this session not yet captured (run /okfmem-save)
#   okfmem   green  - captured this session
#   (nothing)       - no work, or opted out (OKFMEM_NO_STATUS=1)
#
# Wire it into your statusline command, e.g.:
#   $badge = pwsh -NoProfile -File C:\path\to\okfmem-statusline.ps1

$ErrorActionPreference = 'SilentlyContinue'

$base = if ($env:CLAUDE_CONFIG_DIR) { $env:CLAUDE_CONFIG_DIR } else { Join-Path $HOME '.claude' }
$flag = Join-Path $base '.okfmem-status'

# Refuse a symlink/reparse point - a local attacker could aim the flag at a
# sensitive file and have its bytes render every keystroke.
$item = Get-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
if (-not $item) { exit 0 }
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { exit 0 }

# Cap the read and strip anything outside [a-z]; blocks escape-injection.
$raw = Get-Content -LiteralPath $flag -Raw -ErrorAction SilentlyContinue
if (-not $raw) { exit 0 }
$state = ($raw.Substring(0, [Math]::Min(16, $raw.Length)) -replace '[^A-Za-z]', '').ToLower()

$esc = [char]27
switch ($state) {
  'unsaved' { [Console]::Out.Write("$esc[38;5;172mokfmem*$esc[0m") }
  'saved'   { [Console]::Out.Write("$esc[2;38;5;35mokfmem$esc[0m") }
  default   { exit 0 }
}

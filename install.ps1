#Requires -Version 5.1
<#
.SYNOPSIS
    Native Windows installer for okfmem (mirrors install.sh's intent for
    native PowerShell/cmd -- no WSL, no Git Bash required).

.DESCRIPTION
    1. Checks for git and a Python launcher (py or python).
    2. Creates %USERPROFILE%\.local\bin and writes okfmem.cmd + okfmem.ps1
       wrappers that invoke this repo's `okfmem` entry point.
    3. Creates (or reuses) a local git-backed store at $env:OKFMEM_STORE, else
       %USERPROFILE%\okfmem-store.
    4. Runs memory_backfill.py and memory_init.py to wire the store into
       detected harnesses.
    5. Prints PATH guidance (never silently mutates PATH) and the
       Windows-correct Stop-hook JSON snippet.
#>

$ErrorActionPreference = "Stop"

Write-Host "=> Installing okfmem..."

# 0. Check dependencies -------------------------------------------------
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "Error: 'git' is not installed or not on PATH."
    Write-Host "  okfmem requires git to version-control your memory store."
    exit 1
}

# Prefer the `py` launcher (installed by python.org installers on Windows);
# fall back to `python` (e.g. Microsoft Store / venv installs).
$PyCmd = $null
foreach ($cand in @("py", "python")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $PyCmd = $cand
        break
    }
}
if (-not $PyCmd) {
    Write-Host "Error: neither 'py' nor 'python' is on PATH."
    Write-Host "  okfmem uses Python (standard library only) for its engine."
    exit 1
}

# 1. Setup CLI wrappers ---------------------------------------------------
$EngineDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$BinDir = Join-Path $env:USERPROFILE ".local\bin"
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# okfmem.cmd -- works from cmd.exe and from PowerShell (cmd wrappers run fine
# in both). okfmem.ps1 is a native PowerShell entry point for anyone calling
# it from a script rather than an interactive shell.
$OkfmemPy = Join-Path $EngineDir "okfmem"
$CmdWrapper = @"
@echo off
$PyCmd "$OkfmemPy" %*
"@
Set-Content -Path (Join-Path $BinDir "okfmem.cmd") -Value $CmdWrapper -Encoding ASCII

$Ps1Wrapper = @"
& $PyCmd '$OkfmemPy' @args
"@
Set-Content -Path (Join-Path $BinDir "okfmem.ps1") -Value $Ps1Wrapper -Encoding UTF8

Write-Host "=> Wrote okfmem.cmd / okfmem.ps1 wrappers to $BinDir"

# 2. Setup data store ------------------------------------------------------
$StoreDir = $env:OKFMEM_STORE
if (-not $StoreDir) { $StoreDir = Join-Path $env:USERPROFILE "okfmem-store" }

if (-not (Test-Path $StoreDir)) {
    Write-Host "=> Creating local data store at $StoreDir"
    New-Item -ItemType Directory -Force -Path $StoreDir | Out-Null
    git -C "$StoreDir" init -q
} else {
    Write-Host "=> Found existing store at $StoreDir"
}

# 3. Wire it up -------------------------------------------------------------
Write-Host "=> Running backfill and initialization..."
& $PyCmd (Join-Path $EngineDir "memory_backfill.py")
& $PyCmd (Join-Path $EngineDir "memory_init.py")

Write-Host ""
Write-Host "okfmem installation complete!"
Write-Host ""
Write-Host "Next steps:"

$PathDirs = $env:Path -split ";"
if ($PathDirs -notcontains $BinDir) {
    Write-Host "1. Add $BinDir to your User PATH, e.g.:"
    Write-Host "     [Environment]::SetEnvironmentVariable('Path', `"`$env:Path;$BinDir`", 'User')"
    Write-Host "   (open a new terminal afterward for PATH changes to take effect)"
}
Write-Host "2. Check system status by running: okfmem status"
Write-Host "3. (Optional) Set up a remote for your store: git -C $StoreDir remote add origin <url>"
Write-Host "4. Wire the Stop hook in your agent's settings (e.g. Claude Code's"
Write-Host "   settings.json) using the snippet below -- note the absolute path"
Write-Host "   and no '~' (PowerShell/cmd don't expand it the way a POSIX shell does):"
Write-Host ""

$ConsolidatePy = (Join-Path $EngineDir "memory_consolidate.py").Replace("\", "\\")
$HookJson = @"
{ "hooks": { "Stop": [ { "hooks": [ {
  "type": "command",
  "command": "$PyCmd $ConsolidatePy --stdin-hook"
} ] } ] } }
"@
Write-Host $HookJson

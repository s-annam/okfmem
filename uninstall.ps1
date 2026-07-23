#Requires -Version 5.1
<#
.SYNOPSIS
    Native Windows uninstaller for okfmem (mirrors uninstall.sh's intent for
    native PowerShell/cmd -- no WSL, no Git Bash required).

.DESCRIPTION
    Asks ONE up-front [y/N] confirmation before removing anything (default:
    No -- abort). Pass -Yes to answer it unattended (scripts / CI); if no
    prompt can be shown and -Yes was not given, the script aborts with
    nothing removed. Then:

    1. Removes the okfmem.cmd / okfmem.ps1 CLI wrappers from
       %USERPROFILE%\.local\bin.
    2. Strips okfmem-managed harness wiring (pointer blocks, skill links/
       junctions/copies, per-project memory links, Stop/SessionStart hooks)
       via memory_uninstall.py. Never touches content it didn't create.
    3. Optionally delinks the store's git remote (git remote remove origin).
       Default: keep it. The store and its history stay on disk either way.
    4. Optionally deletes the store's data entirely, gated behind BOTH a
       [y/N] confirmation AND a typed confirmation (the exact store path, or
       DELETE). Default: keep. Refuses entirely on a non-interactive run.

    Pass -DryRun to preview every step without changing anything: no wrapper
    is removed, memory_uninstall.py runs with --dry-run, and the confirmation
    prompts are described rather than shown.
#>

# [CmdletBinding()] makes PowerShell REJECT unknown parameters instead of
# silently swallowing them into $args. Without it, a Unix-style `-Dry-run`
# (single dash, inner hyphen) matches no parameter -- the hyphen breaks the
# `-Dry` prefix match to -DryRun -- so it was dropped and the script ran FOR
# REAL while the user believed they were previewing. With CmdletBinding it
# fails fast: "A parameter cannot be found that matches parameter name
# 'Dry-run'." and nothing mutates. The `--dry-run` typo is handled separately
# below (it binds to positional -Store, which CmdletBinding leaves intact).
[CmdletBinding()]
param([switch]$DryRun, [switch]$Yes, [string]$Store)

$ErrorActionPreference = "Stop"

# Several steps below PROBE state with native commands that legitimately exit
# non-zero (no remote yet, no unpushed commits). On PowerShell 7.4+ a non-zero
# native exit combined with ErrorActionPreference=Stop is treated as
# terminating and would abort mid-probe (and leak git's stderr). Opt those
# probes out; the variable doesn't exist on 5.1, which never had this
# behavior, so guarding is harmless there.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

# Windows PowerShell 5.1 has a second, distinct trap: with
# $ErrorActionPreference = 'Stop', ANY stderr redirection on a native command
# (2>$null or 2>&1) routes each stderr line through PowerShell's error stream,
# and the FIRST line becomes a terminating NativeCommandError. So a probe that
# legitimately fails ("no upstream configured for branch 'main'") killed the
# script mid-check instead of returning non-zero. Run all git probes through
# this helper, which relaxes EAP for the duration; callers still read
# $LASTEXITCODE as usual (it is a global automatic variable and survives).
function Invoke-GitQuiet {
    param([string[]]$GitArgs)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & git @GitArgs 2>$null
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Confirmation helper. Deliberately does NOT pre-detect interactivity:
# [Console]::IsInputRedirected is $true in some real terminals (ConPTY hosts
# pipe stdin), so `UserInteractive -and -not IsInputRedirected` classified a
# genuine console as non-interactive and the prompts were never shown.
# Read-Host itself is the ground truth: it prompts a human wherever a host UI
# exists, throws under `powershell -NonInteractive` (-> 'unavailable'), and
# returns '' on piped EOF (-> 'no', the safe default). Nothing can hang: EOF
# returns immediately, and only an answer starting with y/Y is a yes.
function Read-Confirm([string]$Prompt) {
    try {
        $ans = Read-Host $Prompt
    } catch {
        return 'unavailable'
    }
    if ($ans -match '^[Yy]') { return 'yes' }
    return 'no'
}

# Guard against Unix-style double-dash flags. A user coming from uninstall.sh
# may type `--dry-run`; PowerShell binds that unrecognized token to the
# positional -Store param, silently leaving -DryRun unset -- so the script
# would run FOR REAL while the user believed it was previewing. A real store
# path never starts with a dash, so treat a dash-prefixed -Store as a typo and
# fail fast BEFORE removing anything.
if ($Store -like '-*') {
    Write-Host "Error: '$Store' is not a valid value for -Store."
    Write-Host "  This is PowerShell -- flags take a SINGLE dash: -DryRun (not --dry-run)."
    Write-Host "  Usage:  .\uninstall.ps1 [-DryRun] [-Store <path>]"
    exit 2
}

Write-Host "=> Uninstalling okfmem..."

# 0. Resolve paths ---------------------------------------------------------
$StoreDir = $Store
if (-not $StoreDir) { $StoreDir = $env:OKFMEM_STORE }
if (-not $StoreDir) { $StoreDir = Join-Path $env:USERPROFILE "okfmem-store" }

$EngineDir = Split-Path -Parent $MyInvocation.MyCommand.Path

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

# 0.5 Up-front confirmation gate --------------------------------------------
# ONE [y/N] before anything is removed. "Uninstall" is a destructive-by-name
# operation: the user must get to decline BEFORE step 1, not after. Covers the
# CLI wrappers and the harness wiring (both restored by re-running
# install.ps1). The store's git remote and data have their own gates below --
# -Yes does NOT reach the data delete.
if (-not $DryRun -and -not $Yes) {
    switch (Read-Confirm "Uninstall okfmem (remove CLI wrappers and harness wiring)? [y/N]") {
        'yes' { }
        'unavailable' {
            Write-Host "=> No interactive prompt available and -Yes not given -- aborting, nothing removed."
            Write-Host "   Re-run in an interactive terminal to be asked, or pass -Yes to run unattended:"
            Write-Host "     .\uninstall.ps1 -Yes"
            exit 0
        }
        default {
            Write-Host "=> Aborted -- nothing removed."
            exit 0
        }
    }
}

# 1. Remove CLI wrappers ----------------------------------------------------
$BinDir = Join-Path $env:USERPROFILE ".local\bin"
$CmdPath = Join-Path $BinDir "okfmem.cmd"
$Ps1Path = Join-Path $BinDir "okfmem.ps1"
$Wrappers = @($CmdPath, $Ps1Path) | Where-Object { Test-Path -LiteralPath $_ }
if ($Wrappers) {
    if ($DryRun) {
        foreach ($w in $Wrappers) { Write-Host "=> [dry-run] would remove $w" }
    } else {
        foreach ($w in $Wrappers) {
            Remove-Item -LiteralPath $w -Force -Confirm:$false
        }
        Write-Host "=> Removed okfmem CLI wrappers from $BinDir"
    }
} else {
    Write-Host "=> No okfmem CLI wrappers found in $BinDir"
}

# 2. Strip okfmem-managed harness wiring ------------------------------------
# Covered by the up-front gate above (rung-2 consent already given -- via
# prompt or explicit -Yes). Reversible by re-running install.ps1, and
# memory_uninstall.py only ever touches wiring okfmem itself created -- it
# skips any real file it didn't write. The store's data and git remote are
# never touched here (gated separately below).
$UninstallPy = Join-Path $EngineDir "memory_uninstall.py"
if ($DryRun) {
    Write-Host "=> [dry-run] would remove harness wiring (pointers, skill links, hooks):"
    & $PyCmd $UninstallPy --dry-run --store "$StoreDir"
} else {
    Write-Host "=> Removing harness wiring (pointers, skill links, hooks)..."
    & $PyCmd $UninstallPy --store "$StoreDir"
}

# 3. Delink the store remote (opt-in, rung-2) -------------------------------
# The store and its full history stay on disk either way -- this only drops
# the `origin` remote pointer. Skip cleanly if there's no remote, or if the
# console isn't interactive (piped/CI run).
$HasRemote = $false
$OriginUrl = $null
if (Test-Path -LiteralPath $StoreDir) {
    $OriginUrl = (Invoke-GitQuiet @('-C', $StoreDir, 'remote', 'get-url', 'origin'))
    if ($LASTEXITCODE -eq 0 -and $OriginUrl) { $HasRemote = $true }
}
if ($HasRemote) {
    if ($DryRun) {
        Write-Host "=> [dry-run] store has a remote ($OriginUrl) -- would offer to delink (default: keep)."
    } elseif ((Read-Confirm "Store has a remote ($OriginUrl). Delink it (git remote remove origin)? [y/N]") -eq 'yes') {
        Invoke-GitQuiet @('-C', $StoreDir, 'remote', 'remove', 'origin') | Out-Null
        Write-Host "=> Remote delinked. Store data is untouched."
    } else {
        # Declined, or no prompt was available ('no'/'unavailable' both keep).
        Write-Host "   Skipped. Delink later with:"
        Write-Host "     git -C $StoreDir remote remove origin"
    }
} else {
    if (Test-Path -LiteralPath $StoreDir) {
        Write-Host "=> Store has no remote to delink."
    }
}

# 4. Full data delete (opt-in, double-guarded, rung-3) ----------------------
# Default is KEEP. Refuses entirely on a non-interactive console. Requires
# BOTH an initial [y/N] AND a typed confirmation (the exact store path, or
# "DELETE") before touching anything -- Remove-Item -Recurse is gated by the
# typed confirm below, never by a bare -Force alone.
if (-not (Test-Path -LiteralPath $StoreDir)) {
    Write-Host "=> No store found at $StoreDir -- nothing to delete."
} elseif ($DryRun) {
    Write-Host "=> [dry-run] would offer to delete all data at $StoreDir (double-confirm; default: keep)."
} else {
    Write-Host ""
    Write-Host "Store data lives at: $StoreDir"

    # Pending-work checks so the user sees what deleting would actually lose.
    # Three independent ways a store can hold data that exists nowhere else:
    #   (a) committed but unpushed  -> rev-list @{u}..HEAD
    #   (b) commits but NO upstream -> nothing is backed up at all
    #   (c) uncommitted / untracked -> dirty working tree (unsaved edits)
    # (a) alone was checked before; (b) and (c) could be lost silently.
    $LossWarning = $false
    Invoke-GitQuiet @('-C', $StoreDir, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}') | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $countOut = (Invoke-GitQuiet @('-C', $StoreDir, 'rev-list', '--count', '@{u}..HEAD'))
        if ($LASTEXITCODE -eq 0 -and $countOut) {
            $UnpushedCount = [int]("$countOut".Trim())
            if ($UnpushedCount -gt 0) {
                Write-Host "Warning: $UnpushedCount commit(s) not yet pushed to the remote."
                $LossWarning = $true
            }
        }
    } else {
        Invoke-GitQuiet @('-C', $StoreDir, 'rev-parse', 'HEAD') | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "Warning: store has commits but no upstream remote -- nothing is backed up."
            $LossWarning = $true
        }
    }
    $dirty = (Invoke-GitQuiet @('-C', $StoreDir, 'status', '--porcelain'))
    if ($dirty) {
        Write-Host "Warning: store has uncommitted or untracked changes (unsaved memory edits)."
        $LossWarning = $true
    }
    if ($LossWarning) {
        Write-Host "  Deleting now would lose that memory data with no backup."
    }

    # ${} braces required: `?` is a legal variable-name char in Windows
    # PowerShell 5.1, so "$StoreDir?" reads the undefined variable `StoreDir?`
    # and the prompt printed with no path at all.
    switch (Read-Confirm "Delete ALL memory data at ${StoreDir}? [y/N]") {
        'yes' {
            # Second, typed rung-3 confirmation. If the host can't prompt here
            # (throws), that is a refusal -- data stays.
            $confirm = $null
            try {
                $confirm = Read-Host "Type the store path (or DELETE) to confirm"
            } catch {
                $confirm = $null
            }
            if ($confirm -eq $StoreDir -or $confirm -ceq "DELETE") {
                Remove-Item -Recurse -Force -Confirm:$false -LiteralPath $StoreDir
                Write-Host "=> Deleted $StoreDir."
            } else {
                Write-Host "=> Confirmation did not match -- aborted. Data intact."
            }
        }
        'unavailable' {
            Write-Host "=> Store data at $StoreDir was NOT deleted (no prompt available)."
            Write-Host "   Delete it yourself if you're sure:  Remove-Item -Recurse -Force `"$StoreDir`""
        }
        default {
            Write-Host "=> Skipped. Data intact at $StoreDir."
        }
    }
}

Write-Host ""
if ($DryRun) {
    Write-Host "[dry-run] no changes made. Re-run without -DryRun to apply."
} else {
    Write-Host "okfmem uninstalled."
}
Write-Host ""
Write-Host "The engine clone itself ($EngineDir) is untouched -- delete it by hand"
Write-Host "if you no longer need it."
if (Test-Path -LiteralPath $StoreDir) {
    Write-Host "Your memory data is still at $StoreDir."
}
$PathDirs = $env:Path -split ";"
if ($PathDirs -contains $BinDir) {
    Write-Host "($BinDir is still on your PATH -- harmless now that okfmem is removed.)"
}

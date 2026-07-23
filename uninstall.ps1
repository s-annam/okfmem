#Requires -Version 5.1
<#
.SYNOPSIS
    Native Windows uninstaller for okfmem (mirrors uninstall.sh's intent for
    native PowerShell/cmd -- no WSL, no Git Bash required).

.DESCRIPTION
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
    is removed, memory_uninstall.py runs with --dry-run, and both opt-in
    prompts are described rather than shown.
#>

param([switch]$DryRun, [string]$Store)

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

# "Interactive" for the gated prompts below means BOTH a real interactive
# desktop session AND a non-redirected stdin -- the PowerShell analogue of
# uninstall.sh's `[ -t 0 ]`. UserInteractive alone is $true on GitHub-hosted
# Windows runners, so a piped/CI run would otherwise fall through to Read-Host
# and hang; IsInputRedirected catches that and takes the documented skip.
$Interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

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
# Running this uninstaller IS the user's consent to remove what okfmem itself
# wired. Never touches user content it didn't create, and never touches the
# store's data or git remote (gated separately below).
Write-Host "=> Removing harness wiring (pointers, skill links, hooks)..."
if ($DryRun) {
    & $PyCmd (Join-Path $EngineDir "memory_uninstall.py") --dry-run --store "$StoreDir"
} else {
    & $PyCmd (Join-Path $EngineDir "memory_uninstall.py") --store "$StoreDir"
}

# 3. Delink the store remote (opt-in, rung-2) -------------------------------
# The store and its full history stay on disk either way -- this only drops
# the `origin` remote pointer. Skip cleanly if there's no remote, or if the
# console isn't interactive (piped/CI run).
$HasRemote = $false
$OriginUrl = $null
if (Test-Path -LiteralPath $StoreDir) {
    $OriginUrl = (git -C "$StoreDir" remote get-url origin 2>$null)
    if ($LASTEXITCODE -eq 0 -and $OriginUrl) { $HasRemote = $true }
}
if ($HasRemote) {
    if ($DryRun) {
        Write-Host "=> [dry-run] store has a remote ($OriginUrl) -- would offer to delink (default: keep)."
    } elseif ($Interactive) {
        $ans = Read-Host "Store has a remote ($OriginUrl). Delink it (git remote remove origin)? [y/N]"
        if ($ans -match '^[Yy]') {
            git -C "$StoreDir" remote remove origin 2>&1 | Out-Null
            Write-Host "=> Remote delinked. Store data is untouched."
        } else {
            Write-Host "   Skipped. Delink later with:"
            Write-Host "     git -C $StoreDir remote remove origin"
        }
    } else {
        Write-Host "=> Store has a remote ($OriginUrl) -- skipped (non-interactive)."
        Write-Host "   Delink later with:  git -C $StoreDir remote remove origin"
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
} elseif (-not $Interactive) {
    Write-Host "=> Store data at $StoreDir was NOT deleted (non-interactive run)."
    Write-Host "   Delete it yourself if you're sure:  Remove-Item -Recurse -Force `"$StoreDir`""
} else {
    Write-Host ""
    Write-Host "Store data lives at: $StoreDir"

    $UnpushedWarning = $false
    $UnpushedCount = 0
    git -C "$StoreDir" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $countOut = (git -C "$StoreDir" rev-list --count '@{u}..HEAD' 2>$null)
        if ($LASTEXITCODE -eq 0 -and $countOut) {
            $UnpushedCount = [int]($countOut.Trim())
            if ($UnpushedCount -gt 0) { $UnpushedWarning = $true }
        }
    }
    if ($UnpushedWarning) {
        Write-Host "Warning: $StoreDir has $UnpushedCount unpushed commit(s)."
        Write-Host "  Deleting now would lose that memory data with no backup."
    }

    $ans = Read-Host "Delete ALL memory data at $StoreDir? [y/N]"
    if ($ans -match '^[Yy]') {
        $confirm = Read-Host "Type the store path (or DELETE) to confirm"
        if ($confirm -eq $StoreDir -or $confirm -ceq "DELETE") {
            Remove-Item -Recurse -Force -Confirm:$false -LiteralPath $StoreDir
            Write-Host "=> Deleted $StoreDir."
        } else {
            Write-Host "=> Confirmation did not match -- aborted. Data intact."
        }
    } else {
        Write-Host "=> Skipped. Data intact at $StoreDir."
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

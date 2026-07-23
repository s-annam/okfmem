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
    5. Optionally links (or creates) a private GitHub remote for the store,
       pulling existing content down when the local store is empty.
    6. Prints PATH guidance (never silently mutates PATH).
#>

$ErrorActionPreference = "Stop"

# Several steps below PROBE state with native commands that legitimately exit
# non-zero (no remote yet, gh not logged in, empty repo). On PowerShell 7.4+ a
# non-zero native exit combined with ErrorActionPreference=Stop is treated as
# terminating and would abort the installer mid-probe (and leak git's stderr).
# Opt those probes out; the variable doesn't exist on 5.1, which never had this
# behavior, so guarding is harmless there.
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

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

$StoreCreated = $false
if (-not (Test-Path $StoreDir)) {
    Write-Host "=> Creating local data store at $StoreDir"
    New-Item -ItemType Directory -Force -Path $StoreDir | Out-Null
    git -C "$StoreDir" init -q
    $StoreCreated = $true
} else {
    Write-Host "=> Found existing store at $StoreDir"
}

# The engine's backfill/init/status commands all require a projects/ dir under
# the store (they exit non-zero with "no projects/ under ..." otherwise). A
# fresh `git init` store has none, so create it here -- idempotent for an
# existing store.
New-Item -ItemType Directory -Force -Path (Join-Path $StoreDir "projects") | Out-Null

# NOTE (#27): a fresh `git init` store also has no `.gitignore` -- that is
# NOT fixed up here. `memory_init.py --yes` below creates/maintains the
# store's `.gitignore` (managed block: `.okfmem-sync.lock`, `*.db`,
# `__pycache__/`, `*.pyc`, `.DS_Store`) as one of its own steps, unconditionally
# and before this script (or anything else) ever calls `okfmem sync`.

# 3. Wire it up -------------------------------------------------------------
Write-Host "=> Running backfill and initialization..."
& $PyCmd (Join-Path $EngineDir "memory_backfill.py")
# memory_init's per-project memory link resolves the CURRENT repo from the
# process cwd (git rev-parse). This installer may be launched from anywhere
# (e.g. the user's home dir), so run init FROM the engine clone -- otherwise
# the link step sees no git repo and skips. Push/Pop guarantees we restore cwd.
Push-Location $EngineDir
try {
    # --yes: running this installer IS the user's consent to wire hooks +
    # create the skill/memory links, so init applies them without re-prompting.
    # (The outward GitHub-remote step below is still gated separately.)
    & $PyCmd (Join-Path $EngineDir "memory_init.py") --yes
} finally {
    Pop-Location
}

# 4. Optional: private GitHub remote for the store ------------------------
# Both linking an existing repo and creating a new one are outward-facing, so we
# only act after an explicit "yes" and only on an interactive console (skip
# cleanly when piped). Every path prints the exact manual fallback.
function Write-ManualRemoteHint {
    param([string]$StoreDir)
    Write-Host "   Skipped. Add one later with:"
    Write-Host "     git -C $StoreDir remote add origin <url>"
}

# Link an already-existing remote store and, if the local store is empty, pull
# its content down -- the returning-user case: the repo lives on GitHub already
# and the fresh local store just needs to adopt it.
function Link-ExistingStoreRemote {
    param([string]$StoreDir, [string]$Url)

    if ((git -C "$StoreDir" remote 2>$null) -contains "origin") {
        git -C "$StoreDir" remote set-url origin "$Url"
    } else {
        git -C "$StoreDir" remote add origin "$Url"
    }
    Write-Host "=> Linked origin -> $Url; fetching..."
    git -C "$StoreDir" fetch origin
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: fetch failed -- remote is linked but nothing was pulled."
        Write-Host "  Check your access, then:  git -C $StoreDir pull"
        return
    }

    # Remote's default branch (main/master) -- don't assume.
    $def = (git -C "$StoreDir" remote show origin 2>$null |
            Select-String 'HEAD branch:' |
            ForEach-Object { ($_ -replace '.*HEAD branch:\s*', '').Trim() } |
            Select-Object -First 1)
    if (-not $def) { $def = "main" }

    git -C "$StoreDir" rev-parse --verify --quiet HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        # Local store already has its own commits -- never clobber them.
        Write-Host "=> Remote linked as origin. Your local store already has commits;"
        Write-Host "   reconcile when ready:  git -C $StoreDir pull --rebase origin $def"
        return
    }
    # Empty local store: adopt the remote wholesale. -f overwrites the fresh
    # stubs the installer just wrote (registry.json, projects/) -- the remote is
    # authoritative for a store the user is choosing to pull down.
    # Merge stderr into stdout with 2>&1 and capture it: git writes "Switched to
    # a new branch" to stderr even on SUCCESS, and a bare 2>$null still surfaces
    # it as a red NativeCommandError under ErrorActionPreference=Stop.
    $checkout = (git -C "$StoreDir" checkout -f -B $def "origin/$def" 2>&1)
    if ($LASTEXITCODE -eq 0) {
        Write-Host "=> Pulled your memory store from $Url (branch $def)."
    } else {
        Write-Host "Error: couldn't check out origin/$def cleanly (local files in the way)."
        Write-Host "  $checkout"
        Write-Host "  Remote is linked; finish by hand with:  git -C $StoreDir checkout $def"
    }
}

function Set-StoreRemote {
    param([string]$StoreDir)

    # `git remote` exits 0 and prints nothing when there's no remote -- unlike
    # `git remote get-url origin`, which exits non-zero and writes to stderr.
    if ((git -C "$StoreDir" remote 2>$null) -contains "origin") {
        $existing = (git -C "$StoreDir" remote get-url origin 2>$null)
        Write-Host "=> Store already has a remote: $existing"
        return
    }

    if (-not [Environment]::UserInteractive) { return }

    # Without gh we can't detect or create a repo, but the user may already have
    # one -- let them paste its URL so a returning user isn't stuck.
    if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
        $ans = Read-Host "Link a GitHub remote for your store now? (you'll paste its URL) [y/N]"
        if ($ans -notmatch '^[Yy]') { Write-ManualRemoteHint $StoreDir; return }
        $url = Read-Host "  Remote URL (git@... or https://...)"
        if (-not $url) { Write-ManualRemoteHint $StoreDir; return }
        Link-ExistingStoreRemote $StoreDir $url
        return
    }

    gh auth status 2>$null | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: 'gh' is installed but not authenticated."
        Write-Host "  Run:  gh auth login  then re-run the installer, or add a remote by hand:"
        Write-Host "     git -C $StoreDir remote add origin <url>"
        return
    }

    # gh is present and authed: does an okfmem-store repo already exist? If so,
    # offer to link + pull it rather than trying (and failing) to create a dup.
    $existing = (gh repo view okfmem-store --json url --jq .url 2>$null)
    if ($LASTEXITCODE -eq 0 -and $existing) {
        Write-Host "Found an existing private GitHub store:"
        Write-Host "    $existing"
        $ans = Read-Host "Link it and pull your memory into $StoreDir now? [Y/n]"
        if ($ans -match '^[Nn]') { Write-ManualRemoteHint $StoreDir; return }
        Link-ExistingStoreRemote $StoreDir $existing
        return
    }

    $ans = Read-Host "No GitHub store found. Create a PRIVATE 'okfmem-store' repo now? [y/N]"
    if ($ans -notmatch '^[Yy]') { Write-ManualRemoteHint $StoreDir; return }
    Write-Host "=> Creating private repo 'okfmem-store' and wiring it as origin..."
    gh repo create okfmem-store --private --source "$StoreDir" --remote origin
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Error: 'gh repo create' failed (see message above)."
        Write-Host "  Add your existing repo as a remote by hand:"
        Write-Host "     git -C $StoreDir remote add origin <url>"
        return
    }

    # Push only if the store already has a commit to push. Capture stderr (git
    # prints progress + "branch set up to track" there on success) so it doesn't
    # surface as a red NativeCommandError under ErrorActionPreference=Stop.
    git -C "$StoreDir" rev-parse --verify --quiet HEAD 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) {
        $branch = (git -C "$StoreDir" branch --show-current)
        $push = (git -C "$StoreDir" push -u origin $branch 2>&1)
        if ($LASTEXITCODE -ne 0) { Write-Host "  Push failed: $push" }
    }
    Write-Host "=> Store remote ready."
}
# Offer remote setup for a store we just created OR one that exists but is empty
# (no commits) -- the latter is the returning-user case whose content lives on
# GitHub and needs linking. An established local store WITH commits and no remote
# is left alone (deliberately local-only); it got the `git remote add` hint above.
git -C "$StoreDir" rev-parse --verify --quiet HEAD 2>$null | Out-Null
$StoreEmpty = ($LASTEXITCODE -ne 0)
if ($StoreCreated -or $StoreEmpty) { Set-StoreRemote $StoreDir }

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
Write-Host "3. The consolidation Stop hook was wired into Claude Code automatically"
Write-Host "   (see the 'stop hook' line above -- nothing to paste). For OTHER"
Write-Host "   agents, the hook snippet is in README.md."

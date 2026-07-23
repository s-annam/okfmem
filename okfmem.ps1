# Native PowerShell wrapper for okfmem dispatcher on Windows
$PyCmd = $null
foreach ($cand in @("py", "python")) {
    if (Get-Command $cand -ErrorAction SilentlyContinue) {
        $PyCmd = $cand
        break
    }
}
if (-not $PyCmd) {
    Write-Error "Error: neither 'py' nor 'python' is on PATH."
    exit 1
}

$OkfmemPy = Join-Path $PSScriptRoot "okfmem"
& $PyCmd $OkfmemPy @args

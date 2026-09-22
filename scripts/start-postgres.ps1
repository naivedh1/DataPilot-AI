<#
.SYNOPSIS
    Start the local PostgreSQL cluster DataPilot AI runs against.

.DESCRIPTION
    The development cluster is a portable PostgreSQL build, not a Windows
    service, so it does not come back after a reboot. This starts it and
    reports what it found.

    Why portable rather than installed: Docker Desktop needs WSL2, and on the
    original development machine the Windows servicing stack could not enable
    the required optional features (DISM error 193 / 0x800700C1). The EDB
    installer was equally unavailable — get.enterprisedb.com returns 403 to
    non-browser clients on that network. docs/LOCAL_POSTGRES.md has the full
    account.

.PARAMETER Stop
    Stop the cluster instead of starting it.

.EXAMPLE
    .\scripts\start-postgres.ps1
    .\scripts\start-postgres.ps1 -Stop
#>
[CmdletBinding()]
param(
    [switch]$Stop,
    [string]$PgRoot = "$env:USERPROFILE\pgsql17"
)

$ErrorActionPreference = 'Stop'

$pgCtl = Join-Path $PgRoot 'bin\pg_ctl.exe'
$dataDir = Join-Path $PgRoot 'data'
$logFile = Join-Path $PgRoot 'server.log'

if (-not (Test-Path $pgCtl)) {
    Write-Error @"
No PostgreSQL cluster at $PgRoot.

Expected pg_ctl.exe at: $pgCtl
See docs/LOCAL_POSTGRES.md to create the cluster, then run this again.
"@
}

if ($Stop) {
    & $pgCtl -D $dataDir -m fast stop
    exit $LASTEXITCODE
}

# `pg_ctl status` exits non-zero when nothing is running, which is not an
# error here — it is the normal case this script exists to handle.
$running = $false
try {
    & $pgCtl -D $dataDir status *> $null
    $running = ($LASTEXITCODE -eq 0)
} catch {
    $running = $false
}

if ($running) {
    Write-Output "PostgreSQL is already running."
} else {
    # No -w: pg_ctl must detach cleanly. Starting it inside a shell that is
    # later killed leaves the postmaster alive but its backends broken, which
    # presents as "connects, then immediately drops" with
    # STATUS_DLL_INIT_FAILED. See docs/LOCAL_POSTGRES.md.
    & $pgCtl -D $dataDir -l $logFile -o "-p 5432" start
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Failed to start. Check $logFile"
    }
}

& $pgCtl -D $dataDir status

Write-Output ""
Write-Output "Next:"
Write-Output "  cd backend"
Write-Output "  .\.venv\Scripts\python.exe -m pytest"

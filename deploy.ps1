<#
.SYNOPSIS
  Puts the committed and pushed state of host\ on martha, with a config backup per commit.

.DESCRIPTION
  Uses the OpenSSH client that ships with Windows (ssh.exe/scp.exe), LAN only. Steps:
    1. refuses with uncommitted or unpushed changes, and takes the short hash of HEAD;
    2. reads the commit of the previous deploy on martha (/var/lib/martha/deployed) and picks,
       from the diff since then, whether the database is backed up too (host/db or a setup
       script changed) and which setup scripts run;
    3. copies only host\ with scp to ~/martha-deploy on martha (not offline\);
    4. runs host/deploy.sh there with sudo: martha-ha backup <commit> (and backup-db),
       install.sh --host-only, the setup scripts, and on errors the config of that backup back;
    5. removes ~/martha-deploy again.
  sudo asks for its password in the session. A full install or an update of Docker, Home
  Assistant or TimescaleDB goes through 2. ssh\install-ssh.ps1 instead.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File .\deploy.ps1
  powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Setup setup_heat
  powershell -ExecutionPolicy Bypass -File .\deploy.ps1 -Setup none
#>
[CmdletBinding()]
param(
    [string]$Target = 'david@martha.local',
    [int]$Port = 22,
    [string]$IdentityFile,
    # Setup scripts to run instead of the ones picked from the diff: setup_p1, setup_solar,
    # setup_solar_inverter, setup_heat, dashboards, or none.
    [string[]]$Setup
)

$ErrorActionPreference = 'Stop'
$RepoRoot = $PSScriptRoot
$RemoteDir = 'martha-deploy'   # relative to the remote home; must not contain spaces
$Steps = @('setup_p1', 'setup_solar', 'setup_solar_inverter', 'setup_heat', 'dashboards')

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if ($Target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$') { throw "Target must look like user@host, got: $Target" }
$ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
$scp = (Get-Command scp.exe -ErrorAction SilentlyContinue).Source
if (-not $ssh -or -not $scp) { throw 'ssh.exe/scp.exe not found. Enable Windows "OpenSSH Client" (Settings > System > Optional features).' }
$sshOpts = @('-p', $Port)
$scpOpts = @('-P', $Port)
if ($IdentityFile) { $sshOpts += @('-i', $IdentityFile); $scpOpts += @('-i', $IdentityFile) }

function Invoke-Git {
    $out = & git -C $RepoRoot @args
    if ($LASTEXITCODE -ne 0) { throw "git $args failed (exit $LASTEXITCODE)" }
    $out
}

# Remote commands avoid double quotes: Windows PowerShell 5.1 mangles them when calling native programs.
function Invoke-Remote([string]$command, [switch]$Tty) {
    # -n: only the session with a terminal (sudo's password prompt) reads stdin.
    $opts = @('-n') + $sshOpts
    if ($Tty) { $opts = @('-tt') + $sshOpts }
    & $ssh @opts $Target $command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed (exit $LASTEXITCODE): $command" }
}

# --- 1. Only what is committed and pushed ----------------------------------------------------
Write-Step 'Check the repository'
if (Invoke-Git status --porcelain) { throw 'Uncommitted changes; commit and push first.' }
$branch = Invoke-Git rev-parse --abbrev-ref HEAD
Invoke-Git fetch --quiet origin
$unpushed = Invoke-Git rev-list "origin/$branch..HEAD"
if ($unpushed) { throw "HEAD has commits that are not on origin/$branch; push first." }
$commit = Invoke-Git rev-parse --short=10 HEAD
Write-Host "    commit $commit ($(Invoke-Git log -1 --format=%s))"

# --- 2. What changed since the previous deploy ----------------------------------------------
$info = @(& $ssh -n @sshOpts $Target 'cat /var/lib/martha/deployed 2>/dev/null; echo; test -f /etc/martha/grott.ini && echo inverter; true')
if ($LASTEXITCODE -ne 0) { throw "Cannot reach $Target (exit $LASTEXITCODE)" }
$previous = "$($info[0])".Trim()
$inverter = $info -contains 'inverter'

$db = 1
$picked = @()
$known = $false
if ($previous) {
    & git -C $RepoRoot cat-file -e "$previous^{commit}" 2>$null
    $known = ($LASTEXITCODE -eq 0)
}
if ($known) {
    $changed = @(Invoke-Git diff --name-only $previous HEAD -- host)
    Write-Host "    previous deploy $previous; $($changed.Count) file(s) changed in host/"
    $db = [int][bool]($changed | Where-Object { $_ -like 'host/db/*' -or $_ -like 'host/ha/setup_*.py' })
    if ($changed | Where-Object { $_ -eq 'host/ha/setup_p1.py' }) { $picked += 'setup_p1' }
    if ($changed | Where-Object { $_ -in 'host/ha/setup_solar.py', 'host/db/solar.sql' -or $_ -like 'host/ha/packages/solar*' -or $_ -like 'host/grott/*' }) {
        $picked += $(if ($inverter) { 'setup_solar_inverter' } else { 'setup_solar' })
    }
    if ($changed | Where-Object { $_ -in 'host/ha/setup_heat.py', 'host/ha/packages/heat.yaml' -or $_ -like 'host/db/heat*' }) { $picked += 'setup_heat' }
    # Every setup script ends with the dashboards; on their own only when nothing else runs.
    if (-not $picked -and ($changed | Where-Object { $_ -in 'host/ha/dashboards.py', 'host/ha/setup_thermostat.py' -or $_ -like 'host/ha/www/*' })) { $picked += 'dashboards' }
} else {
    Write-Host "    previous deploy unknown ($(if ($previous) { $previous } else { 'none' })): database backup too, no setup scripts unless -Setup"
}
if ($Setup) {
    $picked = @($Setup | Where-Object { $_ -ne 'none' })
    foreach ($s in $picked) { if ($s -notin $Steps) { throw "Unknown setup step $s (one of: $($Steps -join ', '), none)" } }
}
Write-Host "    database backup: $(if ($db) { 'yes' } else { 'no' }); setup: $(if ($picked) { $picked -join ', ' } else { 'none' })"

# --- 3. Copy host\ --------------------------------------------------------------------------
Write-Step "Copy host\ to ${Target}:~/$RemoteDir"
Invoke-Remote "rm -rf ~/$RemoteDir; mkdir -p ~/$RemoteDir"
& $scp @scpOpts -q -r (Join-Path $RepoRoot 'host') "${Target}:$RemoteDir/"
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE)" }

# --- 4. Backup, install, setup --------------------------------------------------------------
Write-Step 'Deploy on martha (sudo asks for your password)'
try {
    Invoke-Remote "sudo bash ~/$RemoteDir/host/deploy.sh $commit $db $($picked -join ' ')" -Tty
} finally {
    Write-Step "Remove ~/$RemoteDir on martha"
    & $ssh -n @sshOpts $Target "rm -rf ~/$RemoteDir"
}
Write-Step "Done: $commit is on martha"

<#
.SYNOPSIS
  Route B over the network: copies the offline bundle to an Ubuntu 26.04 server via SSH and runs install.sh there.

.DESCRIPTION
  Uses the OpenSSH client that ships with Windows (ssh.exe/scp.exe). Only needs a LAN
  connection to the server, no internet. Steps:
    1. check over SSH that the server runs Ubuntu 26.04 (before copying ~750 MB)
    2. scp host\ and offline\ to ~/martha-bundle on the server
    3. run 'sudo bash ~/martha-bundle/host/install.sh' in an interactive session (sudo may ask for a password)
    4. remove ~/martha-bundle again (unless -KeepBundle)
  Without an SSH key you are asked for the SSH password once per step; with a key
  (e.g. via make-usb.ps1 -SshKeyFile) only sudo asks.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ".\2. ssh\install-ssh.ps1" -Target david@192.168.1.50
  powershell -ExecutionPolicy Bypass -File ".\2. ssh\install-ssh.ps1" -Target david@martha.local -IdentityFile $HOME\.ssh\id_ed25519
#>
[CmdletBinding()]
param(
    # user@host of the server; the user needs sudo rights.
    [Parameter(Mandatory)][string]$Target,
    [int]$Port = 22,
    [string]$IdentityFile,
    # Leave ~/martha-bundle on the server after a successful install.
    [switch]$KeepBundle
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$RemoteDir = 'martha-bundle'   # relative to the remote home; must not contain spaces

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

if ($Target -notmatch '^[A-Za-z0-9._-]+@[A-Za-z0-9._-]+$') { throw "Target must look like user@host, got: $Target" }
$bundleEnv = Join-Path $RepoRoot 'offline\bundle.env'
if (-not (Test-Path -LiteralPath $bundleEnv)) { throw "No offline bundle; run '1. download\download.ps1' first." }
$suite = ((Get-Content -LiteralPath $bundleEnv | Where-Object { $_ -match '^UBUNTU_SUITES=' }) -replace '^UBUNTU_SUITES="?(\S+).*$', '$1')

$ssh = (Get-Command ssh.exe -ErrorAction SilentlyContinue).Source
$scp = (Get-Command scp.exe -ErrorAction SilentlyContinue).Source
if (-not $ssh -or -not $scp) { throw 'ssh.exe/scp.exe not found. Enable Windows "OpenSSH Client" (Settings > System > Optional features).' }

$sshOpts = @('-p', $Port)
$scpOpts = @('-P', $Port)
if ($IdentityFile) { $sshOpts += @('-i', $IdentityFile); $scpOpts += @('-i', $IdentityFile) }

# Remote commands avoid double quotes: Windows PowerShell 5.1 mangles them when calling native programs.
function Invoke-Remote([string]$command, [switch]$Tty) {
    $opts = $sshOpts
    if ($Tty) { $opts = @('-t') + $opts }
    & $ssh @opts $Target $command
    if ($LASTEXITCODE -ne 0) { throw "Remote command failed (exit $LASTEXITCODE): $command" }
}

Write-Step "Check $Target"
Invoke-Remote ". /etc/os-release; echo `$PRETTY_NAME; [ `$VERSION_CODENAME = $suite ] || { echo 'Bundle is for Ubuntu $suite' >&2; exit 3; }; [ `$(uname -m) = x86_64 ] || { echo 'Bundle is for amd64' >&2; exit 3; }; rm -rf ~/$RemoteDir; mkdir -p ~/$RemoteDir"

$size = (Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'offline'), (Join-Path $RepoRoot 'host') -Recurse -File | Measure-Object Length -Sum).Sum
Write-Step ("Copy bundle ({0:N0} MB) to {1}:~/{2}" -f ($size / 1MB), $Target, $RemoteDir)
& $scp @scpOpts -r (Join-Path $RepoRoot 'host') (Join-Path $RepoRoot 'offline') "${Target}:$RemoteDir/"
if ($LASTEXITCODE -ne 0) { throw "scp failed (exit $LASTEXITCODE)" }

Write-Step 'Run install.sh on the server'
Invoke-Remote "sudo bash ~/$RemoteDir/host/install.sh" -Tty

if (-not $KeepBundle) {
    Write-Step "Remove ~/$RemoteDir on the server"
    Invoke-Remote "rm -rf ~/$RemoteDir"
}
Write-Step "Done. Open http://$(($Target -split '@')[1]):8123"

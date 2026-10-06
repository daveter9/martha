<#
.SYNOPSIS
  Puts the offline bundle on a USB stick, plus autoinstall.yaml for route A.

.DESCRIPTION
  Route A (autoinstall, default): run after writing "0. install os\ubuntu-*-live-server-amd64.iso"
  to the stick with Rufus in ISO mode (see readme.md). Asks for the password of the admin
  account on the target PC, hashes it (SHA-512 crypt via the openssl that ships with Git for
  Windows) and writes <Drive>\autoinstall.yaml plus <Drive>\martha\{host,offline}.

  Route B (-BundleOnly, existing server): only writes <Drive>\martha\{host,offline}, to any stick.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E:
  powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E: -SshKeyFile $HOME\.ssh\id_ed25519.pub
  powershell -ExecutionPolicy Bypass -File ".\2. usb\make-usb.ps1" -Drive E: -BundleOnly
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Drive,
    [string]$Hostname = 'martha',
    [string]$Username = 'david',
    # Public key(s) allowed to log in over SSH. Without a key, SSH password login is enabled.
    [string]$SshKeyFile,
    [string]$Timezone = 'Europe/Amsterdam',
    [string]$Locale = 'en_US.UTF-8',
    [string]$Keyboard = 'us',
    # Route B: only copy the bundle, no autoinstall.
    [switch]$BundleOnly
)

$ErrorActionPreference = 'Stop'
$RepoRoot = Split-Path -Parent $PSScriptRoot
$Usb = (Resolve-Path -LiteralPath ($Drive.TrimEnd('\') + '\')).Path

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- Sanity checks ----------------------------------------------------------
$bundleEnv = Join-Path $RepoRoot 'offline\bundle.env'
if (-not (Test-Path -LiteralPath $bundleEnv)) { throw "No offline bundle; run '1. download\download.ps1' first." }

$vol = Get-Volume -DriveLetter $Usb[0]
if ($vol.FileSystem -notin 'FAT32', 'exFAT', 'NTFS') { Write-Warning "Unexpected filesystem $($vol.FileSystem) on $Usb" }
$need = (Get-ChildItem -LiteralPath (Join-Path $RepoRoot 'offline'), (Join-Path $RepoRoot 'host') -Recurse -File | Measure-Object Length -Sum).Sum
$old = (Get-ChildItem -LiteralPath (Join-Path $Usb 'martha') -Recurse -File -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
$have = $vol.SizeRemaining + [long]$old
if ($have -lt $need + 50MB) { throw ("Not enough space on {0}: need {1:N0} MB, free {2:N0} MB" -f $Usb, ($need / 1MB), ($have / 1MB)) }

if ($BundleOnly) {
    if (Test-Path -LiteralPath (Join-Path $Usb 'autoinstall.yaml')) {
        Write-Warning "${Usb}autoinstall.yaml exists: booting from this stick starts the autoinstall (route A)."
    }
} else {
    $info = Join-Path $Usb '.disk\info'
    if (-not (Test-Path -LiteralPath (Join-Path $Usb 'casper')) -or -not (Test-Path -LiteralPath $info)) {
        throw "$Usb does not look like a Rufus-written Ubuntu installer (no casper\ or .disk\info)."
    }
    $diskInfo = Get-Content -LiteralPath $info -Raw
    if ($diskInfo -notmatch 'Ubuntu-Server 26\.04') { throw "Expected Ubuntu Server 26.04 on $Usb, found: $diskInfo" }
    if ($Hostname -notmatch '^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$') { throw "Invalid hostname: $Hostname" }
    if ($Username -notmatch '^[a-z_][a-z0-9_-]{0,31}$') { throw "Invalid username: $Username" }

    # --- Credentials -----------------------------------------------------------
    $openssl = @((Get-Command openssl.exe -ErrorAction SilentlyContinue).Source,
                 "$env:ProgramFiles\Git\usr\bin\openssl.exe",
                 "$env:ProgramFiles\Git\mingw64\bin\openssl.exe") | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $openssl) { throw 'openssl.exe not found (it ships with Git for Windows).' }

    function Read-Plain([string]$prompt) {
        $s = Read-Host -AsSecureString $prompt
        $b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s)
        try { [Runtime.InteropServices.Marshal]::PtrToStringBSTR($b) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($b) }
    }
    do {
        $pw = Read-Plain "Password for '$Username' on $Hostname"
        $pw2 = Read-Plain 'Repeat password'
        if ($pw -ne $pw2) { Write-Warning 'Passwords do not match.' } elseif ($pw.Length -lt 8) { Write-Warning 'Use at least 8 characters.' }
    } until ($pw -eq $pw2 -and $pw.Length -ge 8)

    # Feed the password over stdin, so it never appears on a command line. The stdin writer
    # takes [Console]::InputEncoding and writes its BOM at process start, so the hash would
    # include it: switch to BOM-less UTF-8 while starting openssl.
    $psi = New-Object Diagnostics.ProcessStartInfo($openssl, 'passwd -6 -stdin')
    $psi.UseShellExecute = $false; $psi.RedirectStandardInput = $true; $psi.RedirectStandardOutput = $true
    $prevEncoding = [Console]::InputEncoding
    try {
        [Console]::InputEncoding = New-Object Text.UTF8Encoding($false)
        $p = [Diagnostics.Process]::Start($psi)
    } finally { [Console]::InputEncoding = $prevEncoding }
    $p.StandardInput.NewLine = "`n"; $p.StandardInput.WriteLine($pw); $p.StandardInput.Close()
    $hash = $p.StandardOutput.ReadToEnd().Trim(); $p.WaitForExit()
    $pw = $null; $pw2 = $null
    if ($p.ExitCode -ne 0 -or $hash -notmatch '^\$6\$') { throw 'Hashing the password failed.' }

    $keys = @()
    if ($SshKeyFile) {
        $keys = @(Get-Content -LiteralPath $SshKeyFile | Where-Object { $_ -match '^(ssh-|ecdsa-|sk-)' })
        if (-not $keys) { throw "No public keys in $SshKeyFile" }
    }
    $keysYaml = if ($keys) { '[' + (($keys | ForEach-Object { "'" + ($_.Trim() -replace "'", "''") + "'" }) -join ', ') + ']' } else { '[]' }

    # --- autoinstall.yaml ----------------------------------------------------------
    Write-Step "Write ${Usb}autoinstall.yaml"
    $yaml = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'autoinstall.template.yaml') -Raw
    $values = @{
        __LOCALE__ = $Locale; __KEYBOARD__ = $Keyboard; __TIMEZONE__ = $Timezone
        __HOSTNAME__ = $Hostname; __USERNAME__ = $Username; __PASSWORD_HASH__ = $hash
        __ALLOW_PW__ = $(if ($keys) { 'false' } else { 'true' }); __SSH_KEYS__ = $keysYaml
    }
    foreach ($k in $values.Keys) { $yaml = $yaml.Replace($k, $values[$k]) }
    if ($yaml -match '__[A-Z_]+__') { throw "Unfilled placeholder $($matches[0]) in template" }
    [IO.File]::WriteAllText((Join-Path $Usb 'autoinstall.yaml'), ($yaml -replace "`r`n", "`n"), (New-Object Text.UTF8Encoding($false)))
}

# --- Bundle --------------------------------------------------------------------
foreach ($dir in 'host', 'offline') {
    Write-Step "Copy $dir -> ${Usb}martha\$dir"
    robocopy (Join-Path $RepoRoot $dir) (Join-Path $Usb "martha\$dir") /MIR /NFL /NDL /NJH /NP /R:2 /W:2 | Out-Host
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
}
$global:LASTEXITCODE = 0

Get-Content -LiteralPath $bundleEnv | Where-Object { $_ -match '^HA_VERSION=' } | ForEach-Object { Write-Host "    $_" }
if ($BundleOnly) {
    Write-Step "USB stick ready. On the server: mount it and run 'sudo bash <mount>/martha/host/install.sh' (readme.md, route B)."
} else {
    Write-Step "USB stick ready. Eject it safely, boot the target PC from it and confirm the autoinstall with 'yes'."
}

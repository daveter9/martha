<#
.SYNOPSIS
  Splits the Ubuntu ISO into parts that fit in GitHub LFS (< 2 GB per file), or joins them back.

.DESCRIPTION
  The repo stores <iso>.001, <iso>.002, ... plus <iso>.sha256 (the official Ubuntu checksum).
  The .iso itself is gitignored. Run -Join after a fresh clone, before writing the stick with Rufus.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ".\0. install os\iso.ps1" -Join
  powershell -ExecutionPolicy Bypass -File ".\0. install os\iso.ps1" -Split   # after replacing the ISO
#>
[CmdletBinding(DefaultParameterSetName = 'Join')]
param(
    [Parameter(ParameterSetName = 'Join')][switch]$Join,
    [Parameter(ParameterSetName = 'Split', Mandatory)][switch]$Split,
    [string]$Iso = 'ubuntu-26.04.1-live-server-amd64.iso',
    [long]$PartSize = 1536MB
)

$ErrorActionPreference = 'Stop'
$IsoPath = Join-Path $PSScriptRoot $Iso
$ShaPath = "$IsoPath.sha256"

function Get-Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() }
function Get-Parts { @(Get-ChildItem -LiteralPath $PSScriptRoot -File | Where-Object { $_.Name -match "^$([regex]::Escape($Iso))\.\d{3}$" } | Sort-Object Name) }

if (-not (Test-Path -LiteralPath $ShaPath)) { throw "Missing $ShaPath (format: '<sha256> *$Iso', from https://releases.ubuntu.com/)" }
$expected = ((Get-Content -LiteralPath $ShaPath -Raw).Trim() -split '\s+')[0].ToLowerInvariant()

if ($Split) {
    if ((Get-Sha256 $IsoPath) -ne $expected) { throw "$Iso does not match $ShaPath" }
    Get-Parts | Remove-Item
    $buf = New-Object byte[] (4MB)
    $in = [IO.File]::OpenRead($IsoPath)
    try {
        $n = 0
        while ($in.Position -lt $in.Length) {
            $n++
            $part = '{0}.{1:D3}' -f $IsoPath, $n
            $out = [IO.File]::Create($part)
            try {
                $left = [Math]::Min($PartSize, $in.Length - $in.Position)
                while ($left -gt 0) {
                    $r = $in.Read($buf, 0, [int][Math]::Min($buf.Length, $left))
                    $out.Write($buf, 0, $r); $left -= $r
                }
            } finally { $out.Dispose() }
            Write-Host ('    {0} ({1:N0} MB)' -f (Split-Path -Leaf $part), ((Get-Item -LiteralPath $part).Length / 1MB))
        }
    } finally { $in.Dispose() }
    Write-Host "==> Split $Iso into $n parts" -ForegroundColor Cyan
    return
}

if ((Test-Path -LiteralPath $IsoPath) -and (Get-Sha256 $IsoPath) -eq $expected) {
    Write-Host "==> $Iso is already complete and verified" -ForegroundColor Cyan
    return
}
$parts = Get-Parts
if (-not $parts) { throw "No parts $Iso.001... found. Run 'git lfs pull' first." }
foreach ($p in $parts) {
    if ($p.Length -lt 1KB -and (Get-Content -LiteralPath $p.FullName -TotalCount 1) -match 'git-lfs') { throw "$($p.Name) is an LFS pointer; run 'git lfs pull' first." }
}
$tmp = "$IsoPath.joining"
$out = [IO.File]::Create($tmp)
try { foreach ($p in $parts) { $in = [IO.File]::OpenRead($p.FullName); try { $in.CopyTo($out) } finally { $in.Dispose() } } } finally { $out.Dispose() }
$got = Get-Sha256 $tmp
if ($got -ne $expected) { Remove-Item -LiteralPath $tmp; throw "Joined ISO has SHA256 $got, expected $expected" }
Move-Item -Force -LiteralPath $tmp -Destination $IsoPath
Write-Host "==> Joined $($parts.Count) parts into $Iso (SHA256 verified)" -ForegroundColor Cyan

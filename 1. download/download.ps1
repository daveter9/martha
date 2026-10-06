<#
.SYNOPSIS
  Downloads everything the target PC needs into ..\offline (run on Windows, with internet).

.DESCRIPTION
  - Builds a partial, still signed Ubuntu mirror under offline\apt: the original
    InRelease + Packages.gz files plus only the .debs (with their full dependency
    closure) needed for $Packages. The target verifies it with its own Ubuntu
    archive keyring, so no trust is placed in this Windows machine.
  - Downloads the Home Assistant container image for linux/amd64 straight from the
    registry (no Docker needed) and stores it as an OCI image layout under
    offline\images\homeassistant, which `docker load` accepts.
  - Writes offline\bundle.env, read by host/install.sh.

  Safe to re-run: files that already exist with the correct hash are skipped, and
  files that are no longer needed are removed.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File ".\1. download\download.ps1"
  powershell -ExecutionPolicy Bypass -File ".\1. download\download.ps1" -HaVersion 2026.10.1
#>
[CmdletBinding()]
param(
    # Home Assistant tag: 'stable' resolves to the current release and pins it.
    [string]$HaVersion = 'stable',
    [string]$Mirror = 'http://archive.ubuntu.com/ubuntu',
    [string]$Suite = 'resolute',
    [string[]]$Components = @('main', 'universe'),
    [string]$Arch = 'amd64',
    # avahi-daemon makes the PC reachable as <hostname>.local (mDNS).
    [string[]]$Packages = @('docker.io', 'docker-compose-v2', 'avahi-daemon'),
    [string]$Registry = 'ghcr.io',
    [string]$ImageRepo = 'home-assistant/home-assistant'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with a progress bar
Add-Type -AssemblyName System.Net.Http

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Offline = Join-Path $RepoRoot 'offline'
$AptRoot = Join-Path $Offline 'apt'
$ImgRoot = Join-Path $Offline 'images\homeassistant'
$Pockets = @($Suite, "$Suite-updates", "$Suite-security")

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Get-Sha256([string]$path) { (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLowerInvariant() }

function Save-Url([string]$url, [string]$dest, [string]$sha256) {
    if ($sha256 -and (Test-Path -LiteralPath $dest) -and (Get-Sha256 $dest) -eq $sha256) { return $false }
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    $tmp = "$dest.part"
    Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $tmp -TimeoutSec 600
    if ($sha256) {
        $got = Get-Sha256 $tmp
        if ($got -ne $sha256) { Remove-Item -LiteralPath $tmp; throw "SHA256 mismatch for $url (expected $sha256, got $got)" }
    }
    Move-Item -Force -LiteralPath $tmp -Destination $dest
    return $true
}

function Read-GzipText([string]$path) {
    $fs = [IO.File]::OpenRead($path)
    try {
        $gz = New-Object IO.Compression.GZipStream($fs, [IO.Compression.CompressionMode]::Decompress)
        (New-Object IO.StreamReader($gz)).ReadToEnd()
    } finally { $fs.Dispose() }
}

function Remove-Unlisted([string]$root, [System.Collections.Generic.HashSet[string]]$keep) {
    if (-not (Test-Path -LiteralPath $root)) { return }
    Get-ChildItem -LiteralPath $root -Recurse -File | Where-Object { -not $keep.Contains($_.FullName) } | ForEach-Object {
        Write-Host "    remove stale $($_.FullName.Substring($RepoRoot.Length + 1))"
        Remove-Item -LiteralPath $_.FullName
    }
    Get-ChildItem -LiteralPath $root -Recurse -Directory | Sort-Object { $_.FullName.Length } -Descending |
        Where-Object { -not (Get-ChildItem -LiteralPath $_.FullName -Force) } | Remove-Item
}

#region Debian version comparison (dpkg algorithm)
function Compare-DebPart([string]$a, [string]$b) {
    # Non-digit runs compare with '~' < end < letters < other characters; digit runs numerically.
    $order = { param($c) if ($c -eq '~') { -1 } elseif ([char]::IsLetter($c)) { [int]$c } else { [int]$c + 256 } }
    $i = 0; $j = 0
    while ($i -lt $a.Length -or $j -lt $b.Length) {
        while (($i -lt $a.Length -and -not [char]::IsDigit($a[$i])) -or ($j -lt $b.Length -and -not [char]::IsDigit($b[$j]))) {
            $ca = if ($i -lt $a.Length -and -not [char]::IsDigit($a[$i])) { & $order $a[$i] } else { 0 }
            $cb = if ($j -lt $b.Length -and -not [char]::IsDigit($b[$j])) { & $order $b[$j] } else { 0 }
            if ($ca -ne $cb) { return [Math]::Sign($ca - $cb) }
            $i++; $j++
        }
        $na = ''; while ($i -lt $a.Length -and [char]::IsDigit($a[$i])) { $na += $a[$i]; $i++ }
        $nb = ''; while ($j -lt $b.Length -and [char]::IsDigit($b[$j])) { $nb += $b[$j]; $j++ }
        $da = if ($na) { [decimal]$na } else { 0 }
        $db = if ($nb) { [decimal]$nb } else { 0 }
        if ($da -ne $db) { return [Math]::Sign($da - $db) }
    }
    return 0
}

function Compare-DebVersion([string]$a, [string]$b) {
    $split = {
        param($v)
        $epoch = 0
        if ($v -match '^(\d+):(.*)$') { $epoch = [int]$matches[1]; $v = $matches[2] }
        $rev = ''
        $k = $v.LastIndexOf('-')
        if ($k -ge 0) { $rev = $v.Substring($k + 1); $v = $v.Substring(0, $k) }
        , @($epoch, $v, $rev)
    }
    $x = & $split $a; $y = & $split $b
    if ($x[0] -ne $y[0]) { return [Math]::Sign($x[0] - $y[0]) }
    $r = Compare-DebPart $x[1] $y[1]
    if ($r -ne 0) { return $r }
    return Compare-DebPart $x[2] $y[2]
}
#endregion

#region Ubuntu packages
function Get-AptIndexes {
    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    $stanzas = @{}     # package name -> list of stanza strings (all pockets/components)
    $providers = @{}   # virtual name  -> list of real package names
    foreach ($pocket in $Pockets) {
        $distDir = Join-Path $AptRoot "dists\$pocket"
        $inRelease = Join-Path $distDir 'InRelease'
        Write-Step "Index $pocket"
        Save-Url "$Mirror/dists/$pocket/InRelease" $inRelease $null | Out-Null
        [void]$keep.Add($inRelease)
        $release = [IO.File]::ReadAllText($inRelease)
        $sha = $release.Substring($release.IndexOf("`nSHA256:"))
        foreach ($comp in $Components) {
            $rel = "$comp/binary-$Arch/Packages.gz"
            $m = [regex]::Match($sha, "(?m)^ ([0-9a-f]{64})\s+\d+\s+$([regex]::Escape($rel))\s*$")
            if (-not $m.Success) { throw "$rel not listed in $pocket InRelease" }
            $dest = Join-Path $distDir ($rel -replace '/', '\')
            Save-Url "$Mirror/dists/$pocket/$rel" $dest $m.Groups[1].Value | Out-Null
            [void]$keep.Add($dest)
            foreach ($st in ((Read-GzipText $dest) -split "`n`n")) {
                $pm = [regex]::Match($st, '(?m)^Package: (\S+)')
                if (-not $pm.Success) { continue }
                $name = $pm.Groups[1].Value
                if (-not $stanzas.ContainsKey($name)) { $stanzas[$name] = New-Object System.Collections.ArrayList }
                [void]$stanzas[$name].Add($st)
                $pv = [regex]::Match($st, '(?m)^Provides: (.*)$')
                if ($pv.Success) {
                    foreach ($p in $pv.Groups[1].Value -split ',') {
                        $v = ($p -replace '\(.*\)', '').Trim()
                        if (-not $providers.ContainsKey($v)) { $providers[$v] = New-Object System.Collections.ArrayList }
                        if (-not $providers[$v].Contains($name)) { [void]$providers[$v].Add($name) }
                    }
                }
            }
        }
    }
    return @{ Stanzas = $stanzas; Providers = $providers; Keep = $keep }
}

function Get-Candidate($idx, [string]$name) {
    # apt's default candidate: the highest version across all pockets.
    $best = $null; $bestVer = $null
    foreach ($st in $idx.Stanzas[$name]) {
        $ver = [regex]::Match($st, '(?m)^Version: (\S+)').Groups[1].Value
        if (-not $best -or (Compare-DebVersion $ver $bestVer) -gt 0) { $best = $st; $bestVer = $ver }
    }
    return $best
}

function Resolve-AptClosure($idx, [string[]]$roots) {
    $selected = [ordered]@{}   # package name -> candidate stanza
    $provided = @{}            # virtual name -> selected provider
    $queue = New-Object System.Collections.Queue
    $roots | ForEach-Object { $queue.Enqueue($_) }
    while ($queue.Count) {
        $name = $queue.Dequeue()
        if ($selected.Contains($name)) { continue }
        $st = Get-Candidate $idx $name
        if (-not $st) { throw "Package $name not found" }
        $selected[$name] = $st
        $pv = [regex]::Match($st, '(?m)^Provides: (.*)$')
        if ($pv.Success) { $pv.Groups[1].Value -split ',' | ForEach-Object { $provided[($_ -replace '\(.*\)', '').Trim()] = $name } }
        # Recommends are not followed: install.sh uses --no-install-recommends.
        $deps = @([regex]::Matches($st, '(?m)^(?:Pre-)?Depends: (.*)$') | ForEach-Object { $_.Groups[1].Value -split ',' })
        foreach ($group in $deps) {
            $alts = @($group -split '\|' | ForEach-Object { ($_ -replace '\(.*?\)', '' -replace ':\w+', '').Trim() } | Where-Object { $_ })
            if (@($alts | Where-Object { $selected.Contains($_) -or $provided.ContainsKey($_) }).Count) { continue }
            $pick = $null
            foreach ($alt in $alts) {
                if ($idx.Stanzas.ContainsKey($alt)) { $pick = $alt; break }
                if ($idx.Providers.ContainsKey($alt)) { $pick = $idx.Providers[$alt][0]; break }
            }
            if (-not $pick) { throw "Cannot satisfy '$group' (needed by $name)" }
            $queue.Enqueue($pick)
        }
    }
    return $selected
}

function Sync-AptPackages {
    $idx = Get-AptIndexes
    Write-Step "Resolve dependencies of: $($Packages -join ', ')"
    $selected = Resolve-AptClosure $idx $Packages
    Write-Host "    $($selected.Count) packages"
    $keep = $idx.Keep
    $total = 0; $fetched = 0
    foreach ($name in $selected.Keys) {
        $st = $selected[$name]
        $file = [regex]::Match($st, '(?m)^Filename: (\S+)').Groups[1].Value
        $sha = [regex]::Match($st, '(?m)^SHA256: (\S+)').Groups[1].Value
        $dest = Join-Path $AptRoot ($file -replace '/', '\')
        if (Save-Url "$Mirror/$file" $dest $sha) { $fetched++; Write-Host "    + $(Split-Path -Leaf $file)" }
        [void]$keep.Add($dest)
        $total += (Get-Item -LiteralPath $dest).Length
    }
    Write-Host ("    {0} downloaded, {1} up to date, {2:N0} MB total" -f $fetched, ($selected.Count - $fetched), ($total / 1MB))
    $list = Join-Path $AptRoot 'packages.txt'
    $lines = $selected.Keys | ForEach-Object { "$_ $([regex]::Match($selected[$_], '(?m)^Version: (\S+)').Groups[1].Value)" }
    [IO.File]::WriteAllText($list, (($lines -join "`n") + "`n"))
    [void]$keep.Add($list)
    Remove-Unlisted $AptRoot $keep
}
#endregion

#region Container image
$Http = New-Object System.Net.Http.HttpClient((New-Object System.Net.Http.HttpClientHandler -Property @{ AllowAutoRedirect = $false }))
$Http.Timeout = [TimeSpan]::FromMinutes(30)

function Invoke-Registry([string]$path, [string[]]$accept, [string]$token) {
    # Follows redirects manually: blob URLs redirect to a CDN that must not get the bearer token.
    $url = "https://$Registry/v2/$ImageRepo/$path"
    $auth = $true
    for ($n = 0; $n -lt 5; $n++) {
        $req = New-Object System.Net.Http.HttpRequestMessage([System.Net.Http.HttpMethod]::Get, $url)
        if ($auth) { $req.Headers.Authorization = New-Object System.Net.Http.Headers.AuthenticationHeaderValue('Bearer', $token) }
        $accept | ForEach-Object { [void]$req.Headers.Accept.ParseAdd($_) }
        $resp = $Http.SendAsync($req, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
        if ([int]$resp.StatusCode -in 301, 302, 303, 307, 308) {
            $url = $resp.Headers.Location.AbsoluteUri; $auth = $false; $resp.Dispose(); continue
        }
        if (-not $resp.IsSuccessStatusCode) { throw "GET $url -> $([int]$resp.StatusCode) $($resp.ReasonPhrase)" }
        return $resp
    }
    throw "Too many redirects for $path"
}

function Get-Sha256Bytes([byte[]]$bytes) {
    $h = [Security.Cryptography.SHA256]::Create()
    -join ($h.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
}

function Save-Blob([string]$digest, [string]$token, $keep) {
    $dest = Join-Path $ImgRoot ("blobs\sha256\" + $digest.Substring(7))
    [void]$keep.Add($dest)
    if ((Test-Path -LiteralPath $dest) -and ('sha256:' + (Get-Sha256 $dest)) -eq $digest) { return $dest }
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    $resp = Invoke-Registry "blobs/$digest" @('*/*') $token
    $tmp = "$dest.part"
    $out = [IO.File]::Create($tmp)
    try { $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult().CopyTo($out) } finally { $out.Dispose(); $resp.Dispose() }
    if (('sha256:' + (Get-Sha256 $tmp)) -ne $digest) { Remove-Item -LiteralPath $tmp; throw "Digest mismatch for blob $digest" }
    Move-Item -Force -LiteralPath $tmp -Destination $dest
    Write-Host ("    + {0} ({1:N0} MB)" -f $digest.Substring(7, 12), ((Get-Item -LiteralPath $dest).Length / 1MB))
    return $dest
}

function Sync-Image {
    Write-Step "Image $Registry/${ImageRepo}:$HaVersion (linux/$Arch)"
    $token = (Invoke-RestMethod -UseBasicParsing "https://$Registry/token?scope=repository:${ImageRepo}:pull&service=$Registry").token
    $accept = @('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
                'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json')
    $resp = Invoke-Registry "manifests/$HaVersion" $accept $token
    $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult(); $resp.Dispose()
    $doc = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    if ($doc.manifests) {
        # Multi-arch index: keep only the amd64 manifest, so the layout is complete for one platform.
        $entry = $doc.manifests | Where-Object { $_.platform.os -eq 'linux' -and $_.platform.architecture -eq $Arch -and -not $_.platform.variant } | Select-Object -First 1
        if (-not $entry) { throw "No linux/$Arch image in $HaVersion" }
        $resp = Invoke-Registry "manifests/$($entry.digest)" $accept $token
        $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult(); $resp.Dispose()
        $doc = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    }
    $mediaType = if ($doc.mediaType) { $doc.mediaType } else { 'application/vnd.oci.image.manifest.v1+json' }
    $digest = 'sha256:' + (Get-Sha256Bytes $bytes)

    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    $mdest = Join-Path $ImgRoot ("blobs\sha256\" + $digest.Substring(7))
    New-Item -ItemType Directory -Force (Split-Path -Parent $mdest) | Out-Null
    [IO.File]::WriteAllBytes($mdest, $bytes); [void]$keep.Add($mdest)

    $cfgPath = Save-Blob $doc.config.digest $token $keep
    $cfg = [IO.File]::ReadAllText($cfgPath) | ConvertFrom-Json
    $version = $cfg.config.Labels.'org.opencontainers.image.version'
    if (-not $version) { $version = $HaVersion }
    if ($version -eq 'stable' -or $version -eq 'latest') { throw "Could not determine the version of $HaVersion" }
    $total = 0
    foreach ($layer in $doc.layers) { $total += (Get-Item -LiteralPath (Save-Blob $layer.digest $token $keep)).Length }
    Write-Host ("    Home Assistant {0}, {1} layers, {2:N0} MB" -f $version, @($doc.layers).Count, ($total / 1MB))

    $ref = "$Registry/${ImageRepo}:$version"
    $index = [ordered]@{
        schemaVersion = 2
        mediaType     = 'application/vnd.oci.image.index.v1+json'
        manifests     = @([ordered]@{
            mediaType   = $mediaType
            digest      = $digest
            size        = $bytes.Length
            annotations = [ordered]@{ 'io.containerd.image.name' = $ref; 'org.opencontainers.image.ref.name' = $version }
        })
    }
    $utf8 = New-Object Text.UTF8Encoding($false)
    $indexPath = Join-Path $ImgRoot 'index.json'
    $layoutPath = Join-Path $ImgRoot 'oci-layout'
    [IO.File]::WriteAllText($indexPath, ($index | ConvertTo-Json -Depth 6), $utf8)
    [IO.File]::WriteAllText($layoutPath, '{"imageLayoutVersion":"1.0.0"}', $utf8)
    [void]$keep.Add($indexPath); [void]$keep.Add($layoutPath)
    Remove-Unlisted $ImgRoot $keep
    return @{ Ref = $ref; Version = $version; Digest = $digest }
}
#endregion

New-Item -ItemType Directory -Force $Offline | Out-Null
Sync-AptPackages
$img = Sync-Image

$envFile = @(
    "# Generated by 1. download/download.ps1 - do not edit."
    "BUNDLE_CREATED=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    "UBUNTU_SUITES=`"$($Pockets -join ' ')`""
    "UBUNTU_COMPONENTS=`"$($Components -join ' ')`""
    "APT_PACKAGES=`"$($Packages -join ' ')`""
    "HA_IMAGE=$($img.Ref)"
    "HA_VERSION=$($img.Version)"
    "HA_MANIFEST_DIGEST=$($img.Digest)"
) -join "`n"
[IO.File]::WriteAllText((Join-Path $Offline 'bundle.env'), "$envFile`n", (New-Object Text.UTF8Encoding($false)))
Write-Step "Done: offline bundle with Home Assistant $($img.Version) in $Offline"

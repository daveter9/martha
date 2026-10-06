<#
.SYNOPSIS
  Downloads everything the target PC needs into ..\offline (run on Windows, with internet).

.DESCRIPTION
  - Builds a partial, still signed Ubuntu mirror under offline\apt: the original
    InRelease + Packages.gz files plus only the .debs (with their full dependency
    closure) needed for $Packages. The target verifies it with its own Ubuntu
    archive keyring, so no trust is placed in this Windows machine.
  - Downloads the Home Assistant and TimescaleDB container images for linux/amd64
    straight from their registries (no Docker needed) and stores them as OCI image
    layouts under offline\images\<name>, which `docker load` accepts.
  - Downloads the LTSS custom component (offline\custom_components\ltss) and the
    wheels it needs that are not in the Home Assistant image (offline\wheels).
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
    # TimescaleDB image tag on Docker Hub (timescale/timescaledb); a fixed version.
    [string]$TimescaleVersion = '2.30.2-pg18',
    # LTSS release tag (github.com/freol35241/ltss).
    [string]$LtssVersion = 'v2.1.1',
    # Requirements of LTSS that the Home Assistant image lacks, pinned (name==version).
    [string[]]$Wheels = @('psycopg2-binary==2.9.13', 'geoalchemy2==0.20.0')
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow with a progress bar
Add-Type -AssemblyName System.Net.Http

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Offline = Join-Path $RepoRoot 'offline'
$AptRoot = Join-Path $Offline 'apt'
$WheelRoot = Join-Path $Offline 'wheels'
$LtssRoot = Join-Path $Offline 'custom_components\ltss'
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

#region Container images
$Http = New-Object System.Net.Http.HttpClient((New-Object System.Net.Http.HttpClientHandler -Property @{ AllowAutoRedirect = $false }))
$Http.Timeout = [TimeSpan]::FromMinutes(30)

function Get-RegistryToken($src) {
    # Standard token flow: the 401 from /v2/ names the auth realm and service.
    $resp = $Http.GetAsync("https://$($src.Host)/v2/").GetAwaiter().GetResult()
    $challenge = "$($resp.Headers.WwwAuthenticate)"; $resp.Dispose()
    $realm = [regex]::Match($challenge, 'realm="([^"]+)"').Groups[1].Value
    $service = [regex]::Match($challenge, 'service="([^"]+)"').Groups[1].Value
    if (-not $realm) { throw "No token realm from $($src.Host): $challenge" }
    (Invoke-RestMethod -UseBasicParsing "${realm}?service=$service&scope=repository:$($src.Repo):pull").token
}

function Invoke-Registry($src, [string]$path, [string[]]$accept, [string]$token) {
    # Follows redirects manually: blob URLs redirect to a CDN that must not get the bearer token.
    $url = "https://$($src.Host)/v2/$($src.Repo)/$path"
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

function Save-Blob($src, [string]$digest, [string]$token, $keep) {
    $dest = Join-Path $src.Dir ("blobs\sha256\" + $digest.Substring(7))
    [void]$keep.Add($dest)
    if ((Test-Path -LiteralPath $dest) -and ('sha256:' + (Get-Sha256 $dest)) -eq $digest) { return $dest }
    New-Item -ItemType Directory -Force (Split-Path -Parent $dest) | Out-Null
    $resp = Invoke-Registry $src "blobs/$digest" @('*/*') $token
    $tmp = "$dest.part"
    $out = [IO.File]::Create($tmp)
    try { $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult().CopyTo($out) } finally { $out.Dispose(); $resp.Dispose() }
    if (('sha256:' + (Get-Sha256 $tmp)) -ne $digest) { Remove-Item -LiteralPath $tmp; throw "Digest mismatch for blob $digest" }
    Move-Item -Force -LiteralPath $tmp -Destination $dest
    Write-Host ("    + {0} ({1:N0} MB)" -f $digest.Substring(7, 12), ((Get-Item -LiteralPath $dest).Length / 1MB))
    return $dest
}

function Sync-Image($src) {
    # $src: Name (for messages), Host (registry API), Repo, RefName (name for docker),
    # Tag, Dir (OCI layout), and VersionLabel to resolve a floating tag like 'stable'.
    Write-Step "Image $($src.RefName):$($src.Tag) (linux/$Arch)"
    $token = Get-RegistryToken $src
    $accept = @('application/vnd.oci.image.index.v1+json', 'application/vnd.docker.distribution.manifest.list.v2+json',
                'application/vnd.oci.image.manifest.v1+json', 'application/vnd.docker.distribution.manifest.v2+json')
    $resp = Invoke-Registry $src "manifests/$($src.Tag)" $accept $token
    $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult(); $resp.Dispose()
    $doc = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    if ($doc.manifests) {
        # Multi-arch index: keep only the amd64 manifest, so the layout is complete for one platform.
        $entry = $doc.manifests | Where-Object { $_.platform.os -eq 'linux' -and $_.platform.architecture -eq $Arch -and -not $_.platform.variant } | Select-Object -First 1
        if (-not $entry) { throw "No linux/$Arch image in $($src.Tag)" }
        $resp = Invoke-Registry $src "manifests/$($entry.digest)" $accept $token
        $bytes = $resp.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult(); $resp.Dispose()
        $doc = [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    }
    $mediaType = if ($doc.mediaType) { $doc.mediaType } else { 'application/vnd.oci.image.manifest.v1+json' }
    $digest = 'sha256:' + (Get-Sha256Bytes $bytes)

    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    $mdest = Join-Path $src.Dir ("blobs\sha256\" + $digest.Substring(7))
    New-Item -ItemType Directory -Force (Split-Path -Parent $mdest) | Out-Null
    [IO.File]::WriteAllBytes($mdest, $bytes); [void]$keep.Add($mdest)

    $cfgPath = Save-Blob $src $doc.config.digest $token $keep
    $version = $src.Tag
    if ($src.VersionLabel) {
        $cfg = [IO.File]::ReadAllText($cfgPath) | ConvertFrom-Json
        $label = $cfg.config.Labels.($src.VersionLabel)
        if ($label) { $version = $label }
        if ($version -eq 'stable' -or $version -eq 'latest') { throw "Could not determine the version of $($src.Tag)" }
    }
    $total = 0
    foreach ($layer in $doc.layers) { $total += (Get-Item -LiteralPath (Save-Blob $src $layer.digest $token $keep)).Length }
    Write-Host ("    {0} {1}, {2} layers, {3:N0} MB" -f $src.Name, $version, @($doc.layers).Count, ($total / 1MB))

    $ref = "$($src.RefName):$version"
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
    $indexPath = Join-Path $src.Dir 'index.json'
    $layoutPath = Join-Path $src.Dir 'oci-layout'
    [IO.File]::WriteAllText($indexPath, ($index | ConvertTo-Json -Depth 6), $utf8)
    [IO.File]::WriteAllText($layoutPath, '{"imageLayoutVersion":"1.0.0"}', $utf8)
    [void]$keep.Add($indexPath); [void]$keep.Add($layoutPath)
    Remove-Unlisted $src.Dir $keep
    return @{ Ref = $ref; Version = $version; Digest = $digest }
}
#endregion

#region LTSS and its wheels
function Sync-Wheels {
    # Wheels for requirements the Home Assistant image (Alpine, musl) does not ship.
    # Pure-Python wheels if available, otherwise every CPython musllinux x86_64 build,
    # so install.sh can pick the one matching the image's Python.
    Write-Step "Wheels: $($Wheels -join ', ')"
    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    foreach ($spec in $Wheels) {
        if ($spec -notmatch '^([A-Za-z0-9._-]+)==([A-Za-z0-9.+!-]+)$') { throw "Wheel must be pinned as name==version, got: $spec" }
        $name = $matches[1]; $ver = $matches[2]
        $files = (Invoke-RestMethod -UseBasicParsing "https://pypi.org/pypi/$name/$ver/json").urls |
            Where-Object { $_.packagetype -eq 'bdist_wheel' }
        $pick = @($files | Where-Object { $_.filename -match '-none-any\.whl$' })
        if (-not $pick) { $pick = @($files | Where-Object { $_.filename -match '-cp3\d+-cp3\d+-musllinux_1_2_x86_64\.whl$' }) }
        if (-not $pick) { throw "No pure-Python or musllinux x86_64 wheel for $spec" }
        foreach ($f in $pick) {
            $dest = Join-Path $WheelRoot $f.filename
            if (Save-Url $f.url $dest $f.digests.sha256) { Write-Host "    + $($f.filename)" }
            [void]$keep.Add($dest)
        }
    }
    Remove-Unlisted $WheelRoot $keep
}

function Sync-Ltss {
    Write-Step "LTSS $LtssVersion"
    $keep = New-Object 'System.Collections.Generic.HashSet[string]'
    $items = Invoke-RestMethod -UseBasicParsing "https://api.github.com/repos/freol35241/ltss/contents/custom_components/ltss?ref=$LtssVersion"
    foreach ($item in $items) {
        if ($item.type -ne 'file') { throw "Unexpected $($item.type) in LTSS: $($item.path)" }
        $dest = Join-Path $LtssRoot $item.name
        Save-Url $item.download_url $dest $null | Out-Null
        [void]$keep.Add($dest)
    }
    $manifest = Get-Content -Raw -LiteralPath (Join-Path $LtssRoot 'manifest.json') | ConvertFrom-Json
    Write-Host "    requirements: $($manifest.requirements -join ', ')"
    Remove-Unlisted $LtssRoot $keep
}
#endregion

New-Item -ItemType Directory -Force $Offline | Out-Null
Sync-AptPackages
$ha = Sync-Image @{ Name = 'Home Assistant'; Host = 'ghcr.io'; Repo = 'home-assistant/home-assistant'
                    RefName = 'ghcr.io/home-assistant/home-assistant'; Tag = $HaVersion
                    Dir = Join-Path $Offline 'images\homeassistant'; VersionLabel = 'org.opencontainers.image.version' }
$tsdb = Sync-Image @{ Name = 'TimescaleDB'; Host = 'registry-1.docker.io'; Repo = 'timescale/timescaledb'
                      RefName = 'docker.io/timescale/timescaledb'; Tag = $TimescaleVersion
                      Dir = Join-Path $Offline 'images\timescaledb' }
Sync-Ltss
Sync-Wheels

$envFile = @(
    "# Generated by 1. download/download.ps1 - do not edit."
    "BUNDLE_CREATED=$((Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ'))"
    "UBUNTU_SUITES=`"$($Pockets -join ' ')`""
    "UBUNTU_COMPONENTS=`"$($Components -join ' ')`""
    "APT_PACKAGES=`"$($Packages -join ' ')`""
    "HA_IMAGE=$($ha.Ref)"
    "HA_VERSION=$($ha.Version)"
    "HA_MANIFEST_DIGEST=$($ha.Digest)"
    "TSDB_IMAGE=$($tsdb.Ref)"
    "TSDB_VERSION=$($tsdb.Version)"
    "TSDB_MANIFEST_DIGEST=$($tsdb.Digest)"
    "LTSS_VERSION=$LtssVersion"
    "LTSS_WHEELS=`"$($Wheels -join ' ')`""
) -join "`n"
[IO.File]::WriteAllText((Join-Path $Offline 'bundle.env'), "$envFile`n", (New-Object Text.UTF8Encoding($false)))
Write-Step "Done: offline bundle with Home Assistant $($ha.Version) and TimescaleDB $($tsdb.Version) in $Offline"

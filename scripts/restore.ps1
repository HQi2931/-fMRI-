[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][string]$BackupDirectory,
    [string]$DatabasePath = 'work/neuroagent.db'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$workRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'work'))
$backup = [System.IO.Path]::GetFullPath($BackupDirectory)
$backupRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'backups'))

function Assert-PathWithin {
    param(
        [Parameter(Mandatory)][string]$Candidate,
        [Parameter(Mandatory)][string]$Root,
        [Parameter(Mandatory)][string]$Label
    )
    $separator = [System.IO.Path]::DirectorySeparatorChar
    $rootPrefix = $Root.TrimEnd([char[]]@('\', '/')) + $separator
    if ($Candidate -ne $Root -and -not $Candidate.StartsWith(
        $rootPrefix,
        [System.StringComparison]::OrdinalIgnoreCase
    )) {
        throw "$Label must remain inside $Root."
    }
}

function Get-ConfiguredDatabaseUrl {
    param([Parameter(Mandatory)][string]$RepositoryRoot)

    if (-not [string]::IsNullOrWhiteSpace($env:RSFMRI_DATABASE_URL)) {
        return $env:RSFMRI_DATABASE_URL.Trim()
    }
    $dotenvPath = Join-Path $RepositoryRoot '.env'
    if (Test-Path -LiteralPath $dotenvPath -PathType Leaf) {
        foreach ($line in Get-Content -Encoding UTF8 $dotenvPath) {
            if ($line -match '^\s*RSFMRI_DATABASE_URL\s*=\s*(.*?)\s*$') {
                $value = [string]$Matches[1]
                if (($value.StartsWith('"') -and $value.EndsWith('"')) -or
                    ($value.StartsWith("'") -and $value.EndsWith("'"))) {
                    $value = $value.Substring(1, $value.Length - 2)
                }
                return $value
            }
        }
    }
    return 'sqlite:///./work/neuroagent.db'
}

function Resolve-ConfiguredDatabasePath {
    param(
        [Parameter(Mandatory)][string]$DatabaseUrl,
        [Parameter(Mandatory)][string]$RepositoryRoot
    )

    $prefix = 'sqlite:///'
    if (-not $DatabaseUrl.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw 'Backup and restore scripts support only a file-backed SQLite RSFMRI_DATABASE_URL.'
    }
    $pathText = $DatabaseUrl.Substring($prefix.Length)
    if ([string]::IsNullOrWhiteSpace($pathText) -or $pathText -eq ':memory:' -or
        $pathText.Contains('?') -or $pathText.Contains('#')) {
        throw 'RSFMRI_DATABASE_URL must name a plain file-backed SQLite database.'
    }
    $nativePath = $pathText.Replace('/', [System.IO.Path]::DirectorySeparatorChar)
    if ([System.IO.Path]::IsPathRooted($nativePath)) {
        return [System.IO.Path]::GetFullPath($nativePath)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $RepositoryRoot $nativePath))
}

function Test-RecordedServiceIsRunning {
    param(
        [Parameter(Mandatory)][string]$StatePath,
        [Parameter(Mandatory)][string]$ServiceName
    )

    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $false
    }
    try {
        $state = Get-Content -Raw -Encoding UTF8 $StatePath | ConvertFrom-Json
        $pidValue = [int]$state.pid
        if ($pidValue -le 0 -or [string]::IsNullOrWhiteSpace([string]$state.started_at)) {
            throw 'missing pid or started_at'
        }
        $recordedStart = [datetime]::Parse([string]$state.started_at).ToUniversalTime()
    }
    catch {
        throw "Cannot verify $ServiceName runtime state at $StatePath. Stop local services and remove or repair the stale state file before restoring."
    }

    $process = Get-Process -Id $pidValue -ErrorAction SilentlyContinue
    if ($null -eq $process) {
        return $false
    }
    try {
        $actualStart = $process.StartTime.ToUniversalTime()
    }
    catch {
        return $false
    }
    return [math]::Abs(($recordedStart - $actualStart).TotalSeconds) -le 2
}

function Acquire-RestoreSentinel {
    param([Parameter(Mandatory)][string]$SentinelPath)

    $ownerToken = [guid]::NewGuid().ToString('N')
    for ($attempt = 0; $attempt -lt 2; $attempt++) {
        try {
            $stream = [System.IO.File]::Open(
                $SentinelPath,
                [System.IO.FileMode]::CreateNew,
                [System.IO.FileAccess]::Write,
                [System.IO.FileShare]::None
            )
            try {
                $payload = @{
                    pid = $PID
                    owner_token = $ownerToken
                    created_at = [datetime]::UtcNow.ToString('o')
                } | ConvertTo-Json -Compress
                $bytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
                $stream.Write($bytes, 0, $bytes.Length)
                $stream.Flush($true)
            }
            finally {
                $stream.Dispose()
            }
            return $ownerToken
        }
        catch [System.IO.IOException] {
            if (-not (Test-Path -LiteralPath $SentinelPath -PathType Leaf)) {
                continue
            }
            try {
                $existing = Get-Content -Raw -Encoding UTF8 $SentinelPath | ConvertFrom-Json
                $existingPid = [int]$existing.pid
                if ($existingPid -le 0 -or
                    [string]::IsNullOrWhiteSpace([string]$existing.owner_token)) {
                    throw 'missing pid or owner_token'
                }
            }
            catch {
                throw "Cannot verify the existing restore lock at $SentinelPath. Repair or remove it only after confirming that no restore is running."
            }
            if ($null -ne (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
                throw "Another database restore is already in progress (PID $existingPid)."
            }
            Remove-Item -LiteralPath $SentinelPath -Force
        }
    }
    throw "Could not acquire the database restore lock at $SentinelPath."
}

function Release-RestoreSentinel {
    param(
        [Parameter(Mandatory)][string]$SentinelPath,
        [Parameter(Mandatory)][string]$OwnerToken
    )

    if (-not (Test-Path -LiteralPath $SentinelPath -PathType Leaf)) {
        return
    }
    try {
        $current = Get-Content -Raw -Encoding UTF8 $SentinelPath | ConvertFrom-Json
        if ([string]$current.owner_token -eq $OwnerToken) {
            Remove-Item -LiteralPath $SentinelPath -Force
        }
    }
    catch {
        # Fail closed: do not delete a lock that can no longer be proven ours.
    }
}

function Assert-NoDatabaseRuntimeUsers {
    param([Parameter(Mandatory)][string]$MarkerDirectory)

    if (-not (Test-Path -LiteralPath $MarkerDirectory -PathType Container)) {
        return
    }
    foreach ($marker in Get-ChildItem -LiteralPath $MarkerDirectory -Filter '*.json' -File) {
        if (-not (Test-Path -LiteralPath $marker.FullName -PathType Leaf)) {
            continue
        }
        try {
            $runtime = Get-Content -Raw -Encoding UTF8 $marker.FullName | ConvertFrom-Json
            $runtimePid = [int]$runtime.pid
            if ($runtimePid -le 0) {
                throw 'missing pid'
            }
        }
        catch {
            if (-not (Test-Path -LiteralPath $marker.FullName -PathType Leaf)) {
                continue
            }
            throw "Cannot verify database runtime marker $($marker.FullName). Stop all API and Worker processes before restoring."
        }
        if ($null -ne (Get-Process -Id $runtimePid -ErrorAction SilentlyContinue)) {
            throw "The metadata database is in use by a live API or Worker process (PID $runtimePid)."
        }
        Remove-Item -LiteralPath $marker.FullName -Force
    }
}

try {
    Assert-PathWithin -Candidate $backup -Root $backupRoot -Label 'BackupDirectory'
}
catch {
    throw 'BackupDirectory must be inside this repository backup root.'
}
$source = Join-Path $backup 'neuroagent.db'
$manifestPath = Join-Path $backup 'neuroagent.json'
if (-not (Test-Path -LiteralPath $source -PathType Leaf) -or
    -not (Test-Path -LiteralPath $manifestPath -PathType Leaf)) {
    throw 'Backup database or manifest is missing.'
}
$manifest = Get-Content -Raw -Encoding UTF8 $manifestPath | ConvertFrom-Json
$actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $source).Hash.ToLowerInvariant()
if ($actualHash -ne $manifest.sha256.ToLowerInvariant()) {
    throw 'Backup checksum validation failed.'
}
$target = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatabasePath))
Assert-PathWithin -Candidate $target -Root $workRoot -Label 'DatabasePath'
$configuredTarget = Resolve-ConfiguredDatabasePath `
    -DatabaseUrl (Get-ConfiguredDatabaseUrl -RepositoryRoot $repoRoot) `
    -RepositoryRoot $repoRoot
Assert-PathWithin -Candidate $configuredTarget -Root $workRoot -Label 'RSFMRI_DATABASE_URL'
if (-not [string]::Equals(
    $target,
    $configuredTarget,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'DatabasePath does not match RSFMRI_DATABASE_URL. Refusing to restore a different SQLite file.'
}
$targetParent = Split-Path -Parent $target
New-Item -ItemType Directory -Force -Path $targetParent | Out-Null

if (-not $PSCmdlet.ShouldProcess($target, "restore verified metadata backup $source")) {
    return
}

$restoreSentinel = "$target.restore.lock"
$runtimeMarkerDirectory = "$target.runtime-users"
$restoreOwner = Acquire-RestoreSentinel -SentinelPath $restoreSentinel
try {
    foreach ($serviceName in @('api', 'worker')) {
        $statePath = Join-Path $repoRoot "tmp/local/$serviceName.json"
        if (Test-RecordedServiceIsRunning -StatePath $statePath -ServiceName $serviceName) {
            throw "Stop local services before restoring a database: $serviceName is still running."
        }
    }
    Assert-NoDatabaseRuntimeUsers -MarkerDirectory $runtimeMarkerDirectory
    if (Test-Path -LiteralPath $target) {
        $safetyCopy = "$target.before-restore-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Copy-Item -LiteralPath $target -Destination $safetyCopy
    }
    Copy-Item -LiteralPath $source -Destination $target -Force
}
finally {
    Release-RestoreSentinel -SentinelPath $restoreSentinel -OwnerToken $restoreOwner
}
Write-Host "Metadata database restored: $target" -ForegroundColor Green

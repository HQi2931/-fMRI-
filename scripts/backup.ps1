[CmdletBinding()]
param(
    [string]$DatabasePath = 'work/neuroagent.db',
    [string]$BackupRoot = 'backups'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$workRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'work'))
$requiredBackupRoot = [System.IO.Path]::GetFullPath((Join-Path $repoRoot 'backups'))

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

$source = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $DatabasePath))
Assert-PathWithin -Candidate $source -Root $workRoot -Label 'DatabasePath'
$configuredSource = Resolve-ConfiguredDatabasePath `
    -DatabaseUrl (Get-ConfiguredDatabaseUrl -RepositoryRoot $repoRoot) `
    -RepositoryRoot $repoRoot
Assert-PathWithin -Candidate $configuredSource -Root $workRoot -Label 'RSFMRI_DATABASE_URL'
if (-not [string]::Equals(
    $source,
    $configuredSource,
    [System.StringComparison]::OrdinalIgnoreCase
)) {
    throw 'DatabasePath does not match RSFMRI_DATABASE_URL. Refusing to back up a different SQLite file.'
}
if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
    throw "Database does not exist: $source"
}
$backupBase = [System.IO.Path]::GetFullPath((Join-Path $repoRoot $BackupRoot))
Assert-PathWithin -Candidate $backupBase -Root $requiredBackupRoot -Label 'BackupRoot'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$destination = Join-Path (Join-Path $backupBase $stamp) 'neuroagent.db'
uv run python scripts/backup-runtime.py $source $destination
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed.' }
Write-Host "Metadata backup created: $destination" -ForegroundColor Green
Write-Host 'Raw images, generated outputs, .env, and provider secrets were not copied.'

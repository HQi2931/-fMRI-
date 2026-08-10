[CmdletBinding()]
param(
    [switch]$SkipToolInstall
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Test-CommandAvailable {
    param([Parameter(Mandatory)][string]$Name)
    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WingetTool {
    param(
        [Parameter(Mandatory)][string]$Command,
        [Parameter(Mandatory)][string]$PackageId
    )
    if (Test-CommandAvailable $Command) {
        return
    }
    if ($SkipToolInstall) {
        throw "Required command '$Command' is missing. Re-run without -SkipToolInstall."
    }
    if (-not (Test-CommandAvailable 'winget')) {
        throw "Required command '$Command' is missing and winget is unavailable."
    }
    winget install --source winget --id $PackageId --exact --silent `
        --accept-package-agreements --accept-source-agreements --disable-interactivity
    if ($LASTEXITCODE -ne 0) {
        throw "winget failed to install $PackageId (exit $LASTEXITCODE)."
    }
}

Install-WingetTool -Command 'uv' -PackageId 'astral-sh.uv'
Install-WingetTool -Command 'node' -PackageId 'OpenJS.NodeJS.LTS'
Install-WingetTool -Command 'gh' -PackageId 'GitHub.cli'
Install-WingetTool -Command 'gitleaks' -PackageId 'Gitleaks.Gitleaks'

# winget updates persistent PATH but not the current PowerShell process.
. "$PSScriptRoot/refresh-tool-path.ps1"

$nodeMajor = [int]((& node --version).TrimStart('v').Split('.')[0])
if ($nodeMajor -ne 24) {
    throw "Node 24 LTS is required; found $(& node --version)."
}

uv sync --frozen --all-groups
if ($LASTEXITCODE -ne 0) {
    throw 'uv sync failed.'
}

if (Test-Path 'web/package-lock.json') {
    Push-Location 'web'
    try {
        npm ci
        if ($LASTEXITCODE -ne 0) {
            throw 'npm ci failed.'
        }
    }
    finally {
        Pop-Location
    }
}

Write-Host 'Bootstrap complete.' -ForegroundColor Green
Write-Host 'If GitHub CLI is not authenticated, run: gh auth login' -ForegroundColor Yellow

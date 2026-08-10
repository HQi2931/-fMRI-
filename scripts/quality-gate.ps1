[CmdletBinding()]
param(
    [switch]$SkipFrontend,
    [switch]$SkipPython
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. "$PSScriptRoot/refresh-tool-path.ps1"

function Invoke-Checked {
    param(
        [Parameter(Mandatory)]
        [scriptblock]$Command,
        [Parameter(Mandatory)]
        [string]$Description
    )
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

& "$PSScriptRoot/repository-safety.ps1" -Mode Worktree
if ($LASTEXITCODE -ne 0) {
    throw 'Repository safety gate failed.'
}
& "$PSScriptRoot/docs-policy.ps1"
if ($LASTEXITCODE -ne 0) {
    throw 'Documentation policy gate failed.'
}

Invoke-Checked { git diff --check } 'Git whitespace policy'

if (-not $SkipPython) {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw 'uv is required.'
    }
    Invoke-Checked { uv sync --frozen --all-groups } 'Locked Python environment sync'
    Invoke-Checked { uv run ruff format --check . } 'Python format check'
    Invoke-Checked { uv run ruff check . } 'Python lint'
    Invoke-Checked { uv run mypy neuroagent } 'Python type check'
    Invoke-Checked { uv run pip-audit } 'Python dependency audit'
    Invoke-Checked { uv run pytest } 'Python tests'
}

if (-not $SkipFrontend -and (Test-Path 'web/package-lock.json')) {
    if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
        throw 'npm is required for the frontend quality gate.'
    }
    Push-Location 'web'
    try {
        Invoke-Checked { npm ci } 'Frontend locked dependency install'
    }
    finally {
        Pop-Location
    }
    Invoke-Checked { & "$PSScriptRoot/generate-client.ps1" } 'OpenAPI client generation'
    Push-Location 'web'
    try {
        Invoke-Checked { npm run lint } 'Frontend lint'
        Invoke-Checked { npm run typecheck } 'Frontend type check'
        Invoke-Checked { npm run test:coverage } 'Frontend unit tests'
        Invoke-Checked { npm run build } 'Frontend production build'
        Invoke-Checked { npm run test:e2e } 'Frontend mock browser E2E'
        Invoke-Checked { npm audit --audit-level=high } 'Frontend dependency audit'
    }
    finally {
        Pop-Location
    }
}

Write-Host 'All enabled quality gates passed.' -ForegroundColor Green

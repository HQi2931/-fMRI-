[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. "$PSScriptRoot/refresh-tool-path.ps1"

uv run python scripts/export-openapi.py
if ($LASTEXITCODE -ne 0) {
    throw 'OpenAPI export failed.'
}

$generator = Join-Path $repoRoot 'web/node_modules/.bin/openapi-typescript.cmd'
if (-not (Test-Path -LiteralPath $generator -PathType Leaf)) {
    throw 'Frontend dependencies are missing. Run npm ci in web/ first.'
}
& $generator 'web/openapi.json' '-o' 'web/src/api/schema.generated.ts'
if ($LASTEXITCODE -ne 0) {
    throw 'TypeScript API schema generation failed.'
}

Write-Host 'OpenAPI document and TypeScript schema generated.' -ForegroundColor Green

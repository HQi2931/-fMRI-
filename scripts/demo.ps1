[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
uv run python scripts/synthetic-demo.py
if ($LASTEXITCODE -ne 0) { throw 'Synthetic demo failed.' }

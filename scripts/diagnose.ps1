[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
. "$PSScriptRoot/refresh-tool-path.ps1"

$checks = [System.Collections.Generic.List[object]]::new()
foreach ($command in @('uv', 'python', 'node', 'npm', 'git', 'gh', 'gitleaks')) {
    $found = Get-Command $command -ErrorAction SilentlyContinue
    $checks.Add([pscustomobject]@{
        check = "command:$command"
        ok = $null -ne $found
        detail = if ($found) { $found.Source } else { 'missing' }
    })
}

$runtimeSettingsJson = uv run python -c "import json; from neuroagent.application.settings import Settings; settings = Settings.from_env(); print(json.dumps({'host': settings.host, 'port': settings.port}))"
if ($LASTEXITCODE -ne 0) {
    throw 'Unable to resolve the validated API host and port.'
}
$runtimeSettings = $runtimeSettingsJson | ConvertFrom-Json
$uriHost = if ([string]$runtimeSettings.host -eq '::1') {
    '[::1]'
}
else {
    [string]$runtimeSettings.host
}
$apiBaseUri = "http://${uriHost}:$([int]$runtimeSettings.port)"

uv run python -c "from neuroagent.application.settings import Settings; from neuroagent.infrastructure.environment import probe_environment; print(probe_environment(Settings.from_env()).model_dump_json())" |
    ForEach-Object {
        $probe = $_ | ConvertFrom-Json
        foreach ($component in $probe.components) {
            $checks.Add([pscustomobject]@{
                check = "environment:$($component.name)"
                ok = [bool]$component.available
                detail = $component.evidence
            })
        }
    }

try {
    $health = Invoke-RestMethod -Uri "$apiBaseUri/api/v1/health" -TimeoutSec 2
    $checks.Add([pscustomobject]@{ check = 'api'; ok = $health.status -eq 'ok'; detail = $health.status })
}
catch {
    $checks.Add([pscustomobject]@{ check = 'api'; ok = $false; detail = 'not running' })
}

$checks | Format-Table -AutoSize
if (@($checks | Where-Object { -not $_.ok }).Count -gt 0) { exit 1 }

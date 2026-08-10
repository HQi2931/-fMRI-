[CmdletBinding()]
param(
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
. "$PSScriptRoot/refresh-tool-path.ps1"
$runtimeDir = Join-Path $repoRoot 'tmp/local'
$logDir = Join-Path $repoRoot 'logs'
New-Item -ItemType Directory -Force -Path $runtimeDir, $logDir | Out-Null

if (-not $SkipBuild) {
    Push-Location (Join-Path $repoRoot 'web')
    try {
        npm run build
        if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
    }
    finally { Pop-Location }
}

$env:RSFMRI_SERVE_FRONTEND = 'true'

$python = Join-Path $repoRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw 'Python environment is missing. Run scripts/bootstrap.ps1 first.'
}
$runtimeSettingsJson = & $python -c "import json; from neuroagent.application.settings import Settings; settings = Settings.from_env(); print(json.dumps({'host': settings.host, 'port': settings.port}))"
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
$started = [System.Collections.Generic.List[object]]::new()

function Stop-LocalProcessTree {
    param([Parameter(Mandatory)][int]$RootPid)

    $allProcesses = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $descendants = [System.Collections.Generic.List[int]]::new()
    $frontier = [System.Collections.Generic.Queue[int]]::new()
    $frontier.Enqueue($RootPid)
    while ($frontier.Count -gt 0) {
        $parentPid = $frontier.Dequeue()
        foreach ($child in $allProcesses | Where-Object { $_.ParentProcessId -eq $parentPid }) {
            $childPid = [int]$child.ProcessId
            $descendants.Add($childPid)
            $frontier.Enqueue($childPid)
        }
    }
    for ($index = $descendants.Count - 1; $index -ge 0; $index--) {
        Stop-Process -Id $descendants[$index] -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $RootPid -Force -ErrorAction SilentlyContinue
}

function Start-LocalService {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string]$Module
    )

    $service = @{ name = $Name; module = $Module }
    $statePath = Join-Path $runtimeDir "$($service.name).json"
    if (Test-Path -LiteralPath $statePath) {
        $existing = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
        $existingProcess = Get-Process -Id $existing.pid -ErrorAction SilentlyContinue
        if ($null -ne $existingProcess) {
            $recordedStart = [datetime]::Parse([string]$existing.started_at).ToUniversalTime()
            $actualStart = $existingProcess.StartTime.ToUniversalTime()
            if ([math]::Abs(($recordedStart - $actualStart).TotalSeconds) -le 2) {
                throw "$($service.name) is already running with PID $($existing.pid)."
            }
        }
    }
    $stdout = Join-Path $logDir "$($service.name).out.log"
    $stderr = Join-Path $logDir "$($service.name).err.log"
    $process = Start-Process -FilePath $python -ArgumentList @('-m', $service.module) `
        -WorkingDirectory $repoRoot -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr -WindowStyle Hidden -PassThru
    @{
        pid = $process.Id
        started_at = $process.StartTime.ToUniversalTime().ToString('o')
        service = $service.name
    } | ConvertTo-Json | Set-Content -Encoding UTF8 $statePath

    $started.Add([pscustomobject]@{
        name = $service.name
        process = $process
        state_path = $statePath
        stderr_path = $stderr
    })
    return $process
}

try {
    $apiProcess = Start-LocalService -Name 'api' -Module 'neuroagent.api.main'
    $deadline = [datetime]::UtcNow.AddSeconds(30)
    $apiReady = $false
    while ([datetime]::UtcNow -lt $deadline) {
        $apiProcess.Refresh()
        if ($apiProcess.HasExited) {
            throw "API exited during startup. See $($started[0].stderr_path)."
        }
        try {
            $health = Invoke-WebRequest -Uri "$apiBaseUri/api/v1/health" `
                -UseBasicParsing -TimeoutSec 1
            if ($health.StatusCode -eq 200) {
                $apiReady = $true
                break
            }
        }
        catch {
            # The process can be alive while migrations and socket binding finish.
        }
        Start-Sleep -Milliseconds 200
    }
    if (-not $apiReady) {
        throw "API did not become healthy within 30 seconds. See $($started[0].stderr_path)."
    }

    $workerProcess = Start-LocalService -Name 'worker' -Module 'neuroagent.workflow.worker_main'
    Start-Sleep -Milliseconds 750
    $workerProcess.Refresh()
    if ($workerProcess.HasExited) {
        throw "Worker exited during startup. See $($started[1].stderr_path)."
    }
}
catch {
    foreach ($entry in $started) {
        $entry.process.Refresh()
        if (-not $entry.process.HasExited) {
            Stop-LocalProcessTree -RootPid $entry.process.Id
            Wait-Process -Id $entry.process.Id -Timeout 5 -ErrorAction SilentlyContinue
        }
        Remove-Item -LiteralPath $entry.state_path -Force -ErrorAction SilentlyContinue
    }
    throw
}

Write-Host "Local API and worker started at $apiBaseUri." -ForegroundColor Green

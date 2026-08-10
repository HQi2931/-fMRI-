[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $repoRoot 'tmp/local'

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

foreach ($name in @('api', 'worker')) {
    $statePath = Join-Path $runtimeDir "$name.json"
    if (-not (Test-Path -LiteralPath $statePath)) { continue }
    $state = Get-Content -Raw -Encoding UTF8 $statePath | ConvertFrom-Json
    $process = Get-Process -Id $state.pid -ErrorAction SilentlyContinue
    $removeState = $true
    if ($null -ne $process) {
        $recorded = [datetime]::Parse($state.started_at).ToUniversalTime()
        $actual = $process.StartTime.ToUniversalTime()
        if ([math]::Abs(($recorded - $actual).TotalSeconds) -gt 2) {
            throw "Refusing to stop PID $($state.pid): its start time does not match $name."
        }
        if ($PSCmdlet.ShouldProcess("$name PID $($state.pid)", 'stop local service')) {
            Stop-LocalProcessTree -RootPid $state.pid
            Wait-Process -Id $state.pid -Timeout 10 -ErrorAction SilentlyContinue
        }
        else {
            $removeState = $false
        }
    }
    if ($removeState) {
        Remove-Item -LiteralPath $statePath -Force
    }
}

Write-Host 'Local services stopped.' -ForegroundColor Green

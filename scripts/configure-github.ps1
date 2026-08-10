[CmdletBinding(SupportsShouldProcess)]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

function Invoke-NativeChecked {
    param(
        [Parameter(Mandatory)][string]$Description,
        [Parameter(Mandatory)][scriptblock]$Command
    )
    $output = & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
    return $output
}

if (-not (Get-Command gh -ErrorAction SilentlyContinue)) {
    throw 'GitHub CLI is required.'
}
gh auth status --hostname github.com
if ($LASTEXITCODE -ne 0) {
    throw 'Run gh auth login before configuring the repository.'
}

if ($PSCmdlet.ShouldProcess('HQi2931/-fMRI-', 'configure merge settings and main protection')) {
    Invoke-NativeChecked 'Configure repository merge settings' {
        gh repo edit HQi2931/-fMRI- --default-branch main --enable-auto-merge `
            --delete-branch-on-merge --enable-merge-commit=false `
            --enable-rebase-merge=false --enable-squash-merge=true
    } | Out-Null

    $protection = @{
        required_status_checks = @{
            strict = $true
            contexts = @('agent-review', 'quality-gate')
        }
        enforce_admins = $true
        required_pull_request_reviews = @{
            dismiss_stale_reviews = $false
            require_code_owner_reviews = $false
            required_approving_review_count = 0
        }
        restrictions = $null
        required_linear_history = $true
        allow_force_pushes = $false
        allow_deletions = $false
        required_conversation_resolution = $true
        lock_branch = $false
        allow_fork_syncing = $true
    } | ConvertTo-Json -Depth 8
    $tempPath = Join-Path ([System.IO.Path]::GetTempPath()) 'rsfmri-main-protection.json'
    $protection | Set-Content -Encoding UTF8 $tempPath
    Invoke-NativeChecked 'Configure main branch protection' {
        gh api --method PUT 'repos/HQi2931/-fMRI-/branches/main/protection' --input $tempPath
    } | Out-Null

    $repositoryJson = Invoke-NativeChecked 'Verify repository merge settings' {
        gh repo view HQi2931/-fMRI- --json defaultBranchRef,autoMergeAllowed,deleteBranchOnMerge,mergeCommitAllowed,rebaseMergeAllowed,squashMergeAllowed
    }
    $repository = ($repositoryJson | Out-String) | ConvertFrom-Json
    if (
        $repository.defaultBranchRef.name -ne 'main' -or
        -not $repository.autoMergeAllowed -or
        -not $repository.deleteBranchOnMerge -or
        $repository.mergeCommitAllowed -or
        $repository.rebaseMergeAllowed -or
        -not $repository.squashMergeAllowed
    ) {
        throw 'Repository merge settings verification failed.'
    }

    $protectionJson = Invoke-NativeChecked 'Verify main branch protection' {
        gh api 'repos/HQi2931/-fMRI-/branches/main/protection'
    }
    $verified = ($protectionJson | Out-String) | ConvertFrom-Json
    $contexts = @($verified.required_status_checks.contexts)
    if (
        -not $verified.required_status_checks.strict -or
        $contexts -notcontains 'agent-review' -or
        $contexts -notcontains 'quality-gate' -or
        -not $verified.enforce_admins.enabled -or
        $verified.required_pull_request_reviews.required_approving_review_count -ne 0 -or
        -not $verified.required_linear_history.enabled -or
        $verified.allow_force_pushes.enabled -or
        $verified.allow_deletions.enabled -or
        -not $verified.required_conversation_resolution.enabled
    ) {
        throw 'Main branch protection verification failed.'
    }
}

Write-Host 'GitHub repository settings configured.' -ForegroundColor Green

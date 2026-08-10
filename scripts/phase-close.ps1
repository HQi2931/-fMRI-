[CmdletBinding(SupportsShouldProcess)]
param(
    [Parameter(Mandatory)][ValidateRange(0, 99)][int]$Phase,
    [Parameter(Mandatory)][ValidatePattern('^[a-z0-9-]+$')][string]$Slug,
    [Parameter(Mandatory)][string]$Title,
    [Parameter(Mandatory)][string[]]$Paths,
    [switch]$StageZero,
    [switch]$SkipPush
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$phaseId = '{0:d2}' -f $Phase
$reviewPath = "docs/reviews/phase-$phaseId-$Slug.md"
$expectedBranch = if ($StageZero) { 'main' } else { "codex/phase-$phaseId-$Slug" }

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

if (-not (Test-Path $reviewPath)) {
    throw "Missing review report: $reviewPath"
}
$review = Get-Content -Raw -Encoding UTF8 $reviewPath
if ($review -notmatch '(?im)^decision:\s*pass\s*$') {
    throw "Review report does not contain 'decision: pass'."
}
if ($review -notmatch '(?im)^reviewed-tree:\s*([0-9a-f]{40}|[0-9a-f]{64})\s*$') {
    throw "Review report does not contain 'reviewed-tree: <git-tree-hash>'."
}

# Publishing prerequisites are checked before branch creation or staging so an
# unauthenticated workstation cannot be left with a local release commit.
if (-not $SkipPush) {
    $ghCommand = Get-Command gh -ErrorAction SilentlyContinue
    if (-not $ghCommand) {
        throw 'GitHub CLI is required for push and PR automation.'
    }
    & $ghCommand.Source auth status --hostname github.com
    if ($LASTEXITCODE -ne 0) {
        throw 'GitHub CLI is not authenticated. Run gh auth login.'
    }
}

$currentBranch = (git branch --show-current).Trim()
if (-not $StageZero -and $currentBranch -eq 'main') {
    git switch -c $expectedBranch
    $currentBranch = (git branch --show-current).Trim()
}
if ($currentBranch -ne $expectedBranch) {
    throw "Expected branch '$expectedBranch', found '$currentBranch'."
}

# The review report attests the candidate content tree and therefore cannot be
# part of its own hash. Exclude it even when a broad path such as `docs` is in
# scope, then stage the attestation only after the hash comparison succeeds.
$contentPathspecs = @($Paths) + @(
    ":(exclude)$reviewPath"
)
Invoke-NativeChecked 'Stage approved candidate content' {
    git add -- $contentPathspecs
} | Out-Null
git diff --cached --quiet -- $reviewPath
if ($LASTEXITCODE -ne 0) {
    throw 'The review report was already staged; unstage it before candidate-tree verification.'
}

# Refuse to inherit unrelated files from a previously populated index.  A
# phase close is intentionally scoped; callers must name every approved path.
$approvedPaths = @($Paths) | ForEach-Object {
    $_.Replace('\', '/').TrimEnd('/')
}
$unexpected = @(git diff --cached --name-only --diff-filter=ACMRD | Where-Object {
    $candidate = $_.Replace('\', '/')
    -not ($approvedPaths | Where-Object {
        # A deleted file is reported by its former repository-relative path.
        # Prefix matching therefore admits deletions anywhere below an
        # explicitly approved directory while still rejecting sibling paths.
        $candidate -eq $_ -or $candidate.StartsWith("$_/")
    })
})
if ($unexpected.Count -gt 0) {
    throw "The index contains files outside this phase scope: $($unexpected -join ', ')"
}

& "$PSScriptRoot/quality-gate.ps1"

# The quality gate regenerates committed contracts such as OpenAPI. Restage
# only the approved paths, then run the exact-tree mechanical checks.
Invoke-NativeChecked 'Restage approved candidate content' {
    git add -- $contentPathspecs
} | Out-Null
& "$PSScriptRoot/repository-safety.ps1" -Mode Index
if ($LASTEXITCODE -ne 0) {
    throw 'Staged repository safety gate failed.'
}
git diff --cached --check
if ($LASTEXITCODE -ne 0) {
    throw 'Staged diff failed validation.'
}
$remainingScopedChanges = @(git diff --name-only -- $contentPathspecs)
if ($remainingScopedChanges.Count -gt 0) {
    throw "Approved paths changed after staging: $($remainingScopedChanges -join ', ')"
}
$attestedTree = ((Invoke-NativeChecked 'Write candidate content tree' { git write-tree }) | Out-String).Trim()
& "$PSScriptRoot/verify-reviewed-tree.ps1" -ReviewPath $reviewPath -ActualTree $attestedTree | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Candidate content tree does not match the review attestation.'
}

Invoke-NativeChecked 'Stage review attestation' {
    git add -- $reviewPath
} | Out-Null
$remainingReviewChanges = @(git diff --name-only -- $reviewPath)
if ($remainingReviewChanges.Count -gt 0) {
    throw 'Review report changed after staging.'
}
& "$PSScriptRoot/repository-safety.ps1" -Mode Index
if ($LASTEXITCODE -ne 0) {
    throw 'Final staged repository safety gate failed.'
}
git diff --cached --check
if ($LASTEXITCODE -ne 0) {
    throw 'Final staged diff failed validation.'
}
$reviewedTree = ((Invoke-NativeChecked 'Write final reviewed tree' { git write-tree }) | Out-String).Trim()
$commitMessage = if ($StageZero) { 'chore(repo): establish project baseline' } else { "feat(phase-$phaseId): $Title" }

if ($PSCmdlet.ShouldProcess($currentBranch, "commit reviewed tree $reviewedTree")) {
    git commit -m $commitMessage
    if ($LASTEXITCODE -ne 0) {
        throw 'git commit failed.'
    }
}
$committedTree = (git rev-parse 'HEAD^{tree}').Trim()
if ($committedTree -ne $reviewedTree) {
    throw 'Committed tree differs from the reviewed staged tree.'
}

if ($SkipPush) {
    Write-Host "Committed $commitMessage without pushing." -ForegroundColor Yellow
    return
}

git push -u origin $currentBranch
if ($LASTEXITCODE -ne 0) {
    throw 'git push failed.'
}

if ($StageZero) {
    Write-Host 'Stage 0 pushed. Configure repository rules with configure-github.ps1.' -ForegroundColor Green
    return
}

$bodyPath = Join-Path ([System.IO.Path]::GetTempPath()) "rsfmri-pr-$phaseId.md"
@"
## What changed

$Title

## Review

- Reviewed content tree: `$attestedTree`
- Committed tree: `$reviewedTree`
- Agent review: `$reviewPath`
- Local quality gate: passed
- Real MATLAB/DPABI long run: not executed

## Safety

- Original research data remains read-only.
- No secrets, subject data, or generated neuroimaging artifacts are included.
"@ | Set-Content -Encoding UTF8 $bodyPath

$prUrl = Invoke-NativeChecked 'Create draft pull request' {
    gh pr create --draft --base main --head $currentBranch --title "[Phase $phaseId] $Title" --body-file $bodyPath
}
if ([string]::IsNullOrWhiteSpace(($prUrl | Out-String))) {
    throw 'GitHub CLI did not return a pull-request URL.'
}
$sha = (git rev-parse HEAD).Trim()
Invoke-NativeChecked 'Publish agent-review status' {
    gh api --method POST "repos/HQi2931/-fMRI-/statuses/$sha" `
        -f state=success -f context=agent-review -f description='Project agent review passed'
} | Out-Null
Invoke-NativeChecked 'Wait for pull-request checks' {
    gh pr checks $currentBranch --watch --fail-fast
} | Out-Null
Invoke-NativeChecked 'Mark pull request ready' {
    gh pr ready $currentBranch
} | Out-Null
Invoke-NativeChecked 'Enable automatic squash merge' {
    gh pr merge $currentBranch --auto --squash --delete-branch
} | Out-Null
Write-Host "Phase $phaseId published and queued for squash merge." -ForegroundColor Green

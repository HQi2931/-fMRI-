[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ReviewPath,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{40}([0-9a-fA-F]{24})?$')][string]$ActualTree
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path -LiteralPath $ReviewPath -PathType Leaf)) {
    throw "Missing review report: $ReviewPath"
}

$review = Get-Content -Raw -Encoding UTF8 -LiteralPath $ReviewPath
if ($review -notmatch '(?im)^decision:\s*pass\s*$') {
    throw "Review report does not contain 'decision: pass'."
}

$treeMatches = [regex]::Matches(
    $review,
    '(?im)^reviewed-tree:\s*([0-9a-f]{40}|[0-9a-f]{64})\s*$'
)
if ($treeMatches.Count -ne 1) {
    throw "Review report must contain exactly one 'reviewed-tree: <git-tree-hash>' line."
}

$expectedTree = $treeMatches[0].Groups[1].Value.ToLowerInvariant()
$normalizedActualTree = $ActualTree.ToLowerInvariant()
if ($expectedTree -ne $normalizedActualTree) {
    throw "Current candidate tree '$normalizedActualTree' differs from reviewed tree '$expectedTree'."
}

Write-Output $expectedTree

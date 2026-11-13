[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
$errors = [System.Collections.Generic.List[string]]::new()
# A rename that exists only in the working tree leaves the old path in the git
# index, so reading it would crash the gate on a file that is merely pending a
# staged rename.  Skip paths that are absent on disk; git already reports those
# as pending changes elsewhere.
$markdownFiles = @(
    git ls-files --cached --others --exclude-standard -- '*.md' |
        Where-Object { Test-Path -LiteralPath (Join-Path $repoRoot $_) -PathType Leaf }
)

# 发布版本包装只允许出现在历史记录中；其余 Markdown 不得用版本号表达当前状态。
# 详见 docs/development/documentation-standard.md 第 5 节。
$releaseFramingPattern = 'v0\.1\.0|v0\.2\.0|v1\.0\.0|MVP1\.0|MVP0\.1|发布条件|发布阻断|未发布|发布候选|发布门禁|发布说明|发布树'
$releaseFramingAllowed = @('docs/adr/', 'docs/archive/', 'docs/reviews/', 'CHANGELOG.md', 'docs/development/documentation-standard.md')

foreach ($relativePath in $markdownFiles) {
    $normalized = $relativePath.Replace('\', '/')
    $releaseFramingAllowedHere = $false
    foreach ($allowed in $releaseFramingAllowed) {
        if ($normalized.StartsWith($allowed, [System.StringComparison]::OrdinalIgnoreCase)) {
            $releaseFramingAllowedHere = $true
        }
    }
    if (-not $releaseFramingAllowedHere) {
        $lineNumber = 0
        foreach ($line in [System.IO.File]::ReadAllLines((Join-Path $repoRoot $relativePath))) {
            $lineNumber++
            if ($line -match $releaseFramingPattern) {
                $errors.Add("Release-version framing is not allowed (line ${lineNumber}) in $relativePath")
            }
        }
    }
}

foreach ($relativePath in $markdownFiles) {
    try {
        $bytes = [System.IO.File]::ReadAllBytes((Join-Path $repoRoot $relativePath))
        $content = $strictUtf8.GetString($bytes)
    }
    catch {
        $errors.Add("Markdown is not valid UTF-8: $relativePath")
        continue
    }
    if ($content.Contains([char]0xFFFD)) {
        $errors.Add("Markdown contains replacement characters: $relativePath")
    }

    $matches = [regex]::Matches($content, '\[[^\]]+\]\(([^)]+)\)')
    foreach ($match in $matches) {
        $target = $match.Groups[1].Value.Trim('<', '>')
        if ($target -match '^(https?://|mailto:|#)' -or $target -match '^[A-Za-z]:[/\\]') {
            continue
        }
        $withoutAnchor = $target.Split('#')[0]
        if (-not $withoutAnchor) {
            continue
        }
        $resolved = Join-Path (Split-Path -Parent (Join-Path $repoRoot $relativePath)) $withoutAnchor
        if (-not (Test-Path -LiteralPath $resolved)) {
            $errors.Add("Broken relative link in ${relativePath}: $target")
        }
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    throw "Documentation policy failed with $($errors.Count) finding(s)."
}

Write-Host "Documentation policy passed for $($markdownFiles.Count) Markdown file(s)." -ForegroundColor Green

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot
$strictUtf8 = [System.Text.UTF8Encoding]::new($false, $true)
$errors = [System.Collections.Generic.List[string]]::new()
$markdownFiles = @(git ls-files --cached --others --exclude-standard -- '*.md')

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

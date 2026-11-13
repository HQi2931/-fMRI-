[CmdletBinding()]
param()

# 文档规范校验：docs/ 索引覆盖、状态块与枚举、锚点、归档入链、禁用发布版本标签。
# 依据 docs/development/documentation-standard.md。

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$errors = [System.Collections.Generic.List[string]]::new()
$docsRoot = Join-Path $repoRoot 'docs'
$indexPath = Join-Path $docsRoot 'README.md'

$allowedStatus = @(
    'Draft', 'Proposed', 'Review', 'Accepted', 'Implementing',
    'Implemented', 'Superseded', 'Historical', 'Rejected'
)

# 发布版本包装的禁用模式；ADR、归档与审查报告属白名单。
# 文件可用 `<!-- check-docs:allow-release-framing -->` 显式豁免（仅规范文件与架构文档说明该规则时使用）。
$bannedVersionPattern = 'v0\.1\.0|v0\.2\.0|v1\.0\.0|MVP1\.0|MVP0\.1|发布条件|发布阻断|未发布|发布候选|发布门禁|发布说明|发布树'
$bannedVersionAllowList = @(
    'docs\adr\',
    'docs\archive\',
    'docs\reviews\'
)
$releaseFramingOptOut = '<!-- check-docs:allow-release-framing -->'

function Get-MarkdownFiles {
    Get-ChildItem -LiteralPath $docsRoot -Recurse -File -Filter '*.md' |
        Sort-Object FullName
}

function Get-Anchors {
    param([string]$Text)
    $anchors = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($line in ($Text -split "`r?`n")) {
        if ($line -match '^(#{1,6})\s+(.+?)\s*$') {
            $title = $Matches[2]
            $slug = $title.ToLowerInvariant()
            $slug = $slug -replace '`', ''
            $slug = $slug -replace '[^\p{L}\p{Nd}\s\-_]', ''
            $slug = $slug -replace '\s+', '-'
            [void]$anchors.Add($slug)
        }
    }
    return $anchors
}

$allDocs = Get-MarkdownFiles
$currentDocs = @($allDocs | Where-Object { $_.FullName -notmatch '\\archive\\' })

# --- 1. 状态块与状态枚举 ---
foreach ($file in $allDocs) {
    $relative = $file.FullName.Substring($repoRoot.Length + 1)
    $text = [System.IO.File]::ReadAllText($file.FullName)
    $isArchive = $relative -match '^docs\\archive\\'
    $isAdr = $relative -match '^docs\\adr\\'

    if ($text -match '(?m)^##\s*状态\s*$') {
        $errors.Add("Legacy status heading (use a status block): $relative")
    }

    $statusMatch = [regex]::Match($text, '(?m)^-\s*状态：(.+?)\s*$')
    if (-not $statusMatch.Success) {
        if ($isArchive) { continue }
        $errors.Add("Missing status block line '- 状态：<enum>': $relative")
        continue
    }
    $statusValue = $statusMatch.Groups[1].Value.Trim()
    if ($statusValue -notin $allowedStatus) {
        $errors.Add("Invalid status value '$statusValue' (must be one of: $($allowedStatus -join ', ')): $relative")
    }

    $typeMatch = [regex]::Match($text, '(?m)^-\s*文档类型：(.+?)\s*$')
    if (-not $typeMatch.Success -and -not $isArchive) {
        $errors.Add("Missing '- 文档类型：' field: $relative")
    }
    if ($isAdr) {
        $dateMatch = [regex]::Match($text, '(?m)^-\s*(决策日期|日期)：\d{4}-\d{2}-\d{2}\s*$')
        if (-not $dateMatch.Success) {
            $errors.Add("ADR missing '- 决策日期：YYYY-MM-DD': $relative")
        }
    }
}

# --- 2. 禁用发布版本标签 ---
foreach ($file in $allDocs) {
    $relative = $file.FullName.Substring($repoRoot.Length + 1)
    $allowed = $false
    foreach ($prefix in $bannedVersionAllowList) {
        if ($relative.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) { $allowed = $true }
    }
    if ($allowed) { continue }

    $text = [System.IO.File]::ReadAllText($file.FullName)
    if ($text.Contains($releaseFramingOptOut)) { continue }

    $lineNumber = 0
    foreach ($line in ([System.IO.File]::ReadAllLines($file.FullName))) {
        $lineNumber++
        if ($line -match $bannedVersionPattern) {
            $errors.Add("Release-version framing is not allowed in current docs (line ${lineNumber}): $relative")
        }
    }
}
foreach ($rootDoc in @('README.md', 'AGENTS.md')) {
    $full = Join-Path $repoRoot $rootDoc
    if (-not (Test-Path -LiteralPath $full)) { continue }
    $lineNumber = 0
    foreach ($line in ([System.IO.File]::ReadAllLines($full))) {
        $lineNumber++
        if ($line -match $bannedVersionPattern) {
            $errors.Add("Release-version framing is not allowed (line ${lineNumber}): $rootDoc")
        }
    }
}

# --- 3. 锚点可解析 ---
foreach ($file in $allDocs) {
    $relative = $file.FullName.Substring($repoRoot.Length + 1)
    $text = [System.IO.File]::ReadAllText($file.FullName)
    $cache = @{}
    foreach ($match in [regex]::Matches($text, '\]\(([^)\s]+\.md)(#[^)]*)\)')) {
        $target = $match.Groups[1].Value
        $anchor = $match.Groups[2].Value.TrimStart('#').ToLowerInvariant()
        $resolved = Join-Path (Split-Path -Parent $file.FullName) $target
        if (-not (Test-Path -LiteralPath $resolved)) { continue }
        if (-not $cache.ContainsKey($resolved)) {
            $cache[$resolved] = Get-Anchors -Text ([System.IO.File]::ReadAllText($resolved))
        }
        if (-not $cache[$resolved].Contains($anchor)) {
            $errors.Add("Anchor '#$anchor' not found in ${target}: $relative")
        }
    }
}

# --- 4. 归档文档不得被现行文档引用 ---
# 例外：docs/README.md 的「归档清单」小节必须登记归档目标与替代文档。
foreach ($file in $currentDocs) {
    $relative = $file.FullName.Substring($repoRoot.Length + 1)
    $text = [System.IO.File]::ReadAllText($file.FullName)
    $searchText = $text
    if ($file.FullName -eq $indexPath) {
        $idx = $text.IndexOf('## 归档清单')
        if ($idx -ge 0) { $searchText = $text.Substring(0, $idx) }
    }
    foreach ($match in [regex]::Matches($searchText, '\]\(([^)\s]*archive/[^)\s]*)\)')) {
        $errors.Add("Current doc links to an archived doc ($($match.Groups[1].Value)): $relative")
    }
}

# --- 5. 索引覆盖：docs 现行文档必须被 docs/README.md 链接，且索引链接必须可解析 ---
if (-not (Test-Path -LiteralPath $indexPath)) {
    $errors.Add('Missing docs index: docs/README.md')
}
else {
    $indexText = [System.IO.File]::ReadAllText($indexPath)
    $indexTargets = [System.Collections.Generic.HashSet[string]]::new()
    foreach ($match in [regex]::Matches($indexText, '\]\(([^)\s]+\.md)(?:#[^)]*)?\)')) {
        $resolved = Join-Path $docsRoot $match.Groups[1].Value
        if (-not (Test-Path -LiteralPath $resolved)) {
            $errors.Add("Index link does not resolve: $($match.Groups[1].Value)")
            continue
        }
        [void]$indexTargets.Add($resolved)
    }
    foreach ($file in $currentDocs) {
        if ($file.FullName -eq $indexPath) { continue }
        if (-not $indexTargets.Contains($file.FullName)) {
            $errors.Add("Doc is missing from the docs index: $($file.FullName.Substring($repoRoot.Length + 1))")
        }
    }
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    throw "Documentation standard check failed with $($errors.Count) finding(s)."
}

Write-Host "Documentation standard check passed for $($allDocs.Count) Markdown file(s) under docs/." -ForegroundColor Green

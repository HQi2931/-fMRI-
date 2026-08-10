[CmdletBinding()]
param(
    [ValidateSet('Worktree', 'Index')]
    [string]$Mode = 'Worktree'
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $repoRoot

$allowedSyntheticPrefixes = @(
    'tests/fixtures/synthetic/',
    'docs/examples/'
)
$blockedExtensions = @(
    '.nii', '.nii.gz', '.dcm', '.ima', '.img', '.hdr', '.gii', '.mnc', '.mgz', '.mgh',
    '.mat', '.npy', '.npz', '.h5', '.hdf5'
)
$blockedNames = @(
    '.env', 'participants.tsv', 'participants.csv', 'participants.xlsx'
)
$sensitiveNamePatterns = @(
    '^demographics.*\.(csv|tsv|xlsx)$',
    '^subject_manifest.*\.(csv|tsv|xlsx|json)$'
)
$secretPatterns = @(
    '-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----',
    'gh[pousr]_[A-Za-z0-9]{30,}',
    'github_pat_[A-Za-z0-9_]{40,}',
    '(?i)(api[_-]?key|secret|token)\s*[:=]\s*["''][A-Za-z0-9_\-]{20,}["'']'
)

$gitPaths = if ($Mode -eq 'Index') {
    @(git diff --cached --name-only --diff-filter=ACMR)
}
else {
    @(git ls-files --cached --others --exclude-standard)
}

$paths = $gitPaths |
    ForEach-Object { $_.Replace('\', '/') } |
    Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
    Sort-Object -Unique

$errors = [System.Collections.Generic.List[string]]::new()
$allowlistPath = Join-Path $repoRoot 'config/repository-file-allowlist.json'
$allowlisted = @{}
if (Test-Path $allowlistPath) {
    $allowlist = Get-Content -Raw -Encoding UTF8 $allowlistPath | ConvertFrom-Json
    foreach ($entry in @($allowlist.files)) {
        if ($entry.path -and $entry.sha256 -and $entry.reason) {
            $allowlisted[$entry.path.Replace('\', '/')] = $entry
        }
    }
}

foreach ($relativePath in $paths) {
    $normalized = $relativePath.ToLowerInvariant()
    $leaf = [System.IO.Path]::GetFileName($normalized)
    $extension = [System.IO.Path]::GetExtension($normalized)
    $isSynthetic = $false
    foreach ($prefix in $allowedSyntheticPrefixes) {
        if ($normalized.StartsWith($prefix)) {
            $isSynthetic = $true
            break
        }
    }

    if (-not $isSynthetic) {
        if ($blockedNames -contains $leaf) {
            $errors.Add("Blocked sensitive file name: $relativePath")
        }
        foreach ($pattern in $sensitiveNamePatterns) {
            if ($leaf -match $pattern) {
                $errors.Add("Potentially identifiable table: $relativePath")
            }
        }
        foreach ($blockedExtension in $blockedExtensions) {
            if ($normalized.EndsWith($blockedExtension)) {
                $errors.Add("Scientific binary data is not allowed in Git: $relativePath")
                break
            }
        }
    }

    $item = Get-Item -LiteralPath $relativePath
    if ($item.Length -gt 5MB) {
        $errors.Add("File exceeds the absolute 5 MiB repository limit: $relativePath")
    }
    elseif ($item.Length -gt 1MB) {
        if (-not $allowlisted.ContainsKey($relativePath)) {
            $errors.Add("File exceeds 1 MiB and is not allowlisted: $relativePath")
        }
        else {
            $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $relativePath).Hash.ToLowerInvariant()
            if ($actualHash -ne $allowlisted[$relativePath].sha256.ToLowerInvariant()) {
                $errors.Add("Allowlisted SHA-256 does not match: $relativePath")
            }
        }
    }

    if ($item.Length -le 2MB -and $extension -notin @('.png', '.jpg', '.jpeg', '.gif', '.ico', '.pdf', '.woff', '.woff2')) {
        try {
            $content = Get-Content -Raw -Encoding UTF8 -LiteralPath $relativePath -ErrorAction Stop
            if ($content.StartsWith('version https://git-lfs.github.com/spec/v1')) {
                $errors.Add("Git LFS pointers are not accepted by default: $relativePath")
            }
            if ($relativePath -ne '.env.example') {
                foreach ($pattern in $secretPatterns) {
                    if ($content -match $pattern) {
                        $errors.Add("Potential secret detected in: $relativePath")
                        break
                    }
                }
            }
        }
        catch [System.Text.DecoderFallbackException] {
            # A binary file is handled by extension and size policy above.
        }
    }
}

$symlinkEntries = @(git ls-files -s | Where-Object { $_ -match '^120000 ' })
foreach ($entry in $symlinkEntries) {
    $errors.Add("Symbolic links require explicit review: $entry")
}

if (Get-Command gitleaks -ErrorAction SilentlyContinue) {
    if ($Mode -eq 'Index') {
        gitleaks git --no-banner --redact --staged
        if ($LASTEXITCODE -ne 0) {
            $errors.Add('Gitleaks reported a finding or execution failure.')
        }
    }
    else {
        # `gitleaks dir .` also traverses ignored dependency/runtime folders.
        # Scan a temporary mirror containing only the exact Git candidates that
        # the repository policy evaluated above.
        $tempBase = [System.IO.Path]::GetTempPath()
        $scanRoot = Join-Path $tempBase ("rsfmri-gitleaks-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $scanRoot | Out-Null
        try {
            foreach ($relativePath in $paths) {
                $destination = Join-Path $scanRoot $relativePath
                $destinationParent = Split-Path -Parent $destination
                New-Item -ItemType Directory -Force -Path $destinationParent | Out-Null
                Copy-Item -LiteralPath $relativePath -Destination $destination
            }
            gitleaks dir $scanRoot --no-banner --redact
            if ($LASTEXITCODE -ne 0) {
                $errors.Add('Gitleaks reported a finding or execution failure.')
            }
        }
        finally {
            $resolvedScanRoot = [System.IO.Path]::GetFullPath($scanRoot)
            $resolvedTempBase = [System.IO.Path]::GetFullPath($tempBase)
            if ($resolvedScanRoot.StartsWith($resolvedTempBase, [System.StringComparison]::OrdinalIgnoreCase)) {
                Remove-Item -LiteralPath $resolvedScanRoot -Recurse -Force
            }
        }
    }
}
else {
    Write-Warning 'gitleaks is not installed; repository pattern scanning still ran.'
}

if ($errors.Count -gt 0) {
    $errors | ForEach-Object { Write-Error $_ }
    throw "Repository safety check failed with $($errors.Count) finding(s)."
}

Write-Host "Repository safety check passed for $($paths.Count) file(s)." -ForegroundColor Green

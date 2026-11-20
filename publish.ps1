[CmdletBinding()]
param(
    [string]$RepoPath = '.',
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$OutputDir = (Join-Path $PSScriptRoot 'packages')
)
$ErrorActionPreference = 'Stop'
$toolkit = $PSScriptRoot
$repo = (Resolve-Path -LiteralPath $RepoPath).Path
function Invoke-ToolkitGit([string[]]$GitArgs) {
    $result = & git -C $repo @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed" }
    return ($result | Out-String).Trim()
}
# Resolve all paths before changing location; toolkit may live outside the application repo.
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
$repo = Invoke-ToolkitGit -GitArgs @('rev-parse', '--show-toplevel')
if (-not (Invoke-ToolkitGit -GitArgs @('branch', '--show-current'))) { throw 'Detached HEAD: check out your local production branch first.' }
if (Invoke-ToolkitGit -GitArgs @('status', '--porcelain')) { throw 'Commit or stash all working-tree changes before publishing.' }
$remotes = (& git -C $repo remote)
if ($LASTEXITCODE -ne 0 -or $Remote -notin $remotes) { throw "Unknown remote: $Remote" }
Invoke-ToolkitGit -GitArgs @('check-ref-format', "refs/heads/$Branch") | Out-Null
Invoke-ToolkitGit -GitArgs @('fetch', '--no-tags', '--', $Remote, "refs/heads/$Branch") | Out-Null
$base = Invoke-ToolkitGit -GitArgs @('rev-parse', 'FETCH_HEAD')
$target = Invoke-ToolkitGit -GitArgs @('rev-parse', 'HEAD')
if ($base -eq $target) { Write-Host 'Nothing new to push; no deployment ZIP needed.'; return }
Invoke-ToolkitGit -GitArgs @('merge-base', '--is-ancestor', $base, $target) | Out-Null
Write-Host "Production base: $base"
Write-Host "Publish target:  $target"
Push-Location -LiteralPath $repo
try {
    # Validate before pushing, so unsupported paths or missing runtime scripts cannot publish accidentally.
    & python (Join-Path $toolkit 'package.py') $base $target --check
    if ($LASTEXITCODE -ne 0) { throw 'Packaging preflight failed. Nothing was pushed.' }
    New-Item -ItemType Directory -Path $outputPath -Force | Out-Null
    $recordPath = Join-Path $outputPath ("publish_{0}_to_{1}.json" -f $base.Substring(0,12), $target.Substring(0,12))
    $record = [ordered]@{ base_commit=$base; target_commit=$target; remote=$Remote; branch=$Branch; status='prepared' }
    $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    # The ancestry check guarantees fast-forward. The lease rejects ANY change to the observed remote base.
    & git push "--force-with-lease=refs/heads/${Branch}:$base" -- $Remote "${target}:refs/heads/${Branch}"
    if ($LASTEXITCODE -ne 0) {
        $record.status = 'push_failed_or_unknown'
        $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
        throw "Push failed or its result is unknown. Check the remote before retrying. Recovery record: $recordPath"
    }
    $record.status = 'pushed'
    $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    $remoteLine = Invoke-ToolkitGit -GitArgs @('ls-remote', '--', $Remote, "refs/heads/$Branch")
    if (($remoteLine -split '\s+')[0] -ne $target) { throw "Remote changed after push. No ZIP generated. Inspect history. Recovery record: $recordPath" }
    & python (Join-Path $toolkit 'package.py') $base $target --output-dir $outputPath
    if ($LASTEXITCODE -ne 0) { throw "Push succeeded but packaging failed. Rebuild with package.ps1 -Base $base -Target $target. Record: $recordPath" }
    $record.status = 'packaged'
    $record | ConvertTo-Json | Set-Content -LiteralPath $recordPath -Encoding UTF8
    Write-Host "Publish complete. ZIP and recovery record: $outputPath"
}
finally { Pop-Location }

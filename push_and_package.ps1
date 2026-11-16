param(
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$OutputDir = 'packages'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
function Invoke-Git([string[]]$GitArgs) {
    $result = & git @GitArgs
    if ($LASTEXITCODE -ne 0) { throw "git $($GitArgs -join ' ') failed" }
    return ($result | Out-String).Trim()
}
$inside = Invoke-Git -GitArgs @('rev-parse','--show-toplevel')
if (-not $inside) { throw 'Run inside a Git repository.' }
if ((Invoke-Git -GitArgs @('branch','--show-current')) -ne $Branch) { throw "Check out local $Branch first." }
if (Invoke-Git -GitArgs @('status','--porcelain')) { throw 'Uncommitted changes found. Commit or stash first.' }
Invoke-Git -GitArgs @('fetch',$Remote,$Branch) | Out-Null
$base = Invoke-Git -GitArgs @('rev-parse',"refs/remotes/$Remote/$Branch")
$target = Invoke-Git -GitArgs @('rev-parse',"refs/heads/$Branch")
if ($base -eq $target) { Write-Host 'Nothing new to push.'; exit 0 }
& git merge-base --is-ancestor $base $target
if ($LASTEXITCODE -ne 0) { throw "Local $Branch diverges from remote $Branch. Merge/rebase before pushing." }
# Explicit refspec; remote branch is not inferred from upstream.
& git push $Remote "refs/heads/${Branch}:refs/heads/${Branch}"
if ($LASTEXITCODE -ne 0) { throw 'Push failed. No package created.' }
# Verify remote ref still points to our exact target (detect concurrent updates).
$remoteLine = Invoke-Git -GitArgs @('ls-remote',$Remote,"refs/heads/$Branch")
$remoteSha = ($remoteLine -split '\s+')[0]
if ($remoteSha -ne $target) { throw 'Remote changed after push; package not generated. Investigate remote history.' }
& python "$PSScriptRoot/package.py" $base $target --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) { throw 'Push succeeded but packaging failed. Run package.ps1 manually.' }

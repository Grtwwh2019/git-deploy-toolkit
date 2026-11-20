[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$Base,
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$RepoPath = '.',
    [string]$OutputDir = (Join-Path $PSScriptRoot 'packages')
)
$ErrorActionPreference = 'Stop'
$outputPath = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($OutputDir)
$repo = (Resolve-Path -LiteralPath $RepoPath).Path
Push-Location -LiteralPath $repo
try {
    # Existing local Git objects are sufficient; no fetch or push is performed.
    & python (Join-Path $PSScriptRoot 'package.py') $Base $Target --output-dir $outputPath
    if ($LASTEXITCODE -ne 0) { throw 'Packaging failed.' }
}
finally { Pop-Location }

param(
    [Parameter(Mandatory=$true)][string]$Base,
    [Parameter(Mandatory=$true)][string]$Target,
    [string]$OutputDir = 'packages'
)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
& git fetch origin main
if ($LASTEXITCODE -ne 0) { throw 'Fetch failed' }
& python "$PSScriptRoot/package.py" $Base $Target --output-dir $OutputDir
if ($LASTEXITCODE -ne 0) { throw 'Packaging failed' }

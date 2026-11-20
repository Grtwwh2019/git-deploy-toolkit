# Compatibility entry point. Prefer publish.ps1 for new workflows.
[CmdletBinding()]
param(
    [string]$RepoPath = '.',
    [string]$Remote = 'origin',
    [string]$Branch = 'main',
    [string]$OutputDir = (Join-Path $PSScriptRoot 'packages')
)
$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'publish.ps1') @PSBoundParameters

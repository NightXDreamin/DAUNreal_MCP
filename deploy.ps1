# Deploys the DAUnrealMCP plugin into the target Unreal project's Plugins folder.
# Usage:  powershell -ExecutionPolicy Bypass -File .\deploy.ps1 [-ProjectDir "C:\...\DAUNrealTest"]
param(
    [string]$ProjectDir = "C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest"
)

$ErrorActionPreference = "Stop"

$pluginSource = Join-Path $PSScriptRoot "plugin"
$dest = Join-Path $ProjectDir "Plugins\DAUnrealMCP"

if (-not (Test-Path $pluginSource)) {
    throw "Plugin source not found: $pluginSource"
}
if (-not (Test-Path $ProjectDir)) {
    throw "Project directory not found: $ProjectDir"
}

if (Test-Path $dest) {
    Remove-Item $dest -Recurse -Force
}
New-Item -ItemType Directory -Path $dest -Force | Out-Null
Copy-Item -Path (Join-Path $pluginSource "*") -Destination $dest -Recurse -Force

Write-Host "Deployed DAUnrealMCP plugin to: $dest"

param(
    [string]$ProjectDir = "C:\Users\qingpulou\Documents\Unreal Projects\DAUNrealTest55"
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

New-Item -ItemType Directory -Path $dest -Force | Out-Null

# Copy uplugin file
Copy-Item -Path (Join-Path $pluginSource "DAUnrealMCP.uplugin") -Destination $dest -Force

# Copy Config
if (Test-Path (Join-Path $pluginSource "Config")) {
    Copy-Item -Path (Join-Path $pluginSource "Config") -Destination $dest -Recurse -Force
}

# Copy Source
if (Test-Path (Join-Path $pluginSource "Source")) {
    Copy-Item -Path (Join-Path $pluginSource "Source") -Destination $dest -Recurse -Force
}

Write-Host "Deployed DAUnrealMCP plugin source to: $dest"

param(
    [string]$OutputPath = "",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$sourceDir = Join-Path $repoRoot "addons\vw-app-connector"
if (-not $OutputPath) {
    $OutputPath = Join-Path $repoRoot "build\home-assistant\vw-app-connector"
}

if ($Clean -and (Test-Path -LiteralPath $OutputPath)) {
    Remove-Item -LiteralPath $OutputPath -Recurse -Force
}

New-Item -ItemType Directory -Force -Path $OutputPath | Out-Null

$staticFiles = @("config.yaml", "build.yaml", "Dockerfile", "run.sh")
foreach ($file in $staticFiles) {
    Copy-Item -LiteralPath (Join-Path $sourceDir $file) -Destination (Join-Path $OutputPath $file) -Force
}

$runtimeFiles = @("vw_app_connector.py", "mqtt_publisher.py", "requirements.txt", "LICENSE")
foreach ($file in $runtimeFiles) {
    Copy-Item -LiteralPath (Join-Path $repoRoot $file) -Destination (Join-Path $OutputPath $file) -Force
}

Write-Host "Packaged Home Assistant app at $OutputPath"

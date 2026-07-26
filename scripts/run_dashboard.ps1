$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$DashboardRoot = Join-Path $ProjectRoot "dashboard"
$Npm = "C:\Program Files\nodejs\npm.cmd"
$Config = Join-Path $ProjectRoot "config\regions.toml"
$LogDirectory = Join-Path $ProjectRoot "data\logs"
$ApiLog = Join-Path $LogDirectory "dashboard-api.log"
$ApiErrorLog = Join-Path $LogDirectory "dashboard-api.error.log"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run scripts\setup.ps1 first."
}
if (-not (Test-Path -LiteralPath $Npm)) {
    throw "Node.js was not found at $Npm."
}
if (-not (Test-Path -LiteralPath (Join-Path $DashboardRoot "node_modules"))) {
    Push-Location $DashboardRoot
    try {
        & $Npm ci --ignore-scripts --prefer-offline --no-audit --no-fund
    }
    finally {
        Pop-Location
    }
}

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
$ApiProcess = Start-Process `
    -FilePath $Python `
    -ArgumentList @(
        "-m", "ship_analysis",
        "--config", $Config,
        "dashboard-api",
        "--host", "127.0.0.1",
        "--port", "8765"
    ) `
    -WorkingDirectory $ProjectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $ApiLog `
    -RedirectStandardError $ApiErrorLog `
    -PassThru

Write-Host "AIS dashboard: http://localhost:3000"
Write-Host "Press Ctrl+C to stop the dashboard."
Start-Process "http://localhost:3000"

Push-Location $DashboardRoot
try {
    & $Npm run dev
}
finally {
    Pop-Location
    if ($ApiProcess -and -not $ApiProcess.HasExited) {
        Stop-Process -Id $ApiProcess.Id
    }
}

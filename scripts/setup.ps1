param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VirtualEnvironment = Join-Path $ProjectRoot ".venv"

& $Python -m venv $VirtualEnvironment
$VenvPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -e $ProjectRoot
& $VenvPython -m ship_analysis --config (Join-Path $ProjectRoot "config\regions.toml") init-db

Write-Host "Ready: $VenvPython"


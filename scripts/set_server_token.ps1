[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$Server,
    [string]$User = "root",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\ship_analysis_deploy_ed25519"
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $KeyPath)) {
    throw "SSH key not found: $KeyPath"
}

Write-Host "Enter the EuRIS API token for $User@$Server." -ForegroundColor Cyan
Write-Host "Paste the token and press Enter. Input will not be displayed." -ForegroundColor Cyan

& ssh -tt -i $KeyPath "$User@$Server" /usr/local/sbin/set-euris-token
if ($LASTEXITCODE -ne 0) {
    throw "Token validation or installation failed. Server configuration was not changed."
}

Write-Host "Token installed securely and collector restarted." -ForegroundColor Green

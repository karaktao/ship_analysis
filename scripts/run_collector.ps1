$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Config = Join-Path $ProjectRoot "config\regions.toml"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "Virtual environment not found. Run scripts\setup.ps1 first."
}

$TokenWasProvidedByScript = $false
if (-not (Test-Path Env:EURIS_API_TOKEN)) {
    $SecureToken = Read-Host "Paste the EuRIS API token (input is hidden)" -AsSecureString
    $TokenPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureToken)
    try {
        $env:EURIS_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR(
            $TokenPointer
        )
        $TokenWasProvidedByScript = $true
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TokenPointer)
        Remove-Variable SecureToken -ErrorAction SilentlyContinue
    }
}

try {
    & $Python -m ship_analysis --config $Config run
}
finally {
    if ($TokenWasProvidedByScript) {
        Remove-Item Env:EURIS_API_TOKEN -ErrorAction SilentlyContinue
    }
}

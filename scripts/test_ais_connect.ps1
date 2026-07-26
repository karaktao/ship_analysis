$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Net.Http

function Read-TokenSecurely {
    if (-not [string]::IsNullOrWhiteSpace($env:EURIS_API_TOKEN)) {
        return $env:EURIS_API_TOKEN
    }

    $secureToken = Read-Host "Paste the EuRIS token (input is hidden)" -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
        Remove-Variable secureToken -ErrorAction SilentlyContinue
    }
}

function Invoke-SafeProbe {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Uri,

        [Parameter(Mandatory = $true)]
        [string]$Accept,

        [Parameter(Mandatory = $true)]
        [string]$BearerToken
    )

    $handler = [System.Net.Http.HttpClientHandler]::new()
    $client = [System.Net.Http.HttpClient]::new($handler)
    try {
        $client.Timeout = [TimeSpan]::FromSeconds(30)
        $client.DefaultRequestHeaders.Authorization =
            [System.Net.Http.Headers.AuthenticationHeaderValue]::new(
                "Bearer",
                $BearerToken
            )
        $client.DefaultRequestHeaders.Accept.Clear()
        $client.DefaultRequestHeaders.Accept.Add(
            [System.Net.Http.Headers.MediaTypeWithQualityHeaderValue]::new($Accept)
        )
        $client.DefaultRequestHeaders.UserAgent.ParseAdd(
            "ship-analysis-connect-probe/0.1"
        )

        $completionOption = if ($Accept -eq "text/event-stream") {
            [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead
        }
        else {
            [System.Net.Http.HttpCompletionOption]::ResponseContentRead
        }
        $response = $client.GetAsync(
            $Uri,
            $completionOption
        ).GetAwaiter().GetResult()
        $contentType = if ($response.Content.Headers.ContentType) {
            $response.Content.Headers.ContentType.ToString()
        }
        else {
            ""
        }

        $isOpenEventStream = (
            [int]$response.StatusCode -eq 200 -and
            $contentType.StartsWith("text/event-stream")
        )
        $body = if ($isOpenEventStream) {
            ""
        }
        else {
            $response.Content.ReadAsStringAsync().GetAwaiter().GetResult()
        }

        $jsonShape = if ($isOpenEventStream) {
            "open-event-stream"
        }
        else {
            ""
        }
        if (-not $isOpenEventStream -and -not [string]::IsNullOrWhiteSpace($body)) {
            try {
                $parsed = $body | ConvertFrom-Json
                if ($parsed -is [System.Array]) {
                    $jsonShape = "array[$($parsed.Count)]"
                }
                else {
                    $propertyNames = @(
                        $parsed.PSObject.Properties | ForEach-Object { $_.Name }
                    )
                    $jsonShape = "object{" + ($propertyNames -join ",") + "}"
                }
            }
            catch {
                $jsonShape = "non-json"
            }
        }

        return [pscustomobject]@{
            Endpoint = $Uri
            Accept = $Accept
            StatusCode = [int]$response.StatusCode
            Reason = $response.ReasonPhrase
            ContentType = $contentType
            BodyBytes = [Text.Encoding]::UTF8.GetByteCount($body)
            JsonShape = $jsonShape
        }
    }
    finally {
        $client.Dispose()
        $handler.Dispose()
    }
}

$token = Read-TokenSecurely
if ([string]::IsNullOrWhiteSpace($token)) {
    throw "No EuRIS token was supplied."
}

try {
    Write-Host "1/3 Verifying that EuRIS accepts the personal token..."
    $authProbe = Invoke-SafeProbe `
        -Uri "https://www.eurisportal.eu/api/v3/tracks/followed" `
        -Accept "application/json" `
        -BearerToken $token
    $authProbe | Format-List

    if ($authProbe.StatusCode -in 401, 403) {
        Write-Host (
            "EuRIS rejected the token. Revoke/regenerate it and retry. " +
            "No Connect tests were sent."
        ) -ForegroundColor Red
        exit 2
    }

    Write-Host "2/3 Probing AISTracks Connect as documented JSON..."
    $jsonProbe = Invoke-SafeProbe `
        -Uri "https://www.eurisportal.eu/api/AISTracks/Connect" `
        -Accept "application/json" `
        -BearerToken $token
    $jsonProbe | Format-List

    Write-Host "3/3 Probing AISTracks Connect as an event stream..."
    $streamProbe = Invoke-SafeProbe `
        -Uri "https://www.eurisportal.eu/api/AISTracks/Connect" `
        -Accept "text/event-stream" `
        -BearerToken $token
    $streamProbe | Format-List

    if ($jsonProbe.StatusCode -eq 200 -or $streamProbe.StatusCode -eq 200) {
        Write-Host (
            "RESULT: Connect returned HTTP 200. Its response shape can now be " +
            "used to implement an incremental client."
        ) -ForegroundColor Green
        exit 0
    }

    Write-Host (
        "RESULT: The token was accepted by an authenticated endpoint, but " +
        "AISTracks/Connect did not accept a plain JSON or SSE GET. It likely " +
        "requires an undocumented portal-specific handshake/protocol."
    ) -ForegroundColor Yellow
    exit 3
}
finally {
    $token = $null
    Remove-Variable token -ErrorAction SilentlyContinue
}

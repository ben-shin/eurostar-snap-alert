param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z]{2}$')]
    [string]$Market
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$headers = @{
    Origin = 'https://www.eurostar.com'
    'x-market-code' = $Market
    'x-platform' = 'web'
    'x-source-url' = 'search-app/'
}
$body = [Console]::In.ReadToEnd()
try {
    $response = Invoke-WebRequest -Uri 'https://site-api.eurostar.com/gateway' -Method Post -ContentType 'application/json' -Headers $headers -Body $body -UseBasicParsing -TimeoutSec 30
    if ($response.StatusCode -ne 200) {
        throw "Eurostar returned HTTP $($response.StatusCode)"
    }
    [Console]::Out.Write($response.Content)
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}


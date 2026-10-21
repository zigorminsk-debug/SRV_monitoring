$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Write-Host 'Creating Python virtual environment…' -ForegroundColor Cyan
    py -3.11 -m venv .venv
}
$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path '.env')) {
    Copy-Item '.env.example' '.env'
    Write-Host 'Created .env from .env.example. Review it before exposing this server to the Internet.' -ForegroundColor Yellow
}

# Load simple KEY=VALUE entries so APP_HOST/PORT and optional TLS paths also reach Uvicorn.
Get-Content '.env' | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#') -and $line.Contains('=')) {
        $parts = $line.Split('=', 2)
        $key = $parts[0].Trim()
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ($key) { Set-Item -Path "Env:$key" -Value $value }
    }
}

& $Python -c 'import fastapi, uvicorn, httpx, cryptography, psutil, pyVmomi' *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing server dependencies…' -ForegroundColor Cyan
    & $Python -m pip install --disable-pip-version-check -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'pip install failed' }
}

$HostAddress = if ($env:APP_HOST) { $env:APP_HOST } else { '0.0.0.0' }
$Port = if ($env:APP_PORT) { $env:APP_PORT } else { '8765' }
$ForwardedAllowIps = if ($env:FORWARDED_ALLOW_IPS) { $env:FORWARDED_ALLOW_IPS } else { '127.0.0.1' }
$Arguments = @('-m', 'uvicorn', 'srv_monitoring.app:app', '--host', $HostAddress, '--port', $Port, '--proxy-headers', '--forwarded-allow-ips', $ForwardedAllowIps)
if ($env:SSL_CERTFILE -and $env:SSL_KEYFILE) {
    $Arguments += @('--ssl-certfile', $env:SSL_CERTFILE, '--ssl-keyfile', $env:SSL_KEYFILE)
}
Write-Host "Starting SRV Monitoring at $HostAddress`:$Port" -ForegroundColor Green
if (-not $env:ADMIN_PASSWORD) {
    Write-Host "The generated admin password is stored in $Root\data\initial-admin-password.txt" -ForegroundColor Yellow
}
& $Python @Arguments
exit $LASTEXITCODE

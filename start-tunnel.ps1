<#
.SYNOPSIS
    Starts the ngrok tunnel that TikTok's OAuth callback needs.

.DESCRIPTION
    Domain and port are read from .env (TIKTOK_REDIRECT_URI and APP_PORT), so this
    keeps working if either changes - nothing is hardcoded. ngrok runs in the
    foreground: leave the window open, closing it kills the tunnel.

.PARAMETER App
    Also start Ensembly in a separate window if nothing is listening on the port.

.EXAMPLE
    .\start-tunnel.ps1
    .\start-tunnel.ps1 -App
#>
[CmdletBinding()]
param(
    [switch]$App
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$envFile = Join-Path $root '.env'

if (-not (Test-Path $envFile)) {
    Write-Host "No .env found at $envFile" -ForegroundColor Red
    exit 1
}

# Read only the two keys we need. Nothing else from .env is touched or printed.
$port = 8420
$redirect = ''
foreach ($line in Get-Content $envFile) {
    if ($line -match '^\s*APP_PORT\s*=\s*(\d+)') { $port = [int]$Matches[1] }
    if ($line -match '^\s*TIKTOK_REDIRECT_URI\s*=\s*(\S+)') { $redirect = $Matches[1] }
}

if ($redirect -eq '') {
    Write-Host "TIKTOK_REDIRECT_URI is not set in .env - nothing to tunnel to." -ForegroundColor Red
    Write-Host "Set it to https://<domain>/api/tiktok/link/callback first."
    exit 1
}

$domain = ''
try { $domain = ([uri]$redirect).Host } catch { $domain = '' }

if ($domain -eq '') {
    Write-Host "TIKTOK_REDIRECT_URI is not a valid URL: $redirect" -ForegroundColor Red
    exit 1
}

if ($domain -notlike '*ngrok*') {
    Write-Host "TIKTOK_REDIRECT_URI points at '$domain', which is not an ngrok domain." -ForegroundColor Yellow
    Write-Host "If you have moved to your own domain, you do not need this script."
    Write-Host ""
}

if (-not (Get-Command ngrok -ErrorAction SilentlyContinue)) {
    Write-Host "ngrok is not on PATH. Install it with: winget install Ngrok.Ngrok" -ForegroundColor Red
    exit 1
}

function Test-Port($p) {
    $c = Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

# A tunnel to a dead port just serves 502s, so check before starting one.
$up = Test-Port $port

if ((-not $up) -and $App) {
    Write-Host "Nothing on :$port - starting Ensembly in a new window..." -ForegroundColor Cyan
    $python = Join-Path $root '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) { $python = 'python' }
    Start-Process -FilePath $python -ArgumentList 'run.py' -WorkingDirectory $root
    $waited = 0
    while ((-not $up) -and ($waited -lt 30)) {
        Start-Sleep -Milliseconds 500
        $waited = $waited + 1
        $up = Test-Port $port
    }
    if ($up) { Write-Host "Ensembly is up." -ForegroundColor Green }
}

if (-not $up) {
    Write-Host "Warning: nothing is listening on :$port." -ForegroundColor Yellow
    Write-Host "Start Ensembly (python run.py), or re-run this with -App."
    Write-Host ""
}

# ngrok renamed --domain to --url; pick whichever this build understands.
$help = (& ngrok http --help) | Out-String
$flag = "--domain=$domain"
if ($help -match '--url') { $flag = "--url=$domain" }

Write-Host ""
Write-Host "Tunnel:   https://$domain  ->  http://localhost:$port" -ForegroundColor Green
Write-Host "Callback: $redirect"
Write-Host "Leave this window open. Closing it kills the tunnel mid-OAuth." -ForegroundColor Yellow
Write-Host ""

& ngrok http $port $flag

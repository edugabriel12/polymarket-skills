<#
.SYNOPSIS
    Start the Polymarket copy-trader backend (:8002) + frontend (:5175) on Windows.

.DESCRIPTION
    Windows/PowerShell equivalent of dev.sh. On first run it creates the backend
    virtualenv, installs Python deps, and runs `npm install`. Each service is
    launched in its own PowerShell window so you can watch its logs; close a
    window (or Ctrl+C in it) to stop that service.

.PARAMETER WeatherOnly
    Copy only weather markets (default). Pass -WeatherOnly:$false to copy all.

.PARAMETER Reset
    Reset the paper portfolio to $10,000 (clears entries/positions) once the
    backend is up, then continues running.

.PARAMETER PollSeconds
    How often (seconds) the background loop checks saved wallets for new
    trades. Default 15. Lower it (e.g. 5) to capture new bets faster; keep it
    a few seconds at minimum so the public API isn't hammered.

.EXAMPLE
    .\dev.ps1
.EXAMPLE
    .\dev.ps1 -WeatherOnly:$false -Reset
.EXAMPLE
    .\dev.ps1 -PollSeconds 5
#>
[CmdletBinding()]
param(
    [bool]$WeatherOnly = $true,
    [switch]$Reset,
    [int]$PollSeconds = 15
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$backend = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"

# --- Pick a Python launcher --------------------------------------------------
function Get-Python {
    foreach ($cmd in @("py -3", "python", "python3")) {
        $exe = $cmd.Split(" ")[0]
        if (Get-Command $exe -ErrorAction SilentlyContinue) { return $cmd }
    }
    throw "Python not found on PATH. Install Python 3 and retry."
}
$python = Get-Python
$pyParts = $python.Split(" ")
$pyExe = $pyParts[0]
$pyArgs = @($pyParts | Select-Object -Skip 1)

# --- Backend: venv + deps (first run only) -----------------------------------
$venv = Join-Path $backend ".venv"
$venvPython = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Write-Host "[setup] creating backend virtualenv..." -ForegroundColor Cyan
    Push-Location $backend
    & $pyExe @pyArgs -m venv .venv
    & $venvPython -m pip install --quiet --upgrade pip
    & $venvPython -m pip install --quiet -r requirements.txt
    Pop-Location
}

# --- Frontend: node_modules (first run only) ---------------------------------
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "[setup] installing frontend deps (npm install)..." -ForegroundColor Cyan
    Push-Location $frontend
    npm install
    Pop-Location
}

$weatherFlag = if ($WeatherOnly) { "1" } else { "0" }

# --- Launch backend in its own window ----------------------------------------
$backendCmd = @"
Set-Location '$backend'
`$env:COPY_WEATHER_ONLY = '$weatherFlag'
`$env:COPY_DEBUG = '1'
`$env:COPY_POLL_SEC = '$PollSeconds'
Write-Host 'Backend -> http://localhost:8002  (COPY_WEATHER_ONLY=$weatherFlag, COPY_POLL_SEC=$PollSeconds)' -ForegroundColor Green
& '$venvPython' -m uvicorn app:app --port 8002 --reload
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $backendCmd

# --- Launch frontend in its own window ---------------------------------------
$frontendCmd = @"
Set-Location '$frontend'
Write-Host 'Frontend -> http://localhost:5175' -ForegroundColor Green
npm run dev -- --port 5175
"@
Start-Process powershell -ArgumentList "-NoExit", "-Command", $frontendCmd

# --- Optional: reset the paper portfolio once the backend is up --------------
if ($Reset) {
    Write-Host "[reset] waiting for backend to come up..." -ForegroundColor Cyan
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        try {
            Invoke-RestMethod "http://localhost:8002/api/health" -TimeoutSec 2 | Out-Null
            Invoke-RestMethod -Method Post "http://localhost:8002/api/portfolio/reset" | Out-Null
            Write-Host "[reset] paper portfolio reset to `$10,000." -ForegroundColor Green
            break
        } catch { }
    }
}

Write-Host ""
Write-Host "Backend  -> http://localhost:8002" -ForegroundColor Green
Write-Host "Frontend -> http://localhost:5175" -ForegroundColor Green
Write-Host "Each runs in its own window - close a window to stop that service." -ForegroundColor DarkGray

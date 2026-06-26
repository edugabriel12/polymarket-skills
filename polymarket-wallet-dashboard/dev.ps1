# Windows/PowerShell launcher for the Polymarket Wallet dashboard.
# Starts the FastAPI backend (:8001) and the Vite frontend (:5174), each in its
# own window. First run creates the backend venv and installs deps.
#
# Run:  powershell -ExecutionPolicy Bypass -File dev.ps1   (or double-click dev.bat)

$ErrorActionPreference = "Stop"
$root     = $PSScriptRoot
$backend  = Join-Path $root "backend"
$frontend = Join-Path $root "frontend"
$py       = Join-Path $backend ".venv\Scripts\python.exe"

# --- Backend: venv + dependencies (first run only) ---
if (-not (Test-Path $py)) {
    Write-Host "Creating backend venv + installing deps..." -ForegroundColor Cyan
    python -m venv (Join-Path $backend ".venv")
    & $py -m pip install --upgrade pip
    & $py -m pip install -r (Join-Path $backend "requirements.txt")
}

# --- Frontend: node modules (first run only) ---
if (-not (Test-Path (Join-Path $frontend "node_modules"))) {
    Write-Host "Installing frontend deps (npm install)..." -ForegroundColor Cyan
    Push-Location $frontend
    npm install
    Pop-Location
}

# --- Launch both servers in separate windows (close a window to stop it) ---
$uvicorn = "`"$py`" -m uvicorn app:app --port 8001 --reload"
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $uvicorn       -WorkingDirectory $backend
Start-Process -FilePath "cmd.exe" -ArgumentList "/k", "npm run dev"  -WorkingDirectory $frontend

Write-Host ""
Write-Host "Backend  -> http://localhost:8001" -ForegroundColor Green
Write-Host "Frontend -> http://localhost:5174" -ForegroundColor Green
Write-Host "Demo (offline): open the UI and click 'Ver demo', or:"
Write-Host "  curl.exe `"http://localhost:8001/api/wallet?address=demo`""

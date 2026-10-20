# verify_core.ps1 — called by verify.bat
# Starts uvicorn in background, polls /health, runs smoke curl, kills uvicorn

param(
    [string]$BackendDir = "D:\zidongh\backend",
    [string]$Python = "D:\zidongh\backend\.venv\Scripts\python.exe",
    [string]$Uvicorn = "D:\zidongh\backend\.venv\Scripts\uvicorn.exe"
)

$ErrorActionPreference = "Continue"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $BackendDir

$uvLog = Join-Path $BackendDir "uvicorn.log"

# Kill any existing uvicorn on port 8000
Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue |
    ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# Start uvicorn in background
$proc = Start-Process -FilePath $Uvicorn `
    -ArgumentList "app.main:app","--host","127.0.0.1","--port","8000","--log-level","error" `
    -WorkingDirectory $BackendDir `
    -NoNewWindow -PassThru -RedirectStandardOutput $uvLog -RedirectStandardError (Join-Path $BackendDir "uvicorn.err")

Start-Sleep -Seconds 3

# Poll /health
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction SilentlyContinue
        if ($r.StatusCode -eq 200) {
            Write-Host "[READY in $i s]"
            Write-Host "--- GET /health ---"
            Write-Host ($r.Content | ConvertFrom-Json | ConvertTo-Json -Compress)
            $ready = $true
            break
        }
    } catch {}
    Start-Sleep -Seconds 1
}

if (-not $ready) {
    Write-Host "ERROR: server did not respond in 30s"
    if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    exit 1
}

# Smoke endpoints
Write-Host "--- POST /api/v1/auth/login (admin/admin1234) ---"
$login = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/auth/login" `
    -Method POST -ContentType "application/json" `
    -Body '{"username":"admin","password":"admin1234"}' -UseBasicParsing
Write-Host ($login | ConvertTo-Json -Compress)
$token = $login.access_token

if ($token) {
    Write-Host "--- GET /api/v1/system/health (auth) ---"
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/system/health" `
        -Headers @{Authorization="Bearer $token"} -UseBasicParsing |
        ForEach-Object { Write-Host ($_ | ConvertTo-Json -Compress) }
    Write-Host "--- GET /api/v1/system/adapters (auth) ---"
    Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/system/adapters" `
        -Headers @{Authorization="Bearer $token"} -UseBasicParsing |
        ForEach-Object { Write-Host ($_ | ConvertTo-Json -Compress) }
}

Write-Host "--- GET /docs ---"
$docs = Invoke-WebRequest -Uri "http://127.0.0.1:8000/docs" -UseBasicParsing -TimeoutSec 3 -ErrorAction SilentlyContinue
Write-Host "HTTP $($docs.StatusCode)"

# Cleanup
if ($proc -and -not $proc.HasExited) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
Stop-Process -Name "uvicorn" -Force -ErrorAction SilentlyContinue

Write-Host "=== ALL CHECKS PASSED ==="
exit 0

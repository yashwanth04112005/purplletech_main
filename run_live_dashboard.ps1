param(
    [string]$StoreId = "STORE_BLR_002",
    [double]$Speed = 30,
    [string]$EventsDir = "/app/data/events"
)

$ErrorActionPreference = "Stop"

# Always run from repository root (script location)
Set-Location $PSScriptRoot

Write-Host "[1/4] Checking Docker daemon..." -ForegroundColor Cyan
$null = docker version --format '{{.Server.Version}}' 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Docker daemon is not available. Start Docker Desktop and retry."
}

Write-Host "[2/4] Starting stack (api, dashboard, postgres, redis)..." -ForegroundColor Cyan
docker compose up -d

Write-Host "[3/4] Waiting for API health..." -ForegroundColor Cyan
$maxAttempts = 30
$attempt = 0
$healthy = $false

while ($attempt -lt $maxAttempts) {
    $attempt++
    try {
        $health = Invoke-RestMethod -Uri "http://localhost:8000/health" -TimeoutSec 5
        if ($health.status -eq "healthy") {
            $healthy = $true
            break
        }
    }
    catch {
        # API may still be booting
    }
    Start-Sleep -Seconds 2
}

if (-not $healthy) {
    throw "API did not become healthy in time. Run: docker compose logs api --tail 120"
}

Write-Host "[4/4] Starting live replay for $StoreId (speed=$Speed)..." -ForegroundColor Cyan
Write-Host "Dashboard: http://localhost:3000?store=$StoreId" -ForegroundColor Green
Write-Host "Press Ctrl+C to stop replay." -ForegroundColor Yellow

docker compose exec -T api python pipeline/replay.py `
  --events-dir $EventsDir `
  --api-url http://localhost:8000 `
  --store-id $StoreId `
  --speed $Speed `
  --loop

[CmdletBinding()]
param(
    [string]$BaseDir = $PSScriptRoot,
    [string]$DownloadsDir = (Join-Path $HOME "Downloads"),
    [string]$PythonExe = "python",
    [switch]$SkipMove
)

$ErrorActionPreference = "Stop"
Set-Location $BaseDir

Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  STORE INTELLIGENCE - OFFLINE PIPELINE (NO DB/REDIS)" -ForegroundColor Cyan
Write-Host "  Events written directly to JSONL files"              -ForegroundColor Cyan
Write-Host "=======================================================" -ForegroundColor Cyan
Write-Host "  BaseDir:      $BaseDir" -ForegroundColor DarkGray
Write-Host "  DownloadsDir: $DownloadsDir" -ForegroundColor DarkGray
Write-Host "  PythonExe:    $PythonExe" -ForegroundColor DarkGray

# Step 1: Create folders
Write-Host "`n[1/7] Creating output folders..." -ForegroundColor Yellow
New-Item -ItemType Directory -Force -Path "data\clips\STORE_BLR_002" | Out-Null
New-Item -ItemType Directory -Force -Path "data\clips\STORE_BLR_003" | Out-Null
New-Item -ItemType Directory -Force -Path "data\events" | Out-Null
Write-Host "  => Folders ready." -ForegroundColor Green

# Step 2: Move videos (optional)
Write-Host "`n[2/7] Preparing input videos..." -ForegroundColor Yellow

function Move-Video {
    param(
        [string]$Source,
        [string]$Destination
    )

    if ($SkipMove) {
        if (Test-Path $Destination) {
            Write-Host "  => Using existing: $Destination" -ForegroundColor DarkGray
        } else {
            Write-Host "  !! Missing destination (SkipMove set): $Destination" -ForegroundColor Red
        }
        return
    }

    if (Test-Path $Source) {
        Move-Item -Path $Source -Destination $Destination -Force
        Write-Host "  => Moved: $(Split-Path $Source -Leaf) -> $Destination" -ForegroundColor Green
    } elseif (Test-Path $Destination) {
        Write-Host "  => Already in place: $Destination" -ForegroundColor DarkGray
    } else {
        Write-Host "  !! NOT FOUND: $Source" -ForegroundColor Red
    }
}

Move-Video (Join-Path $DownloadsDir "CAM 1.mp4") "data\clips\STORE_BLR_002\CAM_ENTRY_01.mp4"
Move-Video (Join-Path $DownloadsDir "CAM 2.mp4") "data\clips\STORE_BLR_002\CAM_FLOOR_01.mp4"
Move-Video (Join-Path $DownloadsDir "CAM 3.mp4") "data\clips\STORE_BLR_002\CAM_BILLING_01.mp4"
Move-Video (Join-Path $DownloadsDir "CAM 4.mp4") "data\clips\STORE_BLR_003\CAM_ENTRY_01.mp4"
Move-Video (Join-Path $DownloadsDir "CAM 5.mp4") "data\clips\STORE_BLR_003\CAM_FLOOR_01.mp4"

# Step 3-7: Run detection jobs (offline, no --api-url)
function Run-Detection {
    param(
        [int]$Step,
        [string]$Label,
        [string]$Video,
        [string]$Store,
        [string]$Camera,
        [string]$Out
    )

    Write-Host "`n[$Step/7] $Label..." -ForegroundColor Yellow
    if (-not (Test-Path $Video)) {
        Write-Host "  !! Skipping - video not found: $Video" -ForegroundColor Red
        return
    }

    & $PythonExe -m pipeline.detect `
        --video       $Video `
        --store-id    $Store `
        --camera-id   $Camera `
        --layout      "data\store_layout.json" `
        --output      $Out `
        --skip-frames 3 `
        --conf-thresh 0.35 `
        --device      cpu

    if ($LASTEXITCODE -eq 0) {
        $lines = (Get-Content $Out -ErrorAction SilentlyContinue | Measure-Object -Line).Lines
        Write-Host "  => Done! $lines events -> $Out" -ForegroundColor Green
    } else {
        Write-Host "  !! Pipeline failed (exit code $LASTEXITCODE)" -ForegroundColor Red
    }
}

Run-Detection 3 "STORE_BLR_002 ENTRY"   "data\clips\STORE_BLR_002\CAM_ENTRY_01.mp4"   "STORE_BLR_002" "CAM_ENTRY_01"   "data\events\STORE_BLR_002_CAM_ENTRY.jsonl"
Run-Detection 4 "STORE_BLR_002 FLOOR"   "data\clips\STORE_BLR_002\CAM_FLOOR_01.mp4"   "STORE_BLR_002" "CAM_FLOOR_01"   "data\events\STORE_BLR_002_CAM_FLOOR.jsonl"
Run-Detection 5 "STORE_BLR_002 BILLING" "data\clips\STORE_BLR_002\CAM_BILLING_01.mp4" "STORE_BLR_002" "CAM_BILLING_01" "data\events\STORE_BLR_002_CAM_BILLING.jsonl"
Run-Detection 6 "STORE_BLR_003 ENTRY"   "data\clips\STORE_BLR_003\CAM_ENTRY_01.mp4"   "STORE_BLR_003" "CAM_ENTRY_01"   "data\events\STORE_BLR_003_CAM_ENTRY.jsonl"
Run-Detection 7 "STORE_BLR_003 FLOOR"   "data\clips\STORE_BLR_003\CAM_FLOOR_01.mp4"   "STORE_BLR_003" "CAM_FLOOR_01"   "data\events\STORE_BLR_003_CAM_FLOOR.jsonl"

# Summary
Write-Host "`n=======================================================" -ForegroundColor Cyan
Write-Host "  ALL DONE! Event file summary:" -ForegroundColor Green
Write-Host "=======================================================" -ForegroundColor Cyan

Get-ChildItem "data\events\*.jsonl" -ErrorAction SilentlyContinue | ForEach-Object {
    $lines = (Get-Content $_.FullName | Measure-Object -Line).Lines
    Write-Host "  $($_.Name): $lines events" -ForegroundColor White
}

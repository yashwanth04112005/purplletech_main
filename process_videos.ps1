$ErrorActionPreference = "Stop"
$baseDir = "c:\Users\GANES\OneDrive\Desktop\Purplle Tech\store-intelligence"
Set-Location $baseDir

Write-Host "1. FOLDERS BANA RAHE HAIN..."
New-Item -ItemType Directory -Force -Path "data\clips\STORE_BLR_002" | Out-Null
New-Item -ItemType Directory -Force -Path "data\clips\STORE_BLR_003" | Out-Null
New-Item -ItemType Directory -Force -Path "data\events" | Out-Null

Write-Host "2. VIDEOS DOWNLOADS SE MOVE KAR RAHE HAIN..."
Move-Item -Path "c:\Users\GANES\Downloads\CAM 1.mp4" -Destination "data\clips\STORE_BLR_002\CAM_ENTRY_01.mp4" -Force
Move-Item -Path "c:\Users\GANES\Downloads\CAM 2.mp4" -Destination "data\clips\STORE_BLR_002\CAM_FLOOR_01.mp4" -Force
Move-Item -Path "c:\Users\GANES\Downloads\CAM 3.mp4" -Destination "data\clips\STORE_BLR_002\CAM_BILLING_01.mp4" -Force
Move-Item -Path "c:\Users\GANES\Downloads\CAM 4.mp4" -Destination "data\clips\STORE_BLR_003\CAM_ENTRY_01.mp4" -Force
Move-Item -Path "c:\Users\GANES\Downloads\CAM 5.mp4" -Destination "data\clips\STORE_BLR_003\CAM_FLOOR_01.mp4" -Force
Write-Host "=> Sabhi 5 videos successfully rename aur move ho gaye!"

Write-Host "`n3. AI VISION PIPELINE START KAR RAHE HAIN..."
Write-Host "Yahan thoda time lagega (approx 15-20 min) kyunki YOLO model har video process karega."

Write-Host "`n➤ [1/5] Processing STORE_BLR_002: ENTRY CAMERA..."
python pipeline/detect.py --video "data\clips\STORE_BLR_002\CAM_ENTRY_01.mp4" --store-id "STORE_BLR_002" --camera-id "CAM_ENTRY_01" --layout "data\store_layout.json" --output "data\events\STORE_BLR_002_CAM_ENTRY.jsonl" --api-url "http://localhost:8000"

Write-Host "`n➤ [2/5] Processing STORE_BLR_002: FLOOR CAMERA..."
python pipeline/detect.py --video "data\clips\STORE_BLR_002\CAM_FLOOR_01.mp4" --store-id "STORE_BLR_002" --camera-id "CAM_FLOOR_01" --layout "data\store_layout.json" --output "data\events\STORE_BLR_002_CAM_FLOOR.jsonl" --api-url "http://localhost:8000"

Write-Host "`n➤ [3/5] Processing STORE_BLR_002: BILLING CAMERA..."
python pipeline/detect.py --video "data\clips\STORE_BLR_002\CAM_BILLING_01.mp4" --store-id "STORE_BLR_002" --camera-id "CAM_BILLING_01" --layout "data\store_layout.json" --output "data\events\STORE_BLR_002_CAM_BILLING.jsonl" --api-url "http://localhost:8000"

Write-Host "`n➤ [4/5] Processing STORE_BLR_003: ENTRY CAMERA..."
python pipeline/detect.py --video "data\clips\STORE_BLR_003\CAM_ENTRY_01.mp4" --store-id "STORE_BLR_003" --camera-id "CAM_ENTRY_01" --layout "data\store_layout.json" --output "data\events\STORE_BLR_003_CAM_ENTRY.jsonl" --api-url "http://localhost:8000"

Write-Host "`n➤ [5/5] Processing STORE_BLR_003: FLOOR CAMERA..."
python pipeline/detect.py --video "data\clips\STORE_BLR_003\CAM_FLOOR_01.mp4" --store-id "STORE_BLR_003" --camera-id "CAM_FLOOR_01" --layout "data\store_layout.json" --output "data\events\STORE_BLR_003_CAM_FLOOR.jsonl" --api-url "http://localhost:8000"

Write-Host "`n======================================================="
Write-Host "✅ DUMBBELL UTHAO, PROTEIN PIYO! SAARE VIDEOS PROCESS HO GAYE!"
Write-Host "✅ Live metrics ab Dashboard par update ho chuke hain."
Write-Host "======================================================="

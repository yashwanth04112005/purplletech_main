#!/usr/bin/env bash
# run.sh — Process all CCTV clips for all stores and feed events into the API.
#
# Usage:
#   bash pipeline/run.sh \
#     --clips-dir   data/clips \
#     --layout      data/store_layout.json \
#     --output-dir  data/events \
#     [--api-url    http://localhost:8000] \
#     [--start-time 2026-03-03T14:00:00Z]
#
# Expected clip directory structure:
#   clips-dir/
#     STORE_BLR_002/
#       CAM_ENTRY_01.mp4
#       CAM_FLOOR_01.mp4
#       CAM_BILLING_01.mp4

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
CLIPS_DIR=""
LAYOUT=""
OUTPUT_DIR="data/events"
API_URL=""
START_TIME=""
SKIP_FRAMES=2
CONF_THRESH=0.35
DEVICE="cpu"

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case $1 in
    --clips-dir)   CLIPS_DIR="$2";   shift 2 ;;
    --layout)      LAYOUT="$2";      shift 2 ;;
    --output-dir)  OUTPUT_DIR="$2";  shift 2 ;;
    --api-url)     API_URL="$2";     shift 2 ;;
    --start-time)  START_TIME="$2";  shift 2 ;;
    --skip-frames) SKIP_FRAMES="$2"; shift 2 ;;
    --conf-thresh) CONF_THRESH="$2"; shift 2 ;;
    --device)      DEVICE="$2";      shift 2 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
done

# ── Validation ────────────────────────────────────────────────────────────────
if [[ -z "$CLIPS_DIR" || -z "$LAYOUT" ]]; then
  echo "ERROR: --clips-dir and --layout are required."
  exit 1
fi

if [[ ! -d "$CLIPS_DIR" ]]; then
  echo "ERROR: clips directory not found: $CLIPS_DIR"
  exit 1
fi

if [[ ! -f "$LAYOUT" ]]; then
  echo "ERROR: layout file not found: $LAYOUT"
  exit 1
fi

mkdir -p "$OUTPUT_DIR"

# Camera ID → camera role mapping
declare -A CAM_ROLE
CAM_ROLE["CAM_ENTRY"]="CAM_ENTRY_01"
CAM_ROLE["CAM_FLOOR"]="CAM_FLOOR_01"
CAM_ROLE["CAM_BILLING"]="CAM_BILLING_01"

echo "======================================================"
echo " Store Intelligence Detection Pipeline"
echo " Clips dir : $CLIPS_DIR"
echo " Layout    : $LAYOUT"
echo " Output    : $OUTPUT_DIR"
echo " API URL   : ${API_URL:-'(no API posting)'}"
echo " Device    : $DEVICE"
echo "======================================================"

TOTAL_EVENTS=0
FAILED_CLIPS=0

# ── Loop over stores → cameras ─────────────────────────────────────────────────
for store_dir in "$CLIPS_DIR"/*/; do
  store_id=$(basename "$store_dir")
  echo ""
  echo "── Store: $store_id ─────────────────────────────────"

  for video_file in "$store_dir"*.mp4 "$store_dir"*.avi "$store_dir"*.mkv 2>/dev/null; do
    [[ -f "$video_file" ]] || continue

    filename=$(basename "$video_file" | sed 's/\.[^.]*$//')
    output_file="$OUTPUT_DIR/${store_id}_${filename}_events.jsonl"

    # Derive camera_id from filename (e.g. CAM_ENTRY_01, CAM_FLOOR_01)
    camera_id=$(echo "$filename" | tr '[:lower:]' '[:upper:]' | sed 's/[^A-Z0-9_]/_/g')

    echo "  Processing: $filename → $output_file"

    CMD="python pipeline/detect.py \
      --video       \"$video_file\" \
      --store-id    \"$store_id\" \
      --camera-id   \"$camera_id\" \
      --layout      \"$LAYOUT\" \
      --output      \"$output_file\" \
      --skip-frames $SKIP_FRAMES \
      --conf-thresh $CONF_THRESH \
      --device      $DEVICE"

    if [[ -n "$API_URL" ]]; then
      CMD="$CMD --api-url \"$API_URL\""
    fi
    if [[ -n "$START_TIME" ]]; then
      CMD="$CMD --start-time \"$START_TIME\""
    fi

    if eval "$CMD"; then
      n_events=$(wc -l < "$output_file" 2>/dev/null || echo 0)
      echo "  ✓ Done: $n_events events → $output_file"
      TOTAL_EVENTS=$((TOTAL_EVENTS + n_events))
    else
      echo "  ✗ FAILED: $video_file"
      FAILED_CLIPS=$((FAILED_CLIPS + 1))
    fi
  done

  # Load POS transactions for this store
  pos_csv="$CLIPS_DIR/${store_id}/pos_transactions.csv"
  if [[ -f "$pos_csv" && -n "$API_URL" ]]; then
    echo "  Loading POS transactions: $pos_csv"
    python pipeline/load_pos.py --csv "$pos_csv" --store-id "$store_id"
  fi
done

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo "======================================================"
echo " Pipeline Complete"
echo " Total events emitted : $TOTAL_EVENTS"
echo " Failed clips         : $FAILED_CLIPS"
echo " Events directory     : $OUTPUT_DIR"
echo "======================================================"

if [[ $FAILED_CLIPS -gt 0 ]]; then
  exit 1
fi

"""
zone_mapper.py — Maps bounding box pixel coordinates to store zone names.

Two strategies:
  1. Polygon-based: if store_layout.json provides pixel polygons per camera, use cv2.pointPolygonTest.
  2. Grid-based fallback: divide the frame into equal grid cells and assign names from layout.

Zone names come from store_layout.json → zones → zone_id.
Camera coverage is specified per camera in the layout.
"""
import json
from typing import Optional

import cv2
import numpy as np
import structlog

log = structlog.get_logger()


class ZoneMapper:
    """
    Maps pixel (cx, cy) → zone_id string using layout polygons.
    Falls back to proportional grid if no polygon data is available for the camera.
    """

    def __init__(self, layout: dict, camera_id: str):
        self.camera_id = camera_id
        self._polygons: list[tuple[str, np.ndarray]] = []
        self._grid_zones: list[dict] = []

        self._load_layout(layout, camera_id)

    def classify(self, cx: int, cy: int) -> Optional[str]:
        """Return zone_id for a centroid (cx, cy), or None if unclassified."""
        # Try polygon-based mapping first
        for zone_id, poly in self._polygons:
            if cv2.pointPolygonTest(poly, (cx, cy), False) >= 0:
                return zone_id

        # Grid-based fallback
        for cell in self._grid_zones:
            if (cell["x0"] <= cx <= cell["x1"]) and (cell["y0"] <= cy <= cell["y1"]):
                return cell["zone_id"]

        return None   # outside all known zones

    # ── Layout loading ────────────────────────────────────────────────────────

    def _load_layout(self, layout: dict, camera_id: str):
        zones = layout.get("zones", [])
        if not zones:
            log.warning("no_zones_in_layout", camera_id=camera_id)
            return

        for zone in zones:
            zone_id = zone.get("zone_id") or zone.get("name") or "UNKNOWN"
            cameras  = zone.get("cameras", [])

            # Skip zones not covered by this camera
            if cameras and camera_id not in cameras:
                continue

            # Polygon-based
            if "polygon" in zone:
                pts = np.array(zone["polygon"], dtype=np.int32)
                self._polygons.append((zone_id, pts))
                continue

            # Bounding box region
            if all(k in zone for k in ("x0", "y0", "x1", "y1")):
                self._grid_zones.append({
                    "zone_id": zone_id,
                    "x0": zone["x0"], "y0": zone["y0"],
                    "x1": zone["x1"], "y1": zone["y1"],
                })
                continue

        # If still no geometry, create a 3×2 proportional grid as last resort
        if not self._polygons and not self._grid_zones:
            self._build_default_grid(zones)

    def _build_default_grid(self, zones: list[dict], frame_w: int = 1920, frame_h: int = 1080):
        """Divide frame into equal grid cells and assign available zone names."""
        cols, rows = 3, 2
        cell_w = frame_w // cols
        cell_h = frame_h // rows
        zone_names = [z.get("zone_id", f"ZONE_{i}") for i, z in enumerate(zones)]

        idx = 0
        for r in range(rows):
            for c in range(cols):
                if idx >= len(zone_names):
                    break
                self._grid_zones.append({
                    "zone_id": zone_names[idx],
                    "x0": c * cell_w,
                    "y0": r * cell_h,
                    "x1": (c + 1) * cell_w,
                    "y1": (r + 1) * cell_h,
                })
                idx += 1

        log.info("default_grid_built", cells=len(self._grid_zones), camera_id=self.camera_id)

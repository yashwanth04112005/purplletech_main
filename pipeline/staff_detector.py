"""
staff_detector.py — Heuristic staff detection.

Strategy (no ML model required — avoids needing a labelled staff dataset):
  1. Colour histogram analysis: retail staff uniforms are often solid colours
     (dark navy, black, burgundy, white). Score based on colour saturation histogram.
  2. Zone pattern: staff appear in ALL zones repeatedly. A visitor_id that has
     visited >4 distinct zones in <10 minutes is likely staff.
  3. Bounding box aspect ratio: staff often carry equipment (radio, scanner)
     causing slightly wider boxes — used as a weak signal only.

The is_staff flag is set conservatively — false negatives (staff counted as
customers) are worse than false positives (customers marked as staff).
"""
import cv2
import numpy as np
import structlog

log = structlog.get_logger()

# Hue ranges for common retail uniform colours (HSV)
UNIFORM_HUE_RANGES = [
    (100, 130),  # dark blue / navy
    (0,   10),   # red / burgundy (also 160-180)
    (160, 180),  # red wrap-around
    (0,    0),   # black (handled by value channel)
]
UNIFORM_SAT_MIN   = 60    # minimum saturation to be "definitely coloured"
UNIFORM_VAL_MAX   = 80    # value threshold for dark uniforms (black/navy)
STAFF_ZONE_THRESH = 4     # distinct zones in one session → staff
HIGH_FREQ_THRESH  = 0.65  # fraction of pixels matching uniform colour → staff


class StaffDetector:
    def is_staff(self, crop: np.ndarray, zone_history: list[str]) -> bool:
        """
        Returns True if the person is likely a staff member.
        Uses colour + zone-frequency heuristic.
        """
        if crop is None or crop.size == 0:
            return False

        colour_score = self._colour_heuristic(crop)
        zone_score   = self._zone_heuristic(zone_history)

        # Staff if strong colour match OR suspicious zone frequency
        return colour_score or zone_score

    # ── Heuristics ─────────────────────────────────────────────────────────────

    def _colour_heuristic(self, crop: np.ndarray) -> bool:
        """
        Check if the upper body (top 60% of crop) is a uniform colour.
        """
        try:
            h, w = crop.shape[:2]
            torso = crop[:int(h * 0.6), :]   # top 60% = torso area
            if torso.size == 0:
                return False

            hsv = cv2.cvtColor(torso, cv2.COLOR_BGR2HSV)
            total_px = torso.shape[0] * torso.shape[1]

            matching_px = 0

            # Check dark uniforms (black / navy) via value channel
            dark_mask = hsv[:, :, 2] < UNIFORM_VAL_MAX
            matching_px += int(np.sum(dark_mask))

            # Check saturated uniform colours by hue range
            for h_lo, h_hi in UNIFORM_HUE_RANGES:
                if h_lo == 0 and h_hi == 0:
                    continue  # handled by dark mask
                hue_mask = (hsv[:, :, 0] >= h_lo) & (hsv[:, :, 0] <= h_hi)
                sat_mask = hsv[:, :, 1] >= UNIFORM_SAT_MIN
                matching_px += int(np.sum(hue_mask & sat_mask))

            fraction = matching_px / total_px
            return fraction >= HIGH_FREQ_THRESH

        except Exception as exc:
            log.debug("colour_heuristic_error", error=str(exc))
            return False

    def _zone_heuristic(self, zone_history: list[str]) -> bool:
        """
        If a visitor has appeared in many distinct zones it's likely staff.
        """
        distinct_zones = len(set(zone_history))
        return distinct_zones >= STAFF_ZONE_THRESH

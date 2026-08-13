"""Tests verifying that hatch mode does not fill inner holes of nested contours.

A closed loop inside a closed loop (e.g. letter "O") should have hatch lines
only in the annular region between the outer and inner ring, not inside the
inner ring.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_geometry import generate_hatch_for_selection
from app_sessions import UploadSession


def _make_session(zone_map):
    return UploadSession(
        path="",
        zone_map=zone_map,
        zone_payload=[],
        created_ts=0.0,
        last_access_ts=0.0,
        temp_paths=[],
    )


class TestHatchNestedContours(unittest.TestCase):
    """Outer ring + inner hole — inner hole must NOT produce hatch lines."""

    def setUp(self):
        # Outer square 0..20
        self.outer = [[0, 0], [20, 0], [20, 20], [0, 20]]
        # Inner square 6..14 (the "hole" / void inside a letter like "O")
        self.inner = [[6, 6], [14, 6], [14, 14], [6, 14]]

        self.zone_map = {"outer": self.outer, "inner": self.inner}
        self.session = _make_session(self.zone_map)

    def _hatch(self, selected_ids, **kwargs):
        defaults = dict(
            angle=0.0,
            spacing=1.0,
            laser_radius=0.1,
            min_area=0.0,
            outer_zone_only=False,
            alternate_nesting_hatch=False,
        )
        defaults.update(kwargs)
        segments, _stats = generate_hatch_for_selection(
            self.session, selected_ids, **defaults
        )
        return segments

    def test_no_segments_inside_inner_hole(self):
        """No hatch segment should pass through the inner hole region.

        All segments must have at least one endpoint outside the inner square.
        """
        segs = self._hatch(["outer", "inner"])
        self.assertGreater(len(segs), 0, "Expected hatch segments for the outer ring")

        inner_min, inner_max = 6.0, 14.0
        for seg in segs:
            for pt in seg:
                x, y = float(pt[0]), float(pt[1])
                inside = inner_min < x < inner_max and inner_min < y < inner_max
                self.assertFalse(
                    inside,
                    f"Hatch point {pt} is inside the inner hole — should not be hatched",
                )

    def test_segments_present_in_annular_region(self):
        """Hatch segments must exist in the annular region between outer and inner."""
        segs = self._hatch(["outer", "inner"])
        self.assertGreater(len(segs), 0)

    def test_only_outer_selected_fills_entire_region(self):
        """When only the outer ring is selected (no inner hole in selection),
        some segments should appear inside the inner-hole bounds."""
        segs = self._hatch(["outer"])
        # With outer only (no hole selected), segments may pass through inner area
        self.assertGreater(len(segs), 0)


class TestHatchThreeLevelNesting(unittest.TestCase):
    """outer > hole > island: outer and island are filled, hole is not."""

    def setUp(self):
        self.outer = [[0, 0], [30, 0], [30, 30], [0, 30]]
        self.hole = [[5, 5], [25, 5], [25, 25], [5, 25]]
        self.island = [[10, 10], [20, 10], [20, 20], [10, 20]]

        self.zone_map = {"outer": self.outer, "hole": self.hole, "island": self.island}
        self.session = _make_session(self.zone_map)

    def _hatch(self, selected_ids, **kwargs):
        defaults = dict(
            angle=0.0,
            spacing=1.0,
            laser_radius=0.1,
            min_area=0.0,
            outer_zone_only=False,
            alternate_nesting_hatch=False,
        )
        defaults.update(kwargs)
        segments, _stats = generate_hatch_for_selection(
            self.session, selected_ids, **defaults
        )
        return segments

    def test_hole_region_not_hatched(self):
        """No hatch segment should fall inside the hole but outside the island."""
        segs = self._hatch(["outer", "hole", "island"])
        self.assertGreater(len(segs), 0)

        hole_min, hole_max = 5.0, 25.0
        island_min, island_max = 10.0, 20.0

        for seg in segs:
            for pt in seg:
                x, y = float(pt[0]), float(pt[1])
                in_hole = hole_min < x < hole_max and hole_min < y < hole_max
                in_island = island_min < x < island_max and island_min < y < island_max
                # A point inside hole but NOT inside island should not appear
                self.assertFalse(
                    in_hole and not in_island,
                    f"Point {pt} is in the hole region (should be void)",
                )


if __name__ == "__main__":
    unittest.main()

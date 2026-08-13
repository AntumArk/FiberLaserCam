"""Tests for automatic isolation-routing contour direction alternation.

Isolation-routed copper should have its offset contours grow outward for
solid outlines (so removed material leaves the trace unchanged) and inward
for holes/islands nested inside a selected outline, alternating with each
nesting level. These tests cover the automatic nesting-depth detection that
replaces having to manually flip the direction of every nested contour.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_geometry import compute_ring_nesting_depths, resolve_contour_invert_flags
from contour_offsets import generate_contour_offset_loops


class TestComputeRingNestingDepths(unittest.TestCase):
    def test_flat_polygons_have_zero_depth(self):
        a = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        b = [(20.0, 0.0), (30.0, 0.0), (30.0, 10.0), (20.0, 10.0)]
        self.assertEqual(compute_ring_nesting_depths([a, b]), [0, 0])

    def test_hole_and_island_alternate_depth(self):
        outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        hole = [(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0)]
        island = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]
        self.assertEqual(compute_ring_nesting_depths([outer, hole, island]), [0, 1, 2])


class TestResolveContourInvertFlags(unittest.TestCase):
    def setUp(self):
        self.outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        self.hole = [(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0)]
        self.island = [(4.0, 4.0), (6.0, 4.0), (6.0, 6.0), (4.0, 6.0)]
        self.zone_polys = {"outer": self.outer, "hole": self.hole, "island": self.island}

    def test_auto_alternate_flips_direction_for_holes(self):
        flags = resolve_contour_invert_flags(
            self.zone_polys, ["outer", "hole", "island"], invert_offset_direction=False
        )
        self.assertEqual(flags, {"outer": False, "hole": True, "island": False})

    def test_auto_alternate_respects_base_direction(self):
        flags = resolve_contour_invert_flags(
            self.zone_polys, ["outer", "hole", "island"], invert_offset_direction=True
        )
        self.assertEqual(flags, {"outer": True, "hole": False, "island": True})

    def test_disabling_auto_alternate_uses_uniform_direction(self):
        flags = resolve_contour_invert_flags(
            self.zone_polys,
            ["outer", "hole", "island"],
            invert_offset_direction=False,
            auto_alternate_direction=False,
        )
        self.assertEqual(flags, {"outer": False, "hole": False, "island": False})

    def test_only_selected_zones_participate_in_nesting(self):
        # If the hole is not selected, the island is only nested one level
        # deep relative to the outer outline among the *selected* zones.
        flags = resolve_contour_invert_flags(
            self.zone_polys, ["outer", "island"], invert_offset_direction=False
        )
        self.assertEqual(flags, {"outer": False, "island": True})


class TestAutoAlternateProducesOppositeWinding(unittest.TestCase):
    def test_hole_loop_offsets_opposite_direction_from_outer(self):
        outer = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        hole = [(2.0, 2.0), (8.0, 2.0), (8.0, 8.0), (2.0, 8.0)]

        outer_loops = generate_contour_offset_loops(outer, 0.5, 0.5, 1, invert_direction=False)
        hole_loops = generate_contour_offset_loops(hole, 0.5, 0.5, 1, invert_direction=True)

        self.assertEqual(len(outer_loops), 1)
        self.assertEqual(len(hole_loops), 1)

        # Outward offset of the outer square grows its bounding box.
        outer_xs = [p[0] for p in outer_loops[0]]
        self.assertLess(min(outer_xs), 0.0)
        self.assertGreater(max(outer_xs), 10.0)

        # Inverted (inward) offset of the hole shrinks its bounding box.
        hole_xs = [p[0] for p in hole_loops[0]]
        self.assertGreater(min(hole_xs), 2.0)
        self.assertLess(max(hole_xs), 8.0)


if __name__ == "__main__":
    unittest.main()

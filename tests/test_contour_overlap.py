"""Tests preventing overcutting when nearby features' isolation contours overlap.

When two separate copper features (e.g. two pads) sit close together, growing
each one's isolation-routing offset contours independently can make them cross
or overlap once the offset distance exceeds roughly half the gap between them,
causing the laser to re-cut the same area on more than one pass. Contour
generation should stop growing a pair of features' loops once they would start
to overlap (mirroring how 3D-printing slicers stop adding concentric perimeter
shells once they would collide with a neighboring feature).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from contour_offsets import generate_contour_offset_loops, generate_contour_offset_loops_multi


class TestContourOverlapPrevention(unittest.TestCase):
    def test_close_pads_stop_growing_before_they_overlap(self):
        # Two 1x1 squares with a 0.4 unit gap between them.
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        pad_b = [(1.4, 0), (2.4, 0), (2.4, 1), (1.4, 1)]

        loops_a, loops_b = generate_contour_offset_loops_multi(
            [pad_a, pad_b], start_offset=0.05, spacing=0.2, repetitions=10, invert_flags=[False, False]
        )

        # Generating each pad's contours independently (the old behavior)
        # would produce the full 10 loops per pad, which cross each other.
        independent_a = generate_contour_offset_loops(pad_a, 0.05, 0.2, 10, invert_direction=False)
        independent_b = generate_contour_offset_loops(pad_b, 0.05, 0.2, 10, invert_direction=False)
        self.assertEqual(len(independent_a), 10)
        self.assertEqual(len(independent_b), 10)

        # The multi-polygon variant must stop before the pads' loops overlap.
        self.assertLess(len(loops_a), len(independent_a))
        self.assertLess(len(loops_b), len(independent_b))

        for loop_a in loops_a:
            for loop_b in loops_b:
                max_x_a = max(p[0] for p in loop_a)
                min_x_b = min(p[0] for p in loop_b)
                self.assertLess(max_x_a, min_x_b, "kept loops must not cross into the neighboring pad")

    def test_far_apart_pads_are_unaffected(self):
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        pad_b = [(10, 0), (11, 0), (11, 1), (10, 1)]

        loops_a, loops_b = generate_contour_offset_loops_multi(
            [pad_a, pad_b], start_offset=0.05, spacing=0.2, repetitions=10, invert_flags=[False, False]
        )
        self.assertEqual(len(loops_a), 10)
        self.assertEqual(len(loops_b), 10)

    def test_single_polygon_matches_independent_generation(self):
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        (loops_a,) = generate_contour_offset_loops_multi(
            [pad_a], start_offset=0.05, spacing=0.2, repetitions=5, invert_flags=[False]
        )
        independent_a = generate_contour_offset_loops(pad_a, 0.05, 0.2, 5, invert_direction=False)
        self.assertEqual(len(loops_a), len(independent_a))


if __name__ == "__main__":
    unittest.main()

"""Regression test for the contour-offset broad-phase performance bug.

The overcut-prevention broad-phase filter previously bucketed rings into a
spatial grid whose cell size was derived from the single largest ring extent.
A board with one long trace (or any single oversized/elongated ring) next to
many small pads would force one giant cell that swallowed everything into it,
degrading collision detection to an effective O(n^2) scan and making exports
take tens of seconds. The interval-sweep broad phase should keep this fast
regardless of how differently sized the rings are.
"""
from __future__ import annotations

import random
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app_geometry import compute_ring_nesting_depths
from contour_offsets import generate_contour_offset_loops_multi


class TestContourOffsetPerformance(unittest.TestCase):
    def test_long_trace_next_to_many_small_pads_is_fast(self):
        random.seed(1)
        polys = [[(0, 50), (140, 50), (140, 50.2), (0, 50.2)]]  # long, thin trace
        for _ in range(800):
            x = random.uniform(0, 140)
            y = random.uniform(0, 100)
            if 49 < y < 51:
                y += 5
            w = h = 0.3
            polys.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])

        start = time.time()
        generate_contour_offset_loops_multi(polys, 0.01, 0.01, 5, [False] * len(polys))
        elapsed = time.time() - start

        self.assertLess(elapsed, 3.0, "contour offset generation should not require an O(n^2) scan")

    def test_nesting_depth_scales_to_many_scattered_rings(self):
        # compute_ring_nesting_depths previously recomputed each ring's area
        # on every one of O(n^2) pairwise comparisons with no bounding-box
        # pre-filter, taking ~9s for 1500 scattered rings alone -- a major
        # contributor to multi-minute full-board exports.
        random.seed(2)
        polys = []
        for _ in range(2000):
            x = random.uniform(0, 140)
            y = random.uniform(0, 100)
            w = h = 0.5
            polys.append([(x, y), (x + w, y), (x + w, y + h), (x, y + h)])

        start = time.time()
        compute_ring_nesting_depths(polys)
        elapsed = time.time() - start

        self.assertLess(elapsed, 2.0, "nesting-depth computation should not require an O(n^2) scan")


if __name__ == "__main__":
    unittest.main()

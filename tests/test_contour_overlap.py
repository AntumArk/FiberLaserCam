"""Tests preventing overcutting when nearby features' isolation contours overlap.

When two separate copper features (e.g. two pads, or a long trace running
close by a pad for only part of its length) sit close together, growing each
one's isolation-routing offset contours independently can make them cross or
overlap once the offset distance exceeds the gap between them, causing the
laser to re-cut the same area on more than one pass. Contour generation must
avoid that overcut -- but only *locally*, right where the two features'
offsets actually meet: trimming a colliding ring's whole future growth would
also erase loops far away from the conflict (e.g. the rest of a long trace),
undercutting the shape everywhere instead of just where it matters. Each
step's ring is therefore trimmed with `trim_ring_against_rings` to exclude
just the intruding stretch, leaving an open path there, while the
non-conflicting parts of the same ring (and of every later, larger step)
keep growing normally.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from contour_offsets import (
    _point_in_ring,
    generate_contour_offset_loops,
    generate_contour_offset_loops_multi,
)


class TestContourOverlapPrevention(unittest.TestCase):
    def test_close_pads_trim_locally_instead_of_stopping_growth(self):
        # Two 1x1 squares with a 0.4 unit gap between them.
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        pad_b = [(1.4, 0), (2.4, 0), (2.4, 1), (1.4, 1)]

        loops_a, loops_b = generate_contour_offset_loops_multi(
            [pad_a, pad_b], start_offset=0.05, spacing=0.2, repetitions=10, invert_flags=[False, False]
        )

        independent_a = generate_contour_offset_loops(pad_a, 0.05, 0.2, 10, invert_direction=False)
        independent_b = generate_contour_offset_loops(pad_b, 0.05, 0.2, 10, invert_direction=False)
        self.assertEqual(len(independent_a), 10)
        self.assertEqual(len(independent_b), 10)

        # Growth must NOT stop early just because the pads' offsets meet at
        # some step -- every step must still be represented (as a trimmed,
        # locally-open path once it gets close enough to the neighbor).
        self.assertEqual(len(loops_a), 10)
        self.assertEqual(len(loops_b), 10)

        # But the later steps must actually have been trimmed (open), proving
        # local trimming -- not a no-op -- kicked in near the facing sides.
        self.assertTrue(any(not closed for _, closed in loops_a))
        self.assertTrue(any(not closed for _, closed in loops_b))

        # No kept point may lie inside the *other* pad's own footprint --
        # i.e. neither shape's isolation routing re-cuts into the other pad.
        for points, _closed in loops_a:
            for point in points:
                self.assertFalse(_point_in_ring(point, pad_b), "pad_a's contour must not enter pad_b")
        for points, _closed in loops_b:
            for point in points:
                self.assertFalse(_point_in_ring(point, pad_a), "pad_b's contour must not enter pad_a")

    def test_far_apart_pads_are_unaffected(self):
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        pad_b = [(10, 0), (11, 0), (11, 1), (10, 1)]

        loops_a, loops_b = generate_contour_offset_loops_multi(
            [pad_a, pad_b], start_offset=0.05, spacing=0.2, repetitions=10, invert_flags=[False, False]
        )
        self.assertEqual(len(loops_a), 10)
        self.assertEqual(len(loops_b), 10)
        self.assertTrue(all(closed for _, closed in loops_a))
        self.assertTrue(all(closed for _, closed in loops_b))

    def test_single_polygon_matches_independent_generation(self):
        pad_a = [(0, 0), (1, 0), (1, 1), (0, 1)]
        (loops_a,) = generate_contour_offset_loops_multi(
            [pad_a], start_offset=0.05, spacing=0.2, repetitions=5, invert_flags=[False]
        )
        independent_a = generate_contour_offset_loops(pad_a, 0.05, 0.2, 5, invert_direction=False)
        self.assertEqual(len(loops_a), len(independent_a))
        self.assertTrue(all(closed for _, closed in loops_a))

    def test_touching_same_net_features_do_not_collide(self):
        # A pad and the trace soldered to it are exported as separate DXF
        # polygons but are physically one contiguous piece of copper (they
        # touch exactly at a shared edge). Growing their isolation-routing
        # offsets should not treat that touching seam as an overcut collision
        # -- both must still produce the full set of closed loops.
        pad = [(0, 0), (1, 0), (1, 1), (0, 1)]
        trace = [(1, 0.4), (2, 0.4), (2, 0.6), (1, 0.6)]

        loops_pad, loops_trace = generate_contour_offset_loops_multi(
            [pad, trace], start_offset=0.01, spacing=0.01, repetitions=5, invert_flags=[False, False]
        )
        self.assertEqual(len(loops_pad), 5)
        self.assertEqual(len(loops_trace), 5)
        self.assertTrue(all(closed for _, closed in loops_pad))
        self.assertTrue(all(closed for _, closed in loops_trace))

    def test_touching_chain_still_trims_locally_at_separate_feature(self):
        # A multi-segment trace (several touching pieces) must still trim
        # locally where it comes close to a genuinely separate feature,
        # without that stopping its (or the feature's) growth elsewhere.
        pad = [(0, 0), (1, 0), (1, 1), (0, 1)]
        trace_piece = [(1, 0.4), (2, 0.4), (2, 0.6), (1, 0.6)]
        bend_piece = [(2, 0.4), (2, 1.4), (2.2, 1.4), (2.2, 0.4)]
        other_pad = [(2.3, 0.4), (2.32, 0.4), (2.32, 0.6), (2.3, 0.6)]

        loops = generate_contour_offset_loops_multi(
            [pad, trace_piece, bend_piece, other_pad],
            start_offset=0.01,
            spacing=0.01,
            repetitions=5,
            invert_flags=[False] * 4,
        )
        for touching_loops in loops[:3]:
            self.assertGreater(len(touching_loops), 0)
        # Growth keeps going for the full repetition count...
        self.assertEqual(len(loops[3]), 5)
        # ...but at least one of other_pad's late steps got locally trimmed
        # (open) once it grew close enough to bend_piece.
        self.assertTrue(any(not closed for _, closed in loops[3]))
        for points, _closed in loops[3]:
            for point in points:
                self.assertFalse(
                    _point_in_ring(point, bend_piece), "other_pad's contour must not enter bend_piece"
                )

    def test_line_far_from_conflict_keeps_growing_past_local_collision(self):
        # Regression test for a long feature (e.g. a trace/line) that only
        # comes close to a separate feature (e.g. a pad) over a short
        # stretch of its length. Previously, colliding anywhere caused the
        # *entire* ring to stop growing for every future step, undercutting
        # the rest of the long feature far away from the actual conflict.
        line = [(0, 0), (1, 0), (1, 20), (0, 20)]
        square = [(2, 0), (7, 0), (7, 5), (2, 5)]

        loops_line, loops_square = generate_contour_offset_loops_multi(
            [line, square], start_offset=0.1, spacing=0.2, repetitions=10, invert_flags=[False, False]
        )

        # Both features must keep growing for every requested step, even
        # though they collide locally near y in [0, 5].
        self.assertEqual(len(loops_line), 10)
        self.assertEqual(len(loops_square), 10)

        # The far end of the line (y close to 20), well away from the
        # square, must still reach the full nominal offset on the last step
        # instead of being capped at whatever step first collided near y=0.
        last_step_offset = 0.1 + (9 * 0.2)
        far_points = [p for points, _closed in loops_line for p in points if p[1] > 15]
        self.assertTrue(far_points, "the line's far end must still have contour points")
        max_y = max(p[1] for p in far_points)
        self.assertAlmostEqual(max_y, 20.0 + last_step_offset, places=6)

        # And the contour must never actually enter the square's footprint.
        for points, _closed in loops_line:
            for point in points:
                self.assertFalse(_point_in_ring(point, square), "the line's contour must not enter the square")


if __name__ == "__main__":
    unittest.main()

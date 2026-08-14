"""Tests for mirroring drill hole X positions (offline_export._mirror_holes_x).

Drilling is mirrored left/right across the board so it can be done in the
same physical board orientation as F.Cu isolation routing, without flipping
the board over between the two passes.
"""
from __future__ import annotations

import unittest

import pcbnew_geometry as pg
from offline_export import _mirror_holes_x


class MirrorHolesXWithoutBoardTests(unittest.TestCase):
    """These don't need pcbnew - they exercise the hole-bbox-based fallback
    axis used when no board is available (plain-text .kicad_pcb parse path)."""

    def test_mirrors_across_hole_set_bounding_box(self):
        holes = [(0.0, 1.0, 0.5), (10.0, 2.0, 0.6)]
        mirrored = _mirror_holes_x(holes)
        self.assertEqual(mirrored, [(10.0, 1.0, 0.5), (0.0, 2.0, 0.6)])

    def test_preserves_y_and_diameter(self):
        holes = [(2.0, 3.5, 0.8)]
        mirrored = _mirror_holes_x(holes)
        self.assertEqual(len(mirrored), 1)
        x, y, d = mirrored[0]
        self.assertAlmostEqual(y, 3.5)
        self.assertAlmostEqual(d, 0.8)

    def test_empty_input_returns_empty(self):
        self.assertEqual(_mirror_holes_x([]), [])


@unittest.skipUnless(pg.is_pcbnew_available(), "pcbnew is not importable in this environment")
class MirrorHolesXWithBoardTests(unittest.TestCase):
    def test_mirrors_across_board_edge_cuts_span(self):
        import pcbnew

        board = pcbnew.BOARD()
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
        shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10)))
        shape.SetLayer(pcbnew.Edge_Cuts)
        board.Add(shape)

        # A hole near the left edge should end up near the right edge of the
        # same board footprint, not mirrored around its own position.
        holes = [(1.0, 5.0, 0.8)]
        mirrored = _mirror_holes_x(holes, board)
        self.assertEqual(len(mirrored), 1)
        x, y, d = mirrored[0]
        self.assertAlmostEqual(x, 19.0, places=1)
        self.assertAlmostEqual(y, 5.0, places=6)
        self.assertAlmostEqual(d, 0.8, places=6)


if __name__ == "__main__":
    unittest.main()

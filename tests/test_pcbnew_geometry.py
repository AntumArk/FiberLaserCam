"""Tests for the pcbnew-native geometry path (pcbnew_geometry.py).

These tests require the ``pcbnew`` Python module to be importable, which is
only true when running under KiCad's own Python (its bundled interpreter, or
a system KiCad install with pcbnew on sys.path). They are skipped everywhere
else, including this repo's normal CI/test environment.
"""
from __future__ import annotations

import unittest

import pcbnew_geometry as pg


@unittest.skipUnless(pg.is_pcbnew_available(), "pcbnew is not importable in this environment")
class PcbnewGeometryTests(unittest.TestCase):
    def _make_board(self):
        import pcbnew

        board = pcbnew.BOARD()

        def add_pad_at(x_mm: float, y_mm: float, size_mm: float, net_code: int):
            fp = pcbnew.FOOTPRINT(board)
            board.Add(fp)
            net = pcbnew.NETINFO_ITEM(board, f"net{net_code}", net_code)
            pad = pcbnew.PAD(fp)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(size_mm), pcbnew.FromMM(size_mm)))
            pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
            pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
            lset = pcbnew.LSET()
            lset.AddLayer(pcbnew.F_Cu)
            pad.SetLayerSet(lset)
            pad.SetNet(net)
            fp.Add(pad)
            return pad

        add_pad_at(0, 0, 1.0, 1)
        add_pad_at(5, 0, 1.0, 2)
        return board

    def test_touching_same_net_pads_merge_into_one_polygon(self):
        import pcbnew

        board = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(board)
        board.Add(fp)

        net = pcbnew.NETINFO_ITEM(board, "net1", 1)
        lset = pcbnew.LSET()
        lset.AddLayer(pcbnew.F_Cu)

        def add_pad(x_mm, y_mm, w_mm, h_mm):
            pad = pcbnew.PAD(fp)
            pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(w_mm), pcbnew.FromMM(h_mm)))
            pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
            pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(x_mm), pcbnew.FromMM(y_mm)))
            pad.SetLayerSet(lset)
            pad.SetNet(net)
            fp.Add(pad)
            return pad

        # Two touching 1x1mm pads, centers 1mm apart -> share an edge.
        add_pad(0.0, 0.0, 1.0, 1.0)
        add_pad(1.0, 0.0, 1.0, 1.0)

        layer_id = pg.resolve_layer_id(board, "F.Cu")
        net_polys = pg.build_net_polygons_for_layer(board, layer_id)
        self.assertEqual(len(net_polys), 1)
        merged = next(iter(net_polys.values()))
        self.assertEqual(merged.OutlineCount(), 1, "touching same-net pads should merge into one outline")


    def test_generate_contour_offsets_from_board_returns_loops(self):
        board = self._make_board()
        loops = pg.generate_contour_offsets_from_board(
            board, "F.Cu", start_offset=0.1, spacing=0.1, repetitions=2
        )
        self.assertTrue(loops)
        for loop in loops:
            self.assertGreaterEqual(len(loop), 3)

    def test_get_drill_holes_from_board_uses_pad_resolved_position(self):
        """A pad's hole position must come from pcbnew's own resolved
        absolute position (footprint position + rotation + mirroring already
        applied), not a hand-rolled re-derivation of the local pad offset."""
        import pcbnew

        board = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(board)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(5)))
        fp.SetOrientationDegrees(90)
        board.Add(fp)

        net = pcbnew.NETINFO_ITEM(board, "net1", 1)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.5), pcbnew.FromMM(1.5)))
        pad.SetShape(pcbnew.PAD_SHAPE_CIRCLE)
        pad.SetDrillSize(pcbnew.VECTOR2I(pcbnew.FromMM(0.8), pcbnew.FromMM(0.8)))
        # Pad offset 1mm to the "right" of the footprint origin, before the
        # footprint's own 90-degree rotation is applied.
        pad.SetFPRelativePosition(pcbnew.VECTOR2I(pcbnew.FromMM(1), 0))
        pad.SetNet(net)
        fp.Add(pad)

        holes = pg.get_drill_holes_from_board(board)
        self.assertEqual(len(holes), 1)
        x, y, diameter = holes[0]
        # Y is negated relative to pad.GetPosition() to match KiCad's own
        # DXF-export coordinate convention -- see get_drill_holes_from_board.
        expected_x, expected_y = pcbnew.ToMM(pad.GetPosition().x), -pcbnew.ToMM(pad.GetPosition().y)
        self.assertAlmostEqual(x, expected_x, places=6)
        self.assertAlmostEqual(y, expected_y, places=6)
        self.assertAlmostEqual(diameter, 0.8, places=6)

    def test_get_drill_holes_from_board_includes_vias_as_through_holes(self):
        import pcbnew

        board = pcbnew.BOARD()
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(30)))
        via.SetDrill(pcbnew.FromMM(0.4))
        board.Add(via)

        holes = pg.get_drill_holes_from_board(board)
        self.assertEqual(len(holes), 1)
        x, y, diameter = holes[0]
        self.assertAlmostEqual(x, 20.0, places=6)
        self.assertAlmostEqual(y, -30.0, places=6)
        self.assertAlmostEqual(diameter, 0.4, places=6)

    def test_get_drill_holes_from_board_skips_smd_pads(self):
        import pcbnew

        board = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(board)
        board.Add(fp)
        pad = pcbnew.PAD(fp)
        pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
        pad.SetSize(pcbnew.VECTOR2I(pcbnew.FromMM(1.0), pcbnew.FromMM(1.0)))
        pad.SetShape(pcbnew.PAD_SHAPE_RECTANGLE)
        pad.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(3), pcbnew.FromMM(4)))
        fp.Add(pad)

        holes = pg.get_drill_holes_from_board(board)
        self.assertEqual(holes, [])

    def test_get_board_x_span_mm_returns_edge_cuts_extent(self):
        import pcbnew

        board = pcbnew.BOARD()
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
        shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10)))
        shape.SetLayer(pcbnew.Edge_Cuts)
        board.Add(shape)

        span = pg.get_board_x_span_mm(board)
        self.assertIsNotNone(span)
        left, right = span
        self.assertAlmostEqual(left, 0.0, delta=0.2)
        self.assertAlmostEqual(right, 20.0, delta=0.2)

    def test_get_board_x_span_mm_returns_none_without_edge_cuts(self):
        import pcbnew

        board = pcbnew.BOARD()
        span = pg.get_board_x_span_mm(board)
        self.assertIsNone(span)

    def test_get_board_mirror_axis_mm_returns_bbox_center(self):
        import pcbnew

        board = pcbnew.BOARD()
        shape = pcbnew.PCB_SHAPE(board)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
        shape.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10)))
        shape.SetLayer(pcbnew.Edge_Cuts)
        board.Add(shape)

        axis = pg.get_board_mirror_axis_mm(board)
        self.assertIsNotNone(axis)
        self.assertAlmostEqual(axis, 10.0, delta=0.2)

    def test_get_board_mirror_axis_mm_returns_none_without_edge_cuts(self):
        import pcbnew

        board = pcbnew.BOARD()
        self.assertIsNone(pg.get_board_mirror_axis_mm(board))

    def test_build_net_polygons_for_layer_edge_cuts_returns_outer_only(self):
        """Edge.Cuts has no pads/tracks/zones, so it must be resolved through
        the board's own outline geometry -- and any interior holes/cutouts
        drawn on Edge.Cuts (e.g. mounting-hole circles) must be dropped so a
        cutting pass only ever cuts the outer board profile."""
        import pcbnew

        board = pcbnew.BOARD()

        outer = pcbnew.PCB_SHAPE(board)
        outer.SetShape(pcbnew.SHAPE_T_RECT)
        outer.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
        outer.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10)))
        outer.SetLayer(pcbnew.Edge_Cuts)
        board.Add(outer)

        hole = pcbnew.PCB_SHAPE(board)
        hole.SetShape(pcbnew.SHAPE_T_CIRCLE)
        hole.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(5)))
        hole.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(12), pcbnew.FromMM(5)))
        hole.SetLayer(pcbnew.Edge_Cuts)
        board.Add(hole)

        layer_id = pg.resolve_layer_id(board, "Edge.Cuts")
        net_polys = pg.build_net_polygons_for_layer(board, layer_id)

        self.assertEqual(len(net_polys), 1)
        poly = next(iter(net_polys.values()))
        self.assertGreater(poly.OutlineCount(), 0, "Edge.Cuts must yield the outer board contour")
        total_holes = sum(poly.HoleCount(i) for i in range(poly.OutlineCount()))
        self.assertEqual(total_holes, 0, "Edge.Cuts polygons must exclude interior holes/cutouts")

    def test_generate_contour_offsets_from_board_edge_cuts_returns_outer_loop(self):
        board = self._make_edge_cuts_board_with_hole()
        loops = pg.generate_contour_offsets_from_board(
            board, "Edge.Cuts", start_offset=0.1, spacing=0.1, repetitions=1
        )
        self.assertEqual(len(loops), 1, "only the outer board profile should be cut, not the interior hole")

    def _make_edge_cuts_board_with_hole(self):
        import pcbnew

        board = pcbnew.BOARD()

        outer = pcbnew.PCB_SHAPE(board)
        outer.SetShape(pcbnew.SHAPE_T_RECT)
        outer.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(0), pcbnew.FromMM(0)))
        outer.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(20), pcbnew.FromMM(10)))
        outer.SetLayer(pcbnew.Edge_Cuts)
        board.Add(outer)

        hole = pcbnew.PCB_SHAPE(board)
        hole.SetShape(pcbnew.SHAPE_T_CIRCLE)
        hole.SetStart(pcbnew.VECTOR2I(pcbnew.FromMM(10), pcbnew.FromMM(5)))
        hole.SetEnd(pcbnew.VECTOR2I(pcbnew.FromMM(12), pcbnew.FromMM(5)))
        hole.SetLayer(pcbnew.Edge_Cuts)
        board.Add(hole)

        return board

    def test_generate_contour_offsets_from_board_mirrors_back_layer(self):
        """Passing mirror_axis_mm should mirror every generated loop's X
        coordinate across that axis (Y unchanged) -- used for B.Cu/B.Mask so
        the output lines up once the board is physically flipped over."""
        board = self._make_board()
        axis = 2.5

        plain_loops = pg.generate_contour_offsets_from_board(
            board, "F.Cu", start_offset=0.1, spacing=0.1, repetitions=1
        )
        mirrored_loops = pg.generate_contour_offsets_from_board(
            board, "F.Cu", start_offset=0.1, spacing=0.1, repetitions=1, mirror_axis_mm=axis
        )

        self.assertEqual(len(plain_loops), len(mirrored_loops))
        for plain, mirrored in zip(plain_loops, mirrored_loops):
            self.assertEqual(len(plain), len(mirrored))
            for (px, py), (mx, my) in zip(plain, mirrored):
                self.assertAlmostEqual(mx, 2.0 * axis - px, places=6)
                self.assertAlmostEqual(my, py, places=6)


if __name__ == "__main__":
    unittest.main()

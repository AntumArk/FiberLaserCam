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


if __name__ == "__main__":
    unittest.main()

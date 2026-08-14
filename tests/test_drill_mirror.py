"""Tests for the drill-hole coordinate convention (offline_export.py).

Drilling is done in the same (front, unflipped) orientation shown in the PCB
editor -- so drill holes are never X-mirrored. What they *do* need is a Y
negation to match KiCad's own DXF-export coordinate convention (pcbnew's
internal Y axis increases downward; KiCad's DXF plotter negates Y on the way
out, and this app's pcbnew-native geometry source does the same -- see
``pcbnew_geometry._contour_to_ring``). Without that negation, drill holes
end up vertically flipped relative to F.Cu/B.Cu isolation routing exported
anywhere else in this app.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import pcbnew_geometry as pg
from offline_export import _collect_kicad_drill_holes, _collect_kicad_drill_holes_from_text


_MINIMAL_BOARD_TEXT = """
(kicad_pcb
  (version 20240108)
  (generator "test")
  (via (at 5 3) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") (net 0))
  (footprint "test:fp" (layer "F.Cu")
    (at 10 5 90)
    (pad "1" thru_hole circle (at 1 0) (size 1.5 1.5) (drill 0.8) (layers "*.Cu"))
  )
)
"""


class DrillHolesFromTextTests(unittest.TestCase):
    def _write_board(self) -> Path:
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_TEXT)
        return board_path

    def test_from_text_returns_raw_unmirrored_positions(self):
        board_path = self._write_board()
        holes = _collect_kicad_drill_holes_from_text(board_path)
        by_diameter = {round(d, 4): (x, y) for x, y, d in holes}

        # Via: taken verbatim from its (at ...) position.
        self.assertIn(0.4, by_diameter)
        self.assertAlmostEqual(by_diameter[0.4][0], 5.0, places=6)
        self.assertAlmostEqual(by_diameter[0.4][1], 3.0, places=6)

        # Pad: footprint at (10, 5) rotated 90 degrees, pad-local (1, 0)
        # rotates to (0, 1) -> absolute (10, 6).
        self.assertIn(0.8, by_diameter)
        self.assertAlmostEqual(by_diameter[0.8][0], 10.0, places=6)
        self.assertAlmostEqual(by_diameter[0.8][1], 6.0, places=6)

    def test_collect_kicad_drill_holes_negates_y_to_match_dxf_convention(self):
        """Without pcbnew, ``_collect_kicad_drill_holes`` falls back to the
        plain-text parse above, but must still negate Y (X unchanged) so it
        matches the same coordinate convention kicad-cli's DXF export (and
        this app's pcbnew-native geometry source) both use."""
        if pg.is_pcbnew_available():
            self.skipTest("pcbnew is importable; the text-parse fallback path is not used here")

        board_path = self._write_board()
        raw_holes = _collect_kicad_drill_holes_from_text(board_path)
        holes = _collect_kicad_drill_holes(board_path)

        raw_by_diameter = {round(d, 4): (x, y) for x, y, d in raw_holes}
        by_diameter = {round(d, 4): (x, y) for x, y, d in holes}

        for diameter, (raw_x, raw_y) in raw_by_diameter.items():
            x, y = by_diameter[diameter]
            self.assertAlmostEqual(x, raw_x, places=6)
            self.assertAlmostEqual(y, -raw_y, places=6)


@unittest.skipUnless(pg.is_pcbnew_available(), "pcbnew is not importable in this environment")
class DrillHolesFromBoardTests(unittest.TestCase):
    def test_get_drill_holes_from_board_negates_y(self):
        import pcbnew

        board = pcbnew.BOARD()
        via = pcbnew.PCB_VIA(board)
        via.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(5), pcbnew.FromMM(3)))
        via.SetDrill(pcbnew.FromMM(0.4))
        board.Add(via)

        holes = pg.get_drill_holes_from_board(board)
        self.assertEqual(len(holes), 1)
        x, y, diameter = holes[0]
        self.assertAlmostEqual(x, 5.0, places=6)
        self.assertAlmostEqual(y, -3.0, places=6)
        self.assertAlmostEqual(diameter, 0.4, places=6)


if __name__ == "__main__":
    unittest.main()

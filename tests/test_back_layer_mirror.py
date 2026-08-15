"""Tests for back-side layer mirroring (contour_offsets.py mirror helpers,
and offline_export.py's mirror-axis resolution + application in the
DXF-based export pipeline).

Back-side layers (B.Cu, B.Mask, etc.) are never mirrored by kicad-cli's DXF
export or by this app's pcbnew-native geometry source on their own, so this
app mirrors them explicitly across the board's own Edge.Cuts bbox center --
so the exported geometry lines up once the board is physically flipped over
to work on its back side. Front-side layers and Edge.Cuts itself are left
untouched.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import minidxf as ezdxf
from contour_offsets import is_back_layer, mirror_ring_x, mirror_rings_x, mirror_segments_x
from offline_export import (
    _board_edge_cuts_x_center_from_text,
    generate_contour_offset_dxf,
    generate_hatch_dxf,
    resolve_mirror_axis_mm,
)


class IsBackLayerTests(unittest.TestCase):
    def test_back_copper_and_mask_are_back_layers(self):
        for name in ("B.Cu", "B.Mask", "B.SilkS", "b.cu", "  B.Cu  "):
            self.assertTrue(is_back_layer(name), f"{name!r} should be a back layer")

    def test_front_and_edge_cuts_are_not_back_layers(self):
        for name in ("F.Cu", "F.Mask", "Edge.Cuts", "DRILL_GEN"):
            self.assertFalse(is_back_layer(name), f"{name!r} should not be a back layer")


class MirrorHelperTests(unittest.TestCase):
    def test_mirror_ring_x_flips_x_keeps_y(self):
        ring = [(0.0, 1.0), (4.0, 1.0), (4.0, 5.0)]
        mirrored = mirror_ring_x(ring, axis_x=2.0)
        self.assertEqual(mirrored, [(4.0, 1.0), (0.0, 1.0), (0.0, 5.0)])

    def test_mirror_rings_x_applies_to_every_ring(self):
        rings = [[(0.0, 0.0), (2.0, 0.0)], [(1.0, 1.0)]]
        mirrored = mirror_rings_x(rings, axis_x=1.0)
        self.assertEqual(mirrored, [[(2.0, 0.0), (0.0, 0.0)], [(1.0, 1.0)]])

    def test_mirror_segments_x_flips_x_keeps_y(self):
        segments = [[[0.0, 3.0], [2.0, 4.0]]]
        mirrored = mirror_segments_x(segments, axis_x=1.0)
        self.assertEqual(mirrored, [[[2.0, 3.0], [0.0, 4.0]]])


_MINIMAL_BOARD_WITH_EDGE_CUTS = """
(kicad_pcb
  (version 20240108)
  (generator "test")
  (gr_line (start 0 0) (end 20 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "a"))
  (gr_line (start 20 0) (end 20 10) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "b"))
  (gr_line (start 20 10) (end 0 10) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "c"))
  (gr_line (start 0 10) (end 0 0) (stroke (width 0.1) (type solid)) (layer "Edge.Cuts") (uuid "d"))
)
"""


class BoardEdgeCutsTextFallbackTests(unittest.TestCase):
    def _write_board(self) -> Path:
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_WITH_EDGE_CUTS)
        return board_path

    def test_returns_bbox_center_of_edge_cuts(self):
        board_path = self._write_board()
        axis = _board_edge_cuts_x_center_from_text(board_path)
        self.assertIsNotNone(axis)
        self.assertAlmostEqual(axis, 10.0, places=6)

    def test_returns_none_without_edge_cuts(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "empty.kicad_pcb"
        board_path.write_text("(kicad_pcb (version 20240108) (generator \"test\"))")
        self.assertIsNone(_board_edge_cuts_x_center_from_text(board_path))


class ResolveMirrorAxisMmTests(unittest.TestCase):
    def test_front_layer_needs_no_mirroring(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_WITH_EDGE_CUTS)
        self.assertIsNone(resolve_mirror_axis_mm(board_path, "F.Cu"))
        self.assertIsNone(resolve_mirror_axis_mm(board_path, "Edge.Cuts"))

    def test_back_layer_resolves_axis_from_board_text(self):
        import pcbnew_geometry as pg

        if pg.is_pcbnew_available():
            self.skipTest("pcbnew is importable; a real board load path is exercised instead")

        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_WITH_EDGE_CUTS)
        axis = resolve_mirror_axis_mm(board_path, "B.Cu")
        self.assertIsNotNone(axis)
        self.assertAlmostEqual(axis, 10.0, places=6)

    def test_bare_dxf_source_has_no_board_reference(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        dxf_path = tmp_dir / "source.dxf"
        dxf_path.write_text("not really used, _resolve_board_path should reject the extension")
        self.assertIsNone(resolve_mirror_axis_mm(dxf_path, "B.Cu"))


def _write_square_dxf(path: Path, layer: str, corner: tuple[float, float], size: float) -> None:
    doc = ezdxf.new("R2000")
    doc.header["$INSUNITS"] = 4
    if layer not in doc.layers:
        doc.layers.new(layer, dxfattribs={"color": 1})
    msp = doc.modelspace()
    x0, y0 = corner
    pts = [(x0, y0), (x0 + size, y0), (x0 + size, y0 + size), (x0, y0 + size), (x0, y0)]
    msp.add_lwpolyline(pts, close=False, dxfattribs={"layer": layer})
    doc.saveas(str(path))


class GenerateContourOffsetDxfMirrorTests(unittest.TestCase):
    def test_mirror_axis_mm_flips_output_loops_in_x(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        source_path = tmp_dir / "source.dxf"
        _write_square_dxf(source_path, "B.Cu", corner=(0.0, 0.0), size=4.0)

        plain_path = tmp_dir / "plain.dxf"
        generate_contour_offset_dxf(source_path, plain_path, 0.1, 0.1, 1, layer_name="B.Cu")
        mirrored_path = tmp_dir / "mirrored.dxf"
        generate_contour_offset_dxf(
            source_path, mirrored_path, 0.1, 0.1, 1, layer_name="B.Cu", mirror_axis_mm=2.0
        )

        def _first_loop_xs(path: Path) -> list[float]:
            doc = ezdxf.readfile(str(path))
            msp = doc.modelspace()
            entity = next(iter(msp))
            return [pt[0] for pt in entity.get_points()]

        plain_xs = _first_loop_xs(plain_path)
        mirrored_xs = _first_loop_xs(mirrored_path)
        self.assertEqual(len(plain_xs), len(mirrored_xs))
        for px, mx in zip(plain_xs, mirrored_xs):
            self.assertAlmostEqual(mx, 2.0 * 2.0 - px, places=6)


if __name__ == "__main__":
    unittest.main()

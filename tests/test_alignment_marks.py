"""Tests for corner alignment marks (contour_offsets.py's
``corner_alignment_mark_segments`` and offline_export.py's Edge.Cuts bbox
resolution + application in the DXF-based export pipeline).

Some fiber-laser controllers compute their own bounding box from whatever
geometry is present in a loaded file and center/align the job on that box,
rather than trusting the file's own coordinates. Since each exported layer
(F.Cu, B.Mask, drill holes, Edge.Cuts, ...) can contain different geometry --
and therefore a different bounding box -- the machine can end up centering
each layer's file slightly differently, throwing an otherwise-aligned
multi-layer job out of registration.

The fix: compute the board's own Edge.Cuts bounding box, expand it by a
margin, and draw tiny "L" corner tick marks at the expanded box's 4 corners
into every exported file, so they all share an identical overall bounding
box regardless of which layer's geometry the rest of the file holds.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import minidxf as ezdxf
from contour_offsets import corner_alignment_mark_segments
from offline_export import (
    _board_edge_cuts_bbox_from_text,
    generate_contour_offset_dxf,
    generate_hatch_dxf,
    resolve_edge_cuts_bbox_mm,
)


class CornerAlignmentMarkSegmentsTests(unittest.TestCase):
    def test_returns_eight_segments_two_per_corner(self):
        segments = corner_alignment_mark_segments((0.0, 0.0, 10.0, 20.0))
        self.assertEqual(len(segments), 8)

    def test_extreme_points_match_margin_expanded_bbox_corners(self):
        bbox = (0.0, 0.0, 10.0, 20.0)
        segments = corner_alignment_mark_segments(bbox, margin_mm=1.0, mark_length_mm=0.5)
        xs = [p[0] for seg in segments for p in seg]
        ys = [p[1] for seg in segments for p in seg]
        self.assertAlmostEqual(min(xs), -1.0, places=6)
        self.assertAlmostEqual(max(xs), 11.0, places=6)
        self.assertAlmostEqual(min(ys), -1.0, places=6)
        self.assertAlmostEqual(max(ys), 21.0, places=6)

    def test_marks_are_tiny_relative_to_board(self):
        bbox = (0.0, 0.0, 100.0, 100.0)
        segments = corner_alignment_mark_segments(bbox, margin_mm=1.0, mark_length_mm=0.5)
        for p1, p2 in segments:
            length = ((p2[0] - p1[0]) ** 2 + (p2[1] - p1[1]) ** 2) ** 0.5
            self.assertAlmostEqual(length, 0.5, places=6)

    def test_each_corner_forms_an_l_bracket_pointing_inward(self):
        bbox = (0.0, 0.0, 10.0, 10.0)
        segments = corner_alignment_mark_segments(bbox, margin_mm=1.0, mark_length_mm=1.0)
        # Bottom-left corner of the expanded bbox is (-1, -1); both of its
        # segments should start there and point toward positive X/Y (inward).
        bl_segments = [seg for seg in segments if seg[0] == [-1.0, -1.0]]
        self.assertEqual(len(bl_segments), 2)
        endpoints = sorted(seg[1] for seg in bl_segments)
        self.assertEqual(endpoints, [[-1.0, 0.0], [0.0, -1.0]])


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


class BoardEdgeCutsBboxTextFallbackTests(unittest.TestCase):
    def _write_board(self) -> Path:
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_WITH_EDGE_CUTS)
        return board_path

    def test_returns_bbox_with_y_negated(self):
        board_path = self._write_board()
        bbox = _board_edge_cuts_bbox_from_text(board_path)
        self.assertIsNotNone(bbox)
        minx, miny, maxx, maxy = bbox
        self.assertAlmostEqual(minx, 0.0, places=6)
        self.assertAlmostEqual(maxx, 20.0, places=6)
        # Raw text Y spans 0..10; DXF-frame Y is negated, so it becomes -10..0.
        self.assertAlmostEqual(miny, -10.0, places=6)
        self.assertAlmostEqual(maxy, 0.0, places=6)

    def test_returns_none_without_edge_cuts(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "empty.kicad_pcb"
        board_path.write_text("(kicad_pcb (version 20240108) (generator \"test\"))")
        self.assertIsNone(_board_edge_cuts_bbox_from_text(board_path))


class ResolveEdgeCutsBboxMmTests(unittest.TestCase):
    def test_resolves_from_board_text(self):
        import pcbnew_geometry as pg

        if pg.is_pcbnew_available():
            self.skipTest("pcbnew is importable; a real board load path is exercised instead")

        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        board_path = tmp_dir / "test.kicad_pcb"
        board_path.write_text(_MINIMAL_BOARD_WITH_EDGE_CUTS)
        bbox = resolve_edge_cuts_bbox_mm(board_path)
        self.assertIsNotNone(bbox)
        self.assertAlmostEqual(bbox[0], 0.0, places=6)
        self.assertAlmostEqual(bbox[2], 20.0, places=6)

    def test_bare_dxf_source_has_no_board_reference(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        dxf_path = tmp_dir / "source.dxf"
        dxf_path.write_text("not really used, _resolve_board_path should reject the extension")
        self.assertIsNone(resolve_edge_cuts_bbox_mm(dxf_path))


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


def _dxf_lines(path: Path) -> list[tuple[tuple[float, float], tuple[float, float]]]:
    doc = ezdxf.readfile(str(path))
    msp = doc.modelspace()
    return [
        ((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y))
        for e in msp
        if e.dxftype() == "LINE"
    ]


class GeneratedDxfCornerMarkTests(unittest.TestCase):
    def test_contour_offset_dxf_includes_corner_marks_when_bbox_given(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        source_path = tmp_dir / "source.dxf"
        _write_square_dxf(source_path, "F.Cu", corner=(2.0, 2.0), size=4.0)

        bbox = (0.0, 0.0, 8.0, 8.0)
        out_path = tmp_dir / "out.dxf"
        generate_contour_offset_dxf(
            source_path, out_path, 0.1, 0.1, 1, layer_name="F.Cu", edge_cuts_bbox_mm=bbox
        )

        lines = _dxf_lines(out_path)
        self.assertEqual(len(lines), 8, "expected 8 tiny corner-mark line segments")
        xs = [c for line in lines for p in line for c in [p[0]]]
        ys = [c for line in lines for p in line for c in [p[1]]]
        self.assertAlmostEqual(min(xs), -1.0, places=6)
        self.assertAlmostEqual(max(xs), 9.0, places=6)
        self.assertAlmostEqual(min(ys), -1.0, places=6)
        self.assertAlmostEqual(max(ys), 9.0, places=6)

    def test_contour_offset_dxf_omits_corner_marks_without_bbox(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        source_path = tmp_dir / "source.dxf"
        _write_square_dxf(source_path, "F.Cu", corner=(2.0, 2.0), size=4.0)

        out_path = tmp_dir / "out.dxf"
        generate_contour_offset_dxf(source_path, out_path, 0.1, 0.1, 1, layer_name="F.Cu")

        self.assertEqual(_dxf_lines(out_path), [])

    def test_hatch_dxf_includes_corner_marks_when_bbox_given(self):
        tmp_dir = Path(tempfile.mkdtemp(prefix="fiberlasercam-test-"))
        source_path = tmp_dir / "source.dxf"
        _write_square_dxf(source_path, "F.Cu", corner=(2.0, 2.0), size=4.0)

        bbox = (0.0, 0.0, 8.0, 8.0)
        out_path = tmp_dir / "out.dxf"
        generate_hatch_dxf(
            source_path, out_path, 45.0, 0.5, layer_name="F.Cu", edge_cuts_bbox_mm=bbox
        )

        lines = _dxf_lines(out_path)
        xs = [p[0] for line in lines for p in line]
        ys = [p[1] for line in lines for p in line]
        # Hatch lines stay inside the source square (2..6); corner marks
        # extend 1mm beyond the given 0..8 bbox, so they're the extremes.
        self.assertAlmostEqual(min(xs), -1.0, places=6)
        self.assertAlmostEqual(max(xs), 9.0, places=6)
        self.assertAlmostEqual(min(ys), -1.0, places=6)
        self.assertAlmostEqual(max(ys), 9.0, places=6)


if __name__ == "__main__":
    unittest.main()

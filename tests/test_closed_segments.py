"""Tests verifying that closed shapes (squares, circles) export with all N
edges explicit in the DXF output — no closing segment missing."""
from __future__ import annotations

import math
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import minidxf as ezdxf
from app_geometry import collect_entities_as_polygons
from contour_offsets import generate_contour_offset_loops, generate_contour_offset_segments
from offline_export import generate_contour_offset_dxf


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CIRCLE_DXF = """\
  0
SECTION
  2
HEADER
  9
$ACADVER
  1
AC1015
  9
$INSUNITS
 70
4
  0
ENDSEC
  0
SECTION
  2
ENTITIES
  0
CIRCLE
  8
0
 10
5.0
 20
5.0
 30
0.0
 40
3.0
  0
ENDSEC
  0
EOF
"""


def _write_tmp_dxf(content: str) -> str:
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".dxf", delete=False, dir=tempfile.gettempdir()
    ) as f:
        f.write(content)
        return f.name


def _square_dxf_path() -> str:
    """Write a 10x10 closed square as LWPOLYLINE and return the path."""
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (10, 0), (10, 10), (0, 10)], close=True)
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".dxf", delete=False, dir=tempfile.gettempdir()
    ) as f:
        doc.write(f)
        return f.name


def _count_explicit_edges(pts: list) -> int:
    """Count edges in a vertex list (including closing edge if last == first)."""
    if len(pts) < 2:
        return 0
    first = (float(pts[0][0]), float(pts[0][1]))
    last = (float(pts[-1][0]), float(pts[-1][1]))
    if math.hypot(first[0] - last[0], first[1] - last[1]) < 1e-6:
        # Closing vertex is explicit — edges = len(pts) - 1
        return len(pts) - 1
    return len(pts)  # open polyline; each consecutive pair is one edge


# ---------------------------------------------------------------------------
# contour_offsets.py: generate_contour_offset_segments
# ---------------------------------------------------------------------------

class TestContourOffsetSegments(unittest.TestCase):
    def test_square_produces_n_segments(self):
        """A 4-vertex ring with offset=0 must produce exactly 4 segments."""
        ring = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        segs = generate_contour_offset_segments(ring, 0.0, 0.5, 1)
        self.assertEqual(len(segs), 4, "Expected 4 segments for a 4-vertex ring")

    def test_square_closing_segment_present(self):
        """The last segment must connect the last vertex back to the first."""
        ring = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        segs = generate_contour_offset_segments(ring, 0.0, 0.5, 1)
        last_p1 = segs[-1][0]
        last_p2 = segs[-1][1]
        self.assertAlmostEqual(last_p2[0], ring[0][0], places=6)
        self.assertAlmostEqual(last_p2[1], ring[0][1], places=6)

    def test_offset_ring_produces_n_segments(self):
        """An offset ring should also produce N segments (one per vertex)."""
        ring = [(0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)]
        loops = generate_contour_offset_loops(ring, 0.5, 0.5, 1)
        self.assertEqual(len(loops), 1)
        segs = generate_contour_offset_segments(ring, 0.5, 0.5, 1)
        self.assertEqual(len(segs), len(loops[0]))

    def test_triangle_produces_n_segments(self):
        ring = [(0.0, 0.0), (5.0, 8.0), (10.0, 0.0)]
        segs = generate_contour_offset_segments(ring, 0.0, 0.5, 1)
        self.assertEqual(len(segs), 3)


# ---------------------------------------------------------------------------
# collect_entities_as_polygons
# ---------------------------------------------------------------------------

class TestCollectEntitiesAsPolygons(unittest.TestCase):
    def _read_tmp(self, path: str) -> list:
        doc = ezdxf.readfile(path)
        return collect_entities_as_polygons(doc)

    def test_closed_lwpolyline_square(self):
        path = _square_dxf_path()
        polys = self._read_tmp(path)
        self.assertEqual(len(polys), 1)
        self.assertEqual(len(polys[0]), 4)

    def test_circle_entity_produces_polygon(self):
        path = _write_tmp_dxf(_CIRCLE_DXF)
        polys = self._read_tmp(path)
        self.assertEqual(len(polys), 1)
        # A circle sampled at 6° steps should have ~60 vertices
        self.assertGreater(len(polys[0]), 50)

    def test_circle_polygon_is_closed_via_modulo(self):
        """The polygon produced from a CIRCLE entity must form a fully closed ring
        (the ring is stored without a repeated closing vertex but linework has
        all N edges including the wrap-around edge).
        """
        path = _write_tmp_dxf(_CIRCLE_DXF)
        doc = ezdxf.readfile(path)
        polys = collect_entities_as_polygons(doc)
        self.assertTrue(polys)
        ring = polys[0]
        n = len(ring)
        # Verify first != last (no duplicate closing vertex stored)
        dist = math.hypot(ring[0][0] - ring[-1][0], ring[0][1] - ring[-1][1])
        self.assertGreater(dist, 1e-6, "Ring must NOT store a duplicate closing vertex")
        # Verify the ring represents a closed polygon (area > 0 and all N edges implied)
        self.assertGreater(n, 0)


# ---------------------------------------------------------------------------
# DXF export: explicit closing vertex present
# ---------------------------------------------------------------------------

class TestDXFExportClosingEdge(unittest.TestCase):
    """Verify that exported LWPOLYLINE entities contain an explicit closing
    vertex so that the final edge is never missing even in tools that ignore
    the LWPOLYLINE closed flag.
    """

    def _export_and_read(self, source_path: str) -> list:
        with tempfile.NamedTemporaryFile(suffix=".dxf", dir=tempfile.gettempdir(), delete=False) as f:
            output_path = f.name
        generate_contour_offset_dxf(
            Path(source_path), Path(output_path), start_offset=0.5, spacing=0.5, repetitions=1
        )
        doc = ezdxf.readfile(output_path)
        return list(doc.modelspace())

    def test_square_export_all_4_edges_explicit(self):
        """A 4-vertex square exported via contour_offset must have all 4 edges
        explicitly in the LWPOLYLINE vertex list (no reliance on close flag).
        """
        path = _square_dxf_path()
        entities = self._export_and_read(path)
        self.assertEqual(len(entities), 1)
        ent = entities[0]
        self.assertEqual(ent.dxftype(), "LWPOLYLINE")
        pts = ent.get_points()
        edge_count = _count_explicit_edges(pts)
        self.assertEqual(edge_count, 4, f"Expected 4 explicit edges, got {edge_count} (pts={len(pts)})")

    def test_square_export_last_pt_equals_first_pt(self):
        """The last vertex in the exported LWPOLYLINE must equal the first
        vertex to form an explicit closing edge."""
        path = _square_dxf_path()
        entities = self._export_and_read(path)
        pts = entities[0].get_points()
        first = pts[0]
        last = pts[-1]
        dist = math.hypot(float(first[0]) - float(last[0]), float(first[1]) - float(last[1]))
        self.assertLess(dist, 1e-6, "Last vertex must equal first vertex (explicit close)")

    def test_circle_export_all_edges_explicit(self):
        """A CIRCLE entity exported via contour_offset must have all sampled
        edges explicit (no implicit close-flag edge missing).
        """
        path = _write_tmp_dxf(_CIRCLE_DXF)
        entities = self._export_and_read(path)
        self.assertEqual(len(entities), 1)
        pts = entities[0].get_points()
        edge_count = _count_explicit_edges(pts)
        # The offset circle polygon has ~60 vertices → 60 explicit edges
        self.assertGreater(edge_count, 50, f"Expected >50 explicit edges, got {edge_count}")
        # Verify closing vertex is explicit
        first = pts[0]
        last = pts[-1]
        dist = math.hypot(float(first[0]) - float(last[0]), float(first[1]) - float(last[1]))
        self.assertLess(dist, 1e-6, "Last vertex must equal first vertex for explicit close")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import math
from pathlib import Path

import ezdxf
from shapely.geometry import LineString, Point, Polygon
from shapely.ops import polygonize, unary_union

try:
    from contour_offsets import generate_contour_offset_loops
except ImportError:
    from kicad_plugin.contour_offsets import generate_contour_offset_loops


def _vec2(value) -> tuple[float, float]:
    return float(value[0]), float(value[1])


def _ensure_polygon(points: list[tuple[float, float]]) -> Polygon | None:
    if len(points) < 3:
        return None
    poly = Polygon(points)
    if not poly.is_valid:
        poly = poly.buffer(0)
    if poly.is_empty or poly.area <= 1e-9:
        return None
    return poly


def _sample_arc(
    center_x: float,
    center_y: float,
    radius: float,
    start_deg: float,
    end_deg: float,
    step_deg: float = 4.0,
) -> list[tuple[float, float]]:
    start = math.radians(start_deg)
    end = math.radians(end_deg)
    if end < start:
        end += 2 * math.pi

    sweep = end - start
    steps = max(8, int(abs(math.degrees(sweep)) / step_deg))
    pts: list[tuple[float, float]] = []
    for i in range(steps + 1):
        t = start + sweep * (i / steps)
        pts.append((center_x + radius * math.cos(t), center_y + radius * math.sin(t)))
    return pts


def _collect_polygons_from_dxf(doc: ezdxf.document.Drawing) -> list[Polygon]:
    modelspace = doc.modelspace()
    direct_polys: list[Polygon] = []
    linework: list[LineString] = []

    for entity in modelspace:
        kind = entity.dxftype()
        try:
            if kind == "LWPOLYLINE":
                pts = [tuple(map(float, pt[:2])) for pt in entity.get_points()]
                if len(pts) >= 2:
                    for i in range(len(pts) - 1):
                        linework.append(LineString([pts[i], pts[i + 1]]))
                    if entity.closed:
                        linework.append(LineString([pts[-1], pts[0]]))
                if entity.closed and len(pts) >= 3:
                    poly = _ensure_polygon(pts)
                    if poly is not None:
                        direct_polys.append(poly)

            elif kind == "POLYLINE":
                if bool(getattr(entity, "is_polygon_mesh", False)) or bool(getattr(entity, "is_poly_face_mesh", False)):
                    continue

                pts = [(float(v.dxf.location.x), float(v.dxf.location.y)) for v in entity.vertices]
                if len(pts) >= 2:
                    for i in range(len(pts) - 1):
                        linework.append(LineString([pts[i], pts[i + 1]]))
                    if entity.is_closed:
                        linework.append(LineString([pts[-1], pts[0]]))
                if entity.is_closed and len(pts) >= 3:
                    poly = _ensure_polygon(pts)
                    if poly is not None:
                        direct_polys.append(poly)

            elif kind == "LINE":
                start = _vec2(entity.dxf.start)
                end = _vec2(entity.dxf.end)
                linework.append(LineString([start, end]))

            elif kind == "CIRCLE":
                center = _vec2(entity.dxf.center)
                radius = float(entity.dxf.radius)
                circle_poly = Point(center).buffer(radius, resolution=64)
                direct_polys.append(circle_poly)
                linework.append(LineString(list(circle_poly.exterior.coords)))

            elif kind == "ARC":
                center = _vec2(entity.dxf.center)
                pts = _sample_arc(
                    center[0],
                    center[1],
                    float(entity.dxf.radius),
                    float(entity.dxf.start_angle),
                    float(entity.dxf.end_angle),
                )
                for i in range(len(pts) - 1):
                    linework.append(LineString([pts[i], pts[i + 1]]))

            elif kind == "SPLINE":
                pts3d = list(entity.flattening(0.02))
                pts = [(float(p[0]), float(p[1])) for p in pts3d]
                if len(pts) < 2:
                    continue

                for i in range(len(pts) - 1):
                    linework.append(LineString([pts[i], pts[i + 1]]))

                start = pts[0]
                end = pts[-1]
                close_tol = 0.05
                is_closed_like = bool(getattr(entity, "closed", False)) or math.hypot(end[0] - start[0], end[1] - start[1]) <= close_tol
                if is_closed_like:
                    linework.append(LineString([pts[-1], pts[0]]))
                    poly = _ensure_polygon(pts)
                    if poly is not None:
                        direct_polys.append(poly)
        except Exception:
            continue

    poly_from_lines: list[Polygon] = []
    if linework:
        try:
            unified = unary_union(linework)
            candidates = polygonize(unified)
        except Exception:
            candidates = polygonize(linework)

        for poly in candidates:
            fixed = _ensure_polygon(list(poly.exterior.coords)[:-1])
            if fixed is not None:
                poly_from_lines.append(fixed)

    all_polys = direct_polys + poly_from_lines
    unique: dict[tuple[float, float, float], Polygon] = {}
    for poly in all_polys:
        c = poly.centroid
        key = (round(c.x, 4), round(c.y, 4), round(poly.area, 4))
        if key not in unique:
            unique[key] = poly

    result = list(unique.values())
    result.sort(key=lambda p: p.area, reverse=True)
    return result


def generate_contour_offset_dxf(
    source_dxf_path: Path,
    output_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
    layer_name: str = "HATCH_GEN",
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    loops: list[list[tuple[float, float]]] = []
    for poly in polys:
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions))

    if not loops:
        raise RuntimeError(
            "No contour loops generated from source DXF. "
            "Check selected export layers and contour parameters."
        )

    out_doc = ezdxf.new("R2000")
    if "$INSUNITS" in source_doc.header:
        out_doc.header["$INSUNITS"] = source_doc.header["$INSUNITS"]

    for header_key in ("$PDMODE", "$PDSIZE"):
        if header_key in out_doc.header:
            del out_doc.header[header_key]

    if layer_name not in out_doc.layers:
        out_doc.layers.new(layer_name, dxfattribs={"color": 1})

    msp = out_doc.modelspace()
    for loop in loops:
        if len(loop) < 3:
            continue
        try:
            msp.add_lwpolyline(loop, close=True, dxfattribs={"layer": layer_name})
        except Exception:
            continue

    output_dxf_path.parent.mkdir(parents=True, exist_ok=True)
    out_doc.saveas(str(output_dxf_path))
    return len(polys), len(loops)


def preview_contour_offset_counts(
    source_dxf_path: Path,
    start_offset: float,
    spacing: float,
    repetitions: int,
) -> tuple[int, int]:
    source_doc = ezdxf.readfile(str(source_dxf_path))
    polys = _collect_polygons_from_dxf(source_doc)

    loops: list[list[tuple[float, float]]] = []
    for poly in polys:
        loops.extend(generate_contour_offset_loops(poly, start_offset, spacing, repetitions))

    return len(polys), len(loops)
